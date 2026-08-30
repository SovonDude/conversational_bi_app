import datetime
import io
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

_UPLOAD_OPTION = "Upload New Data"

# Close dashboard sidebar chat when on this tab
st.session_state.dash_chat_open = False

CHART_TYPES = {
    "Bar": {"icon": ":material/bar_chart:", "type": "bar"},
    "H-Bar": {"icon": ":material/align_horizontal_left:", "type": "barh"},
    "Line": {"icon": ":material/show_chart:", "type": "line"},
    "Area": {"icon": ":material/area_chart:", "type": "area"},
    "Donut": {"icon": ":material/donut_large:", "type": "pie"},
    "Table": {"icon": ":material/table_chart:", "type": "table"},
}

LOGISTICS_SUGGESTIONS = [
    (":material/trending_up:", ":blue[Revenue by region]", "What is the total revenue by region?"),
    (":material/schedule:", ":green[Delivery times]", "What is the average delivery time by ship mode?"),
    (":material/group:", ":orange[Market segments]", "How do market segments compare by revenue?"),
    (":material/priority_high:", ":red[Order priorities]", "Which order priorities generate the most revenue?"),
    (":material/inventory:", ":violet[Late shipments]", "How many shipments were late by ship mode?"),
    (":material/public:", ":gray[Top nations]", "What are the top 5 nations by revenue?"),
]

GENERIC_SUGGESTIONS = [
    (":material/table_chart:", ":blue[List tables]", "What tables are available and what columns do they have?"),
    (":material/bar_chart:", ":green[Summary stats]", "Give me a summary of the data with key statistics."),
    (":material/search:", ":orange[Top records]", "Show me the top 10 records by the most important metric."),
    (":material/trending_up:", ":red[Trends]", "Are there any trends or patterns in this data?"),
    (":material/pie_chart:", ":violet[Breakdown]", "Break down the data by the main category column."),
    (":material/analytics:", ":gray[Comparison]", "Compare the top 5 categories by their numeric values."),
]

UPLOAD_SUGGESTIONS = [
    (":material/upload:", ":blue[Upload first]", "Upload a file above to get started with analysis."),
]


def _get_suggestions():
    src = st.session_state.b_data_source
    if src == _UPLOAD_OPTION:
        return UPLOAD_SUGGESTIONS
    if src == "LOGISTICS_DATA":
        return LOGISTICS_SUGGESTIONS
    return GENERIC_SUGGESTIONS

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
if "b_messages" not in st.session_state:
    st.session_state.b_messages = []
if "b_uploaded_df" not in st.session_state:
    st.session_state.b_uploaded_df = None
if "b_uploaded_files" not in st.session_state:
    st.session_state.b_uploaded_files = {}
if "b_upload_zip_name" not in st.session_state:
    st.session_state.b_upload_zip_name = ""
if "b_upload_ready" not in st.session_state:
    st.session_state.b_upload_ready = False
if "b_temp_tables" not in st.session_state:
    st.session_state.b_temp_tables = []
if "b_saved_permanently" not in st.session_state:
    st.session_state.b_saved_permanently = set()


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


