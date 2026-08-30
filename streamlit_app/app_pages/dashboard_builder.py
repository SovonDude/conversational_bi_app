import datetime
import io
import json
import re
import zipfile

import pandas as pd
import streamlit as st

from app_pages._shared import (
    BRAND, COLORS, BasePDF, vega_config, extract_followups, strip_sql_blocks,
    LOGISTICS_SCHEMA, llm_query,
)

conn = st.session_state.conn
call_agent = st.session_state.shared["call_agent"]
fmt_number = st.session_state.shared["fmt_number"]
to_float = st.session_state.shared["to_float"]
BMK_EXPR = st.session_state.shared["BMK_EXPR"]
BMK_EXPR_DOLLAR = st.session_state.shared["BMK_EXPR_DOLLAR"]

st.session_state.dash_chat_open = False

_UPLOAD_OPTION = "Upload New Data"
_TEMP_TABLE_PREFIX = "CONVERSATIONAL_BI_ASSISTANT.USER_UPLOADS.__TEMP_DB_"

CHART_TYPES = {
    "Bar": "bar", "H-Bar": "barh", "Line": "line",
    "Area": "area", "Donut": "pie", "Table": "table",
}

BUILDER_SUGGESTIONS = [
    (":material/trending_up:", ":blue[Revenue by region]", "Show me total revenue by region as a bar chart"),
    (":material/schedule:", ":green[Delivery times]", "Show average delivery time by ship mode"),
    (":material/pie_chart:", ":violet[Market share]", "Show market segment breakdown as a donut chart"),
    (":material/public:", ":gray[Top nations]", "Show the top 10 nations by revenue"),
]

GENERIC_BUILDER_SUGGESTIONS = [
    (":material/bar_chart:", ":blue[Overview]", "Show me an overview chart of the main metrics"),
    (":material/trending_up:", ":green[Trends]", "Show trends over time for the key metric"),
    (":material/pie_chart:", ":violet[Breakdown]", "Break down the data by the main category"),
    (":material/search:", ":orange[Top records]", "Show the top 10 records by the most important metric"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SQL_PATTERNS = [
    re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE),
    re.compile(r"```\s*\n(SELECT.*?)```", re.DOTALL | re.IGNORECASE),
]


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name.strip()).upper()