# ---------------------------------------------------------------------------
# PDF writer for chat export
# ---------------------------------------------------------------------------
class _ChatPDF(BasePDF):
    """Portrait Letter PDF for chat conversation export."""

    def __init__(self):
        super().__init__(width=612, height=792, margin=40)

    def title_bar(self, title, subtitle):
        self.rect(0, self._H - 50, self._W, 50, *BRAND)
        self.text(self._M, self._H - 30, title, size=16, bold=True, r=1, g=1, b=1)
        self.text(self._M, self._H - 44, subtitle, size=8, r=0.85, g=0.9, b=1)
        self._y = self._H - 60

    def _wrap_text(self, text, max_chars=80):
        lines = []
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                lines.append("")
                continue
            words = paragraph.split()
            current = ""
            for word in words:
                if len(current) + len(word) + 1 <= max_chars:
                    current = f"{current} {word}" if current else word
                else:
                    if current:
                        lines.append(current)
                    current = word
            if current:
                lines.append(current)
        return lines

    def _write_wrapped(self, x, text, size=9, bold=False, r=0.2, g=0.2, b=0.2, line_h=13):
        lines = self._wrap_text(text, max_chars=int((self._W - 2 * self._M) / (size * 0.5)))
        for ln in lines:
            self._check_space(line_h)
            self.text(x, self._y, ln, size=size, bold=bold, r=r, g=g, b=b)
            self._y -= line_h

    def bar_chart(self, df, title="Chart", height=160):
        num_cols = [c for c in df.columns if df[c].dtype in ("float64", "int64", "float32")]
        str_cols = [c for c in df.columns if df[c].dtype == "object"]
        if not num_cols or not str_cols:
            return
        y_col = num_cols[0]
        x_col = str_cols[0]
        labels = [str(v)[:18] for v in df[x_col].tolist()[:12]]
        values = df[y_col].tolist()[:12]
        if not values:
            return
        is_dollar = any(k in y_col.upper() for k in ("REVENUE", "PRICE", "COST", "BALANCE", "VALUE", "TOTAL"))
        self._check_space(height + 30)
        chart_w = self._W - 2 * self._M
        x = self._M
        top_y = self._y
        self.section_label(x, top_y, chart_w, title[:60])
        self.border(x, top_y - height, chart_w, height)
        bar_top = top_y - 20
        max_val = max(values) if max(values) > 0 else 1
        label_w = 90
        val_w = 55
        chart_left = x + label_w
        bar_area_w = chart_w - label_w - val_w
        n = len(labels)
        avail_h = height - 26
        bar_h = min(avail_h / n - 2, 14)
        gap = max((avail_h - n * bar_h) / max(n, 1), 1)
        for i, (lab, val) in enumerate(zip(labels, values)):
            by = bar_top - (bar_h + gap) * (i + 1) + gap
            bw = (val / max_val) * bar_area_w if max_val else 0
            c = COLORS[i % len(COLORS)]
            self.rect(chart_left, by, max(bw, 1), bar_h, *c)
            self.text(x + 2, by + bar_h / 2 - 3, lab, size=6)
            display = fmt_number(val, prefix="$") if is_dollar else fmt_number(val)
            self.text(chart_left + bw + 3, by + bar_h / 2 - 3, display, size=6, bold=True)
            self.line(chart_left, by, chart_left + bar_area_w, by, w=0.2)
        self._y = top_y - height - 8


def _build_chat_pdf():
    pdf = _ChatPDF()
    msgs = st.session_state.b_messages
    chart_count = sum(1 for m in msgs if m.get("chart_df") is not None and not m["chart_df"].empty)
    pdf.title_bar(
        "BI Assistant Data Analysis",
        f"Generated: {datetime.datetime.now().strftime('%B %d, %Y %H:%M')}  |  "
        f"{len(msgs)} messages  |  {chart_count} charts",
    )
    pdf._y -= 5
    for msg in msgs:
        is_user = msg["role"] == "user"
        role_label = "You" if is_user else "BI Assistant"
        pdf._check_space(30)
        pdf.rect(pdf._M, pdf._y - 1, pdf._W - 2 * pdf._M, 1, *BRAND)
        pdf._y -= 8
        pdf.text(pdf._M, pdf._y, role_label, size=10, bold=True,
                 r=0.25 if is_user else BRAND[0],
                 g=0.25 if is_user else BRAND[1],
                 b=0.25 if is_user else BRAND[2])
        pdf._y -= 16
        content = msg["content"]
        if not is_user:
            content, _ = extract_followups(content)
        pdf._write_wrapped(pdf._M + 5, content, size=8.5, line_h=12)
        pdf._y -= 4
        if msg.get("chart_df") is not None and not msg["chart_df"].empty:
            pdf.bar_chart(msg["chart_df"], title=msg.get("question", "Chart")[:60], height=150)
        pdf._y -= 8
    return pdf.build()


def _render_chart(df, chart_type="bar", height=280):
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
    _dark = st.session_state.get("dark_mode", False)
    cfg = vega_config(_dark)

    if chart_type == "pie":
        st.vega_lite_chart(chart_df, {"height": height,
            "mark": {"type": "arc", "innerRadius": 50, "outerRadius": height // 2 - 20},
            "encoding": {"theta": {"field": y_col, "type": "quantitative", "stack": True},
                         "color": {"field": x_col, "type": "nominal"}, "tooltip": tooltips}},
            use_container_width=True)
    elif chart_type == "barh":
        st.vega_lite_chart(chart_df, {"height": max(len(df) * 26, 180),
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


# ---------------------------------------------------------------------------
# Export / Email
# ---------------------------------------------------------------------------
EMAIL_INTEGRATION = "LOGISTICS_EMAIL_INT"


def _send_email(recipients):
    # Collect chart data from messages
    charts = []
    for msg in st.session_state.b_messages:
        if msg.get("chart_df") is not None and not msg["chart_df"].empty:
            charts.append({"title": msg.get("question", "Chart")[:50], "df": msg["chart_df"]})
    if not charts:
        return
    tables_html = ""
    for card in charts:
        df = card["df"]
        headers = "".join(
            f'<th style="padding:4px 8px;background:#418CF0;color:#fff;font-size:10px;'
            f'text-align:left;">{c.replace("_", " ").title()}</th>' for c in df.columns)
        rows = ""
        for ri in range(min(len(df), 10)):
            bg = "#f5f7fa" if ri % 2 else "#fff"
            cells = "".join(
                f'<td style="padding:3px 8px;font-size:10px;background:{bg};">{str(df.iloc[ri, ci])[:18]}</td>'
                for ci in range(len(df.columns)))
            rows += f"<tr>{cells}</tr>"
        tables_html += (
            f'<div style="margin-top:12px;">'
            f'<div style="background:#418CF0;color:#fff;padding:5px 10px;font-weight:bold;'
            f'font-size:11px;border-radius:3px 3px 0 0;">{card["title"]}</div>'
            f'<table style="width:100%;border-collapse:collapse;border:1px solid #e0e0e0;">'
            f'<tr>{headers}</tr>{rows}</table></div>')
    html = f"""
    <div style="font-family:Helvetica,Arial,sans-serif;max-width:650px;margin:0 auto;">
        <div style="background:#418CF0;padding:14px 18px;border-radius:6px 6px 0 0;">
            <h2 style="color:#fff;margin:0;font-size:17px;">BI Assistant Data Analysis</h2>
            <p style="color:#dce8fc;margin:3px 0 0;font-size:10px;">
                {datetime.datetime.now().strftime('%B %d, %Y %H:%M')} | {len(charts)} charts</p>
        </div>
        <div style="padding:14px;background:#fff;border:1px solid #e0e0e0;">{tables_html}</div>
        <div style="padding:6px;background:#f5f5f5;font-size:9px;color:#999;text-align:center;
                     border-radius:0 0 6px 6px;border:1px solid #e0e0e0;border-top:0;">
            Sent from Logistics BI Assistant</div>
    </div>"""
    session = conn.session()
    session.sql("CALL SYSTEM$SEND_EMAIL(?, ?, ?, ?, 'text/html')",
                params=[EMAIL_INTEGRATION, recipients, "Custom Dashboard", html]).collect()


def _write_to_snowflake(df, schema_name, table_name):
    safe_schema = _safe_name(schema_name) or "USER_UPLOADS"
    safe_table = _safe_name(table_name) or "UPLOADED_DATA"
    fqn = f"CONVERSATIONAL_BI_ASSISTANT.{safe_schema}.{safe_table}"
    session = conn.session()
    session.sql(f"CREATE SCHEMA IF NOT EXISTS CONVERSATIONAL_BI_ASSISTANT.{safe_schema}").collect()
    snowpark_df = session.create_dataframe(df)
    snowpark_df.write.mode("overwrite").save_as_table(fqn)
    _list_schemas.clear()
    return fqn


_TEMP_TABLE_PREFIX = "CONVERSATIONAL_BI_ASSISTANT.USER_UPLOADS.__TEMP_"


def _write_temp_table(df, name="UPLOADED_DATA"):
    """Write df to a session-scoped temporary table for immediate analysis."""
    safe = _safe_name(name)
    fqn = f"{_TEMP_TABLE_PREFIX}{safe}"
    session = conn.session()
    session.sql("CREATE SCHEMA IF NOT EXISTS CONVERSATIONAL_BI_ASSISTANT.USER_UPLOADS").collect()
    snowpark_df = session.create_dataframe(df)
    snowpark_df.write.mode("overwrite").save_as_table(fqn, table_type="temporary")
    return fqn


def _load_upload_to_temp():
    """Auto-load uploaded data into temp tables and set upload_ready."""
    temp_tables = []
    if st.session_state.b_uploaded_files:
        for fname, df in st.session_state.b_uploaded_files.items():
            fqn = _write_temp_table(df, fname)
            temp_tables.append(fqn)
    elif st.session_state.b_uploaded_df is not None:
        fqn = _write_temp_table(st.session_state.b_uploaded_df)
        temp_tables.append(fqn)
    if temp_tables:
        st.session_state.b_temp_tables = temp_tables
        st.session_state.b_upload_ready = True


if "b_data_source" not in st.session_state:
    st.session_state.b_data_source = "LOGISTICS_DATA"

@st.cache_data(ttl=60)
def _list_schemas():
    df = conn.query(
        "SELECT SCHEMA_NAME FROM CONVERSATIONAL_BI_ASSISTANT.INFORMATION_SCHEMA.SCHEMATA "
        "WHERE SCHEMA_NAME NOT IN ('INFORMATION_SCHEMA', 'PUBLIC', 'SEMANTIC_MODELS', 'USER_UPLOADS') "
        "ORDER BY SCHEMA_NAME"
    )
    return df["SCHEMA_NAME"].tolist()

# ---------------------------------------------------------------------------
# Title + Action buttons
# ---------------------------------------------------------------------------
with st.container(horizontal=True):
    st.subheader(":material/smart_toy: BI Assistant Data Analysis")
    with st.popover(f":material/database: {st.session_state.b_data_source}"):
        schemas = _list_schemas()
        for schema in schemas:
            if st.button(schema, use_container_width=True, key=f"src_{schema}",
                         type="primary" if st.session_state.b_data_source == schema else "secondary"):
                st.session_state.b_data_source = schema
                st.session_state.b_uploaded_df = None
                st.session_state.b_uploaded_files = {}
                st.session_state.b_upload_zip_name = ""
                st.rerun()
        st.divider()
        if st.button(f":material/upload: {_UPLOAD_OPTION}", use_container_width=True, key="src_upload",
                     type="primary" if st.session_state.b_data_source == _UPLOAD_OPTION else "secondary"):
            st.session_state.b_data_source = _UPLOAD_OPTION
            st.session_state.b_uploaded_df = None
            st.session_state.b_uploaded_files = {}
            st.session_state.b_upload_zip_name = ""
            st.session_state.b_upload_ready = False
            st.session_state.b_temp_tables = []
            st.session_state.b_saved_permanently = set()
            st.rerun()
    if st.session_state.b_messages:
        if st.button("Clear", icon=":material/delete:", help="Clear chat & dashboard", key="b_clear"):
            st.session_state.b_messages = []
            st.rerun()
    with st.popover("Export", icon=":material/download:"):
        export_dfs = {}
        for msg in st.session_state.b_messages:
            if msg.get("chart_df") is not None and not msg["chart_df"].empty:
                title = msg.get("question", "Chart")[:31]
                export_dfs[title] = msg["chart_df"]
        if export_dfs and st.session_state.b_data_source != _UPLOAD_OPTION:
            if len(export_dfs) == 1:
                _, df = next(iter(export_dfs.items()))
                export_data = df.to_csv(index=False).encode("utf-8")
                st.download_button("Export CSV", data=export_data,
                                   file_name="analysis_export.csv", mime="text/csv",
                                   icon=":material/download:", use_container_width=True)
            else:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for name, df in export_dfs.items():
                        zf.writestr(name.lower().replace(" ", "_")[:40] + ".csv",
                                    df.to_csv(index=False))
                st.download_button("Export ZIP", data=buf.getvalue(),
                                   file_name="analysis_export.zip", mime="application/zip",
                                   icon=":material/folder_zip:", use_container_width=True)
        if st.session_state.b_messages:
            pdf_bytes = _build_chat_pdf()
            st.download_button("Export PDF", data=pdf_bytes,
                               file_name="analysis_conversation.pdf", mime="application/pdf",
                               icon=":material/picture_as_pdf:", use_container_width=True)
            st.divider()
            st.caption("Email Dashboard")
            email_to = st.text_input("Recipient email(s)", placeholder="user@example.com",
                                     key="b_email", label_visibility="collapsed")
            if st.button("Send Email", icon=":material/send:", use_container_width=True,
                         disabled=not email_to):
                with st.spinner("Sending..."):
                    try:
                        _send_email(email_to.strip())
                        st.success("Email sent!")
                    except Exception as e:
                        st.error(f"Failed: {e}")
        else:
            st.caption("Add charts to export.")

st.caption("Ask questions to get insights and generate charts. Use the data selector to choose a schema or upload your own data.")

# Disable page scroll — only chat area scrolls
st.markdown("""
<style>
/* Allow page to scroll when upload panel is visible */
section.stMain, [data-testid="stMain"] {
    overflow-y: auto !important;
    height: 100vh !important;
}
[data-testid="stMainBlockContainer"] {
    overflow-x: hidden !important;
    padding-bottom: 2rem !important;
}
/* Smaller text inside chat area */
[data-testid="stVerticalBlockBorderWrapper"] p,
[data-testid="stVerticalBlockBorderWrapper"] li,
[data-testid="stVerticalBlockBorderWrapper"] span,
[data-testid="stVerticalBlockBorderWrapper"] .stMarkdown,
[data-testid="stVerticalBlockBorderWrapper"] .stChatMessage,
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li,
[data-testid="stChatMessage"] span,
.stChatMessage p, .stChatMessage li, .stChatMessage span {
    font-size: 13px !important;
    line-height: 1.4 !important;
}
</style>
""", unsafe_allow_html=True)

# Upload panel (shown automatically when Upload Data is selected)
if st.session_state.b_data_source == _UPLOAD_OPTION:
    with st.container(border=True):
        uploaded_file = st.file_uploader("Upload CSV, Excel, or ZIP", type=["csv", "xlsx", "xls", "zip"],
                                          label_visibility="collapsed", key="b_file_uploader")
        if uploaded_file is not None:
            try:
                if uploaded_file.name.endswith(".zip"):
                    zip_stem = re.sub(r"\.[^.]+$", "", uploaded_file.name)
                    st.session_state.b_upload_zip_name = zip_stem
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
                            st.session_state.b_uploaded_files = files_dict
                            st.session_state.b_uploaded_df = None
                elif uploaded_file.name.endswith(".csv"):
                    st.session_state.b_uploaded_df = pd.read_csv(uploaded_file)
                    st.session_state.b_uploaded_files = {}
                else:
                    st.session_state.b_uploaded_df = pd.read_excel(uploaded_file)
                    st.session_state.b_uploaded_files = {}
            except Exception as e:
                st.error(f"Failed to read file: {e}")

        # Auto-load to temp tables for immediate analysis
        has_data = bool(st.session_state.b_uploaded_files) or st.session_state.b_uploaded_df is not None
        if has_data and not st.session_state.b_upload_ready:
            with st.spinner("Loading data for analysis..."):
                _load_upload_to_temp()
            st.success("Data loaded — you can start asking questions below.")

        # Multi-file view (ZIP)
        if st.session_state.b_uploaded_files:
            zip_name = st.session_state.b_upload_zip_name
            file_names = list(st.session_state.b_uploaded_files.keys())
            df_map = st.session_state.b_uploaded_files

            tabs = st.tabs(file_names)
            for idx, (tab, fname) in enumerate(zip(tabs, file_names)):
                is_saved = fname in st.session_state.b_saved_permanently
                with tab:
                    if is_saved:
                        st.success(f"Saved permanently")
                    else:
                        btn_cols = st.columns([1, 1, 1])
                        # Save button
                        with btn_cols[0]:
                            if st.button("Save", icon=":material/cloud_upload:", key=f"b_save_{idx}",
                                         use_container_width=True):
                                schema_val = _safe_name(zip_name) if zip_name else "USER_UPLOADS"
                                table_val = _safe_name(fname)
                                try:
                                    fqn = _write_to_snowflake(df_map[fname], schema_val, table_val)
                                    st.session_state.b_saved_permanently.add(fname)
                                    st.success(f"Saved to `{fqn}`.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed: {e}")
                        # Delete button
                        with btn_cols[1]:
                            if st.button("Delete", icon=":material/delete:", key=f"b_del_{idx}",
                                         use_container_width=True):
                                del st.session_state.b_uploaded_files[fname]
                                st.session_state.b_temp_tables = [
                                    t for t in st.session_state.b_temp_tables
                                    if not t.endswith(f"__{re.sub(r'[^A-Za-z0-9_]', '_', fname).upper()}")
                                ]
                                if not st.session_state.b_uploaded_files:
                                    st.session_state.b_upload_ready = False
                                st.rerun()
                    st.dataframe(df_map[fname].head(50), hide_index=True, use_container_width=True)

            # Save All (only if any unsaved)
            unsaved = [f for f in file_names if f not in st.session_state.b_saved_permanently]
            if unsaved:
                if st.button(f"Save All {len(unsaved)} Unsaved Tables",
                             icon=":material/cloud_upload:", key="b_push_all", use_container_width=True):
                    schema_all = _safe_name(zip_name) if zip_name else "USER_UPLOADS"
                    saved = []
                    with st.spinner(f"Saving {len(unsaved)} tables..."):
                        for fname in unsaved:
                            tbl = _safe_name(fname)
                            try:
                                fqn = _write_to_snowflake(df_map[fname], schema_all, tbl)
                                st.session_state.b_saved_permanently.add(fname)
                                saved.append(fqn)
                            except Exception as e:
                                st.error(f"Failed to save `{tbl}`: {e}")
                    if saved:
                        st.success(f"Saved {len(saved)} tables.")
                        st.rerun()

        # Single-file view (CSV / Excel)
        elif st.session_state.b_uploaded_df is not None:
            single_saved = "__single__" in st.session_state.b_saved_permanently
            if not single_saved:
                btn_cols = st.columns([1, 1, 2])
                with btn_cols[0]:
                    if st.button("Save", icon=":material/cloud_upload:", key="b_push",
                                 use_container_width=True):
                        try:
                            fqn = _write_to_snowflake(st.session_state.b_uploaded_df, "USER_UPLOADS", "UPLOADED_DATA")
                            st.session_state.b_saved_permanently.add("__single__")
                            st.success(f"Saved to `{fqn}`.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed: {e}")
                with btn_cols[1]:
                    if st.button("Delete", icon=":material/delete:", key="b_del_single",
                                 use_container_width=True):
                        st.session_state.b_uploaded_df = None
                        st.session_state.b_upload_ready = False
                        st.session_state.b_temp_tables = []
                        st.rerun()
            else:
                st.success("Saved permanently")
            st.dataframe(st.session_state.b_uploaded_df.head(50), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# Chat (scrollable)
# ---------------------------------------------------------------------------
chat_container = st.container(height=500)
last_followups = []
with chat_container:
    for i, msg in enumerate(st.session_state.b_messages):
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant":
                display_text, followups = extract_followups(msg["content"])
                st.markdown(display_text)
                if i == len(st.session_state.b_messages) - 1:
                    last_followups = followups
            else:
                st.markdown(msg["content"])
            # Show chart for assistant messages with data
            if msg.get("chart_df") is not None:
                ct = msg.get("chart_type", "bar")
                _render_chart(msg["chart_df"], chart_type=ct)
                current_name = next(
                    (name for name, info in CHART_TYPES.items() if info["type"] == ct), "Bar")
                sw_col, _ = st.columns([2, 7])
                with sw_col:
                    new_type = st.selectbox(
                        "Switch chart", list(CHART_TYPES.keys()),
                        index=list(CHART_TYPES.keys()).index(current_name),
                        key=f"bsw_{i}",
                    )
                if CHART_TYPES[new_type]["type"] != ct:
                    st.session_state.b_messages[i]["chart_type"] = CHART_TYPES[new_type]["type"]
                    st.rerun()

    if not st.session_state.b_messages:
        suggestions = _get_suggestions()
        for row_start in range(0, len(suggestions), 3):
            row_items = suggestions[row_start:row_start + 3]
            cols = st.columns(len(row_items))
            for ci, (col, (icon, label, question)) in enumerate(zip(cols, row_items)):
                with col:
                    if st.button(label, icon=icon, use_container_width=True, key=f"bsug_{row_start+ci}"):
                        st.session_state.b_messages.append(
                            {"role": "user", "content": question})
                        st.rerun()

    # Follow-up buttons (inside scrollable chat)
    if last_followups:
        for i, q in enumerate(last_followups):
            if st.button(q, icon=":material/chat:", key=f"bfup_{i}_{q[:20]}",
                         use_container_width=True):
                st.session_state.b_messages.append(
                    {"role": "user", "content": q})
                st.rerun()

# Chat input
if prompt := st.chat_input("Ask about your data...", key="b_chat_input"):
    st.session_state.b_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

# Generate response + auto-chart
if st.session_state.b_messages and st.session_state.b_messages[-1]["role"] == "user":
    user_q = st.session_state.b_messages[-1]["content"]
    selected_schema = st.session_state.b_data_source
    use_agent = selected_schema == LOGISTICS_SCHEMA

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                if use_agent:
                    answer = call_agent(user_q)
                elif selected_schema == _UPLOAD_OPTION and st.session_state.b_upload_ready:
                    # Build upload context for LLM
                    temp_tables = st.session_state.b_temp_tables
                    col_info = ""
                    if st.session_state.b_uploaded_files:
                        for fname, df in st.session_state.b_uploaded_files.items():
                            safe = _safe_name(fname)
                            col_info += f"\nTable {_TEMP_TABLE_PREFIX}{safe}: {', '.join(df.columns.tolist())}"
                    elif st.session_state.b_uploaded_df is not None:
                        col_info = f"Columns: {', '.join(st.session_state.b_uploaded_df.columns.tolist())}"
                    upload_prompt = (
                        f"Tables: {', '.join(temp_tables)}. {col_info}\n\n"
                        f"Question: {user_q}\n\n"
                        f"Write a Snowflake SQL query using the exact table and column names above. "
                        f"Wrap SQL in a ```sql block. Provide a brief explanation."
                    )
                    session = conn.session()
                    result = session.sql(
                        "SELECT SNOWFLAKE.CORTEX.COMPLETE('llama3.1-70b', ?) AS resp",
                        params=[upload_prompt],
                    ).collect()
                    answer = result[0]["RESP"] if result else "No response."
                else:
                    answer = llm_query(conn, user_q, selected_schema)
            except Exception as e:
                answer = f"Error: {e}"
        display_answer = strip_sql_blocks(answer) if not use_agent else answer
        st.markdown(display_answer)

    # Auto-generate chart if the response contains SQL
    chart_df = None
    sql = _try_extract_sql(answer)

    # If no SQL in response, ask for chart-ready SQL
    if not sql and "error" not in answer.lower():
        chart_prompt = (
            f"For this question: '{user_q}' -- "
            f"return ONLY a single simple SQL SELECT query in a ```sql code block. "
            f"Do NOT use CTEs. "
            f"The query should return data suitable for a chart with a category column and a numeric column. "
            f"No explanation, just the SQL."
        )
        try:
            if use_agent:
                chart_resp = call_agent(chart_prompt)
            else:
                chart_resp = llm_query(conn, chart_prompt, selected_schema)
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

    st.session_state.b_messages.append({
        "role": "assistant",
        "content": display_answer,
        "chart_df": chart_df,
        "chart_type": "bar",
        "question": user_q,
    })
    st.rerun()