def _try_extract_sql(text):
    for pat in _SQL_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def _render_card_chart(df, chart_type="bar", height=220):
    if df.empty:
        st.info("No data.")
        return
    num_cols = [c for c in df.columns if df[c].dtype in ("float64", "int64", "float32")]
    str_cols = [c for c in df.columns if df[c].dtype == "object"]
    if chart_type == "table" or not num_cols:
        st.dataframe(df, hide_index=True, use_container_width=True)
        return
    y_col = num_cols[0]
    x_col = str_cols[0] if str_cols else df.columns[0]
    is_dollar = any(k in y_col.upper() for k in ("REVENUE", "PRICE", "COST", "BALANCE", "VALUE", "TOTAL"))
    axis_expr = BMK_EXPR_DOLLAR if is_dollar else BMK_EXPR
    chart_df = df.copy()
    fmt_col = f"{y_col}_fmt"
    chart_df[fmt_col] = chart_df[y_col].apply(lambda v: fmt_number(v, prefix="$" if is_dollar else ""))
    tooltips = [{"field": c, "title": c.replace("_", " ").title(), "type": "nominal"} for c in str_cols]
    tooltips.append({"field": fmt_col, "title": y_col.replace("_", " ").title(), "type": "nominal"})
    cfg = vega_config(st.session_state.get("dark_mode", False))

    if chart_type == "pie":
        st.vega_lite_chart(chart_df, {"height": height,
            "mark": {"type": "arc", "innerRadius": 50, "outerRadius": height // 2 - 20},
            "encoding": {"theta": {"field": y_col, "type": "quantitative", "stack": True},
                         "color": {"field": x_col, "type": "nominal"}, "tooltip": tooltips}},
            use_container_width=True)
    elif chart_type == "barh":
        st.vega_lite_chart(chart_df, {"height": max(len(df) * 26, 150),
            "mark": {"type": "bar", "cornerRadiusEnd": 4},
            "encoding": {"y": {"field": x_col, "type": "nominal", "sort": "-x"},
                         "x": {"field": y_col, "type": "quantitative", "axis": {"labelExpr": axis_expr}},
                         "tooltip": tooltips}, "config": cfg}, use_container_width=True)
    elif chart_type in ("line", "area"):
        mark = {"type": chart_type, "opacity": 0.4, "line": True} if chart_type == "area" else {"type": "line", "strokeWidth": 2, "point": {"size": 40}}
        x_type = "temporal" if x_col.upper() in ("MONTH", "DATE", "YEAR", "DAY", "WEEK", "QUARTER") else "nominal"
        st.vega_lite_chart(chart_df, {"height": height, "mark": mark,
            "encoding": {"x": {"field": x_col, "type": x_type},
                         "y": {"field": y_col, "type": "quantitative", "axis": {"labelExpr": axis_expr}},
                         "tooltip": tooltips}, "config": cfg}, use_container_width=True)
    else:
        st.vega_lite_chart(chart_df, {"height": height,
            "mark": {"type": "bar", "cornerRadiusEnd": 4},
            "encoding": {"x": {"field": x_col, "type": "nominal", "sort": "-y"},
                         "y": {"field": y_col, "type": "quantitative", "axis": {"labelExpr": axis_expr}},
                         "tooltip": tooltips}, "config": cfg}, use_container_width=True)


def _write_temp_table(df, name="UPLOADED_DATA"):
    safe = _safe_name(name)
    fqn = f"{_TEMP_TABLE_PREFIX}{safe}"
    session = conn.session()
    session.sql("CREATE SCHEMA IF NOT EXISTS CONVERSATIONAL_BI_ASSISTANT.USER_UPLOADS").collect()
    snowpark_df = session.create_dataframe(df)
    snowpark_df.write.mode("overwrite").save_as_table(fqn, table_type="temporary")
    return fqn


@st.cache_data(ttl=60)
def _list_schemas():
    df = conn.query(
        "SELECT SCHEMA_NAME FROM CONVERSATIONAL_BI_ASSISTANT.INFORMATION_SCHEMA.SCHEMATA "
        "WHERE SCHEMA_NAME NOT IN ('INFORMATION_SCHEMA', 'PUBLIC', 'SEMANTIC_MODELS', 'USER_UPLOADS') "
        "ORDER BY SCHEMA_NAME"
    )
    return df["SCHEMA_NAME"].tolist()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
if "db_messages" not in st.session_state:
    st.session_state.db_messages = []
if "db_cards" not in st.session_state:
    st.session_state.db_cards = []  # [{title, df, chart_type, sql}]
if "db_data_source" not in st.session_state:
    st.session_state.db_data_source = "LOGISTICS_DATA"
if "db_uploaded_df" not in st.session_state:
    st.session_state.db_uploaded_df = None
if "db_uploaded_files" not in st.session_state:
    st.session_state.db_uploaded_files = {}
if "db_upload_zip_name" not in st.session_state:
    st.session_state.db_upload_zip_name = ""
if "db_upload_ready" not in st.session_state:
    st.session_state.db_upload_ready = False
if "db_temp_tables" not in st.session_state:
    st.session_state.db_temp_tables = []
if "db_dash_name" not in st.session_state:
    st.session_state.db_dash_name = "My Dashboard"

# ---------------------------------------------------------------------------
# Title + Action buttons
# ---------------------------------------------------------------------------
with st.container(horizontal=True):
    st.subheader(":material/dashboard_customize: BI Assistant Dashboard Builder")
    with st.popover(f":material/database: {st.session_state.db_data_source}"):
        schemas = _list_schemas()
        for schema in schemas:
            if st.button(schema, use_container_width=True, key=f"db_src_{schema}",
                         type="primary" if st.session_state.db_data_source == schema else "secondary"):
                st.session_state.db_data_source = schema
                st.session_state.db_uploaded_df = None
                st.session_state.db_uploaded_files = {}
                st.rerun()
        st.divider()
        if st.button(f":material/upload: {_UPLOAD_OPTION}", use_container_width=True, key="db_src_upload",
                     type="primary" if st.session_state.db_data_source == _UPLOAD_OPTION else "secondary"):
            st.session_state.db_data_source = _UPLOAD_OPTION
            st.session_state.db_uploaded_df = None
            st.session_state.db_uploaded_files = {}
            st.session_state.db_upload_ready = False
            st.session_state.db_temp_tables = []
            st.rerun()
    # Publish
    with st.popover("Publish", icon=":material/publish:"):
        dash_name = st.text_input("Dashboard name", value=st.session_state.db_dash_name,
                                   key="db_publish_name")
        if st.session_state.db_cards:
            if st.button("Publish Dashboard", icon=":material/check_circle:",
                         use_container_width=True, key="db_publish_btn",
                         disabled=not dash_name.strip()):
                # Serialize cards config to Snowflake
                cards_config = []
                for card in st.session_state.db_cards:
                    cards_config.append({
                        "title": card["title"],
                        "chart_type": card["chart_type"],
                        "sql": card.get("sql", ""),
                        "data": card["df"].to_dict(orient="records"),
                    })
                config_json = json.dumps({
                    "name": dash_name.strip(),
                    "schema": st.session_state.db_data_source,
                    "cards": cards_config,
                    "created": datetime.datetime.now().isoformat(),
                })
                session = conn.session()
                session.sql("CREATE SCHEMA IF NOT EXISTS CONVERSATIONAL_BI_ASSISTANT.PUBLISHED_DASHBOARDS").collect()
                session.sql(
                    "CREATE TABLE IF NOT EXISTS CONVERSATIONAL_BI_ASSISTANT.PUBLISHED_DASHBOARDS.DASHBOARDS "
                    "(NAME VARCHAR, CONFIG VARIANT, CREATED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP())"
                ).collect()
                session.sql(
                    "INSERT INTO CONVERSATIONAL_BI_ASSISTANT.PUBLISHED_DASHBOARDS.DASHBOARDS (NAME, CONFIG) "
                    "SELECT ?, PARSE_JSON(?)",
                    params=[dash_name.strip(), config_json],
                ).collect()
                st.session_state.db_dash_name = dash_name.strip()
                if "published_dashboards" in st.session_state:
                    del st.session_state["published_dashboards"]
                # Clear builder state
                st.session_state.db_messages = []
                st.session_state.db_cards = []
                st.session_state.db_uploaded_df = None
                st.session_state.db_uploaded_files = {}
                st.session_state.db_upload_ready = False
                st.session_state.db_temp_tables = []
                st.session_state.db_dash_name = "My Dashboard"
                st.rerun()
        else:
            st.caption("Add cards to publish.")
    if st.session_state.db_cards or st.session_state.db_messages:
        if st.button("Clear", icon=":material/delete:", help="Clear all", key="db_clear"):
            st.session_state.db_messages = []
            st.session_state.db_cards = []
            st.rerun()

st.caption("Build dashboards by asking questions. Use the data selector to choose a schema or upload your own data, then publish when ready.")

# Upload panel
if st.session_state.db_data_source == _UPLOAD_OPTION:
    with st.container(border=True):
        uploaded_file = st.file_uploader("Upload CSV, Excel, or ZIP", type=["csv", "xlsx", "xls", "zip"],
                                          label_visibility="collapsed", key="db_file_uploader")
        if uploaded_file is not None:
            try:
                if uploaded_file.name.endswith(".zip"):
                    zip_stem = re.sub(r"\.[^.]+$", "", uploaded_file.name)
                    st.session_state.db_upload_zip_name = zip_stem
                    with zipfile.ZipFile(io.BytesIO(uploaded_file.read())) as zf:
                        data_files = [n for n in zf.namelist()
                                      if not n.startswith("__") and
                                      (n.endswith(".csv") or n.endswith(".xlsx") or n.endswith(".xls"))]
                        if not data_files:
                            st.error("ZIP contains no CSV or Excel files.")
                        else:
                            files_dict = {}
                            for name in data_files:
                                with zf.open(name) as f:
                                    base = re.sub(r"\.[^.]+$", "", name.split("/")[-1])
                                    if name.endswith(".csv"):
                                        files_dict[base] = pd.read_csv(f)
                                    else:
                                        files_dict[base] = pd.read_excel(f)
                            st.session_state.db_uploaded_files = files_dict
                            st.session_state.db_uploaded_df = None
                elif uploaded_file.name.endswith(".csv"):
                    st.session_state.db_uploaded_df = pd.read_csv(uploaded_file)
                    st.session_state.db_uploaded_files = {}
                else:
                    st.session_state.db_uploaded_df = pd.read_excel(uploaded_file)
                    st.session_state.db_uploaded_files = {}
            except Exception as e:
                st.error(f"Failed to read file: {e}")

        has_data = bool(st.session_state.db_uploaded_files) or st.session_state.db_uploaded_df is not None
        if has_data and not st.session_state.db_upload_ready:
            with st.spinner("Loading data for analysis..."):
                temp_tables = []
                if st.session_state.db_uploaded_files:
                    for fname, df in st.session_state.db_uploaded_files.items():
                        temp_tables.append(_write_temp_table(df, fname))
                elif st.session_state.db_uploaded_df is not None:
                    temp_tables.append(_write_temp_table(st.session_state.db_uploaded_df))
                st.session_state.db_temp_tables = temp_tables
                st.session_state.db_upload_ready = True
            st.success("Data loaded — start building your dashboard below.")

# ---------------------------------------------------------------------------
# Main layout: Dashboard grid (left) + Chat (right)
# ---------------------------------------------------------------------------
grid_col, chat_col = st.columns([3, 2])

# --- Dashboard Grid ---
with grid_col:
    if not st.session_state.db_cards:
        st.info("Your dashboard is empty. Use the chat to add charts by asking questions about your data.")
    else:
        # Render cards in 2-column grid
        card_cols = st.columns(2)
        for idx, card in enumerate(st.session_state.db_cards):
            with card_cols[idx % 2]:
                with st.container(border=True):
                    tc1, tc2 = st.columns([5, 1])
                    tc1.markdown(f"**{card['title']}**")
                    if tc2.button(":material/close:", key=f"db_del_{idx}", help="Remove"):
                        st.session_state.db_cards.pop(idx)
                        st.rerun()
                    _render_card_chart(card["df"], chart_type=card["chart_type"], height=200)
                    # Chart type switcher
                    current_name = next(
                        (name for name, t in CHART_TYPES.items() if t == card["chart_type"]), "Bar")
                    new_type = st.selectbox("Chart type", list(CHART_TYPES.keys()),
                                            index=list(CHART_TYPES.keys()).index(current_name),
                                            key=f"db_ct_{idx}", label_visibility="collapsed")
                    if CHART_TYPES[new_type] != card["chart_type"]:
                        st.session_state.db_cards[idx]["chart_type"] = CHART_TYPES[new_type]
                        st.rerun()

# --- Chat Panel ---
with chat_col:
    with st.container(border=True):
        st.markdown("**Chat with BI Assistant**")
        chat_box = st.container(height=450)
        with chat_box:
            if not st.session_state.db_messages:
                src = st.session_state.db_data_source
                suggestions = BUILDER_SUGGESTIONS if src == "LOGISTICS_DATA" else GENERIC_BUILDER_SUGGESTIONS
                if src == _UPLOAD_OPTION:
                    if not st.session_state.db_upload_ready:
                        st.caption("Upload data above to start building.")
                    else:
                        st.caption("Data loaded. Ask questions to create dashboard cards.")
                for icon, label, question in suggestions:
                    if st.button(label, icon=icon, use_container_width=True, key=f"dbsug_{question[:15]}"):
                        st.session_state.db_messages.append({"role": "user", "content": question})
                        st.rerun()
            else:
                for msg in st.session_state.db_messages:
                    with st.chat_message(msg["role"]):
                        if msg["role"] == "assistant":
                            display_text, _ = extract_followups(msg["content"])
                            st.markdown(display_text)
                        else:
                            st.markdown(msg["content"])

        # Chat input
        if prompt := st.chat_input("Describe a chart to add...", key="db_chat_input"):
            st.session_state.db_messages.append({"role": "user", "content": prompt})
            with chat_box:
                with st.chat_message("user"):
                    st.markdown(prompt)

        # Generate response + auto-create card
        if st.session_state.db_messages and st.session_state.db_messages[-1]["role"] == "user":
            user_q = st.session_state.db_messages[-1]["content"]
            selected = st.session_state.db_data_source
            use_agent = selected == LOGISTICS_SCHEMA

            with chat_box:
                with st.chat_message("assistant"):
                    with st.spinner("Thinking..."):
                        try:
                            if use_agent:
                                answer = call_agent(
                                    user_q + "\n\nAlways return a SQL query in a ```sql code block."
                                )
                            elif selected == _UPLOAD_OPTION and st.session_state.db_upload_ready:
                                table_list = ", ".join(st.session_state.db_temp_tables)
                                col_info = ""
                                if st.session_state.db_uploaded_files:
                                    for fname, df in st.session_state.db_uploaded_files.items():
                                        safe = _safe_name(fname)
                                        col_info += f"\nTable {_TEMP_TABLE_PREFIX}{safe}: {', '.join(df.columns.tolist())}"
                                elif st.session_state.db_uploaded_df is not None:
                                    col_info = f"Columns: {', '.join(st.session_state.db_uploaded_df.columns.tolist())}"
                                upload_prompt = (
                                    f"Tables: {table_list}. {col_info}\n\n"
                                    f"Question: {user_q}\n\n"
                                    f"Write a Snowflake SQL query. Wrap SQL in a ```sql block. "
                                    f"Return data with a category column and a numeric column for charting."
                                )
                                session = conn.session()
                                result = session.sql(
                                    "SELECT SNOWFLAKE.CORTEX.COMPLETE('llama3.1-70b', ?) AS resp",
                                    params=[upload_prompt],
                                ).collect()
                                answer = result[0]["RESP"] if result else "No response."
                            else:
                                answer = llm_query(conn, user_q + "\nReturn data suitable for charting.", selected)
                        except Exception as e:
                            answer = f"Error: {e}"
                    display_answer = strip_sql_blocks(answer) if not use_agent else answer
                    st.markdown(display_answer)

            # Try to extract SQL and create a card
            chart_df = None
            sql = _try_extract_sql(answer)

            if not sql and "error" not in answer.lower():
                chart_prompt = (
                    f"For: '{user_q}' — return ONLY a single SQL SELECT in a ```sql block. "
                    f"No CTEs. Return a category column and a numeric column for charting."
                )
                try:
                    if use_agent:
                        chart_resp = call_agent(chart_prompt)
                    else:
                        chart_resp = llm_query(conn, chart_prompt, selected)
                    sql = _try_extract_sql(chart_resp)
                except Exception:
                    pass

            if sql:
                try:
                    df = to_float(conn.query(sql))
                    if not df.empty and len(df.columns) >= 2:
                        chart_df = df
                except Exception:
                    pass

            st.session_state.db_messages.append({"role": "assistant", "content": answer})

            if chart_df is not None:
                title = user_q[:50]
                st.session_state.db_cards.append({
                    "title": title,
                    "df": chart_df,
                    "chart_type": "bar",
                    "sql": sql or "",
                })
            st.rerun()
