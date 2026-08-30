# Dashboard v2
import datetime
import io
import json
import math
import re
import zipfile

import pydeck as pdk
import streamlit as st

from app_pages._shared import (
    hex_to_rgb, BRAND, COLORS, BasePDF, vega_config, extract_followups, strip_sql_blocks,
    LOGISTICS_SCHEMA, llm_query,
)

# Access shared helpers from main module
s = st.session_state.shared
conn = st.session_state.conn
fmt_number = s["fmt_number"]
to_float = s["to_float"]
_date_clause = s["_date_clause"]
_in_clause = s["_in_clause"]
call_agent = s["call_agent"]
ALL_REGIONS = s["ALL_REGIONS"]
ALL_SHIP_MODES = s["ALL_SHIP_MODES"]
DATE_MIN = s["DATE_MIN"]
DATE_MAX = s["DATE_MAX"]
BMK_EXPR = s["BMK_EXPR"]
BMK_EXPR_DOLLAR = s["BMK_EXPR_DOLLAR"]
CHART_H = 300
_is_dark = st.session_state.get("dark_mode", False)
VEGA_CONFIG = vega_config(_is_dark)

# ---------------------------------------------------------------------------
# Hide all default sidebar toggle arrows globally
# ---------------------------------------------------------------------------
_HIDE_SIDEBAR_CSS = """
<style>
button[data-testid*="idebarCol"],
button[data-testid*="ollapsed"],
div[data-testid*="ollapsed"],
[data-testid*="ollapsedControl"],
button[aria-label="Close sidebar"],
button[aria-label="Collapse sidebar"],
button[aria-label="Expand sidebar"],
button[aria-label="open sidebar"] {
    display: none !important;
    visibility: hidden !important;
    width: 0 !important;
    height: 0 !important;
    position: absolute !important;
    left: -9999px !important;
}
/* BI Assistant button — gradient + pulse (scoped to main content, not nav) */
@keyframes pulse-glow {
    0%, 100% { box-shadow: 0 0 8px rgba(168,85,247,0.5); }
    50% { box-shadow: 0 0 20px rgba(236,72,153,0.7); }
}
[data-testid="stMainBlockContainer"] [data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, #7c3aed, #ec4899) !important;
    color: #ffffff !important;
    border: none !important;
    animation: pulse-glow 2.5s ease-in-out infinite;
    border-radius: 8px !important;
    font-weight: 600 !important;
}
[data-testid="stMainBlockContainer"] [data-testid="stBaseButton-primary"] p,
[data-testid="stMainBlockContainer"] [data-testid="stBaseButton-primary"] span {
    color: #ffffff !important;
}
[data-testid="stMainBlockContainer"] [data-testid="stBaseButton-primary"]:hover {
    background: linear-gradient(135deg, #6d28d9, #db2777) !important;
    box-shadow: 0 0 24px rgba(236,72,153,0.8) !important;
}
</style>
"""
st.markdown(_HIDE_SIDEBAR_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Ask AI — sidebar chat
# ---------------------------------------------------------------------------
if "dash_messages" not in st.session_state:
    st.session_state.dash_messages = []
if "dash_chat_open" not in st.session_state:
    st.session_state.dash_chat_open = False


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600)
def load_kpi_data(date_range, regions, ship_modes):
    d1, d2 = date_range
    result = to_float(conn.query(
        f"SELECT "
        f"(SELECT COUNT(*) FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"  WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)}) AS order_cnt, "
        f"(SELECT COALESCE(SUM(O.O_TOTALPRICE),0) FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"  WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)}) AS rev, "
        f"(SELECT COALESCE(AVG(O.O_TOTALPRICE),0) FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"  WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)}) AS aov, "
        f"(SELECT COUNT(*) FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.LINEITEM "
        f"  WHERE {_date_clause('L_SHIPDATE', d1, d2)} AND {_in_clause('L_SHIPMODE', ship_modes)}) AS ship_cnt, "
        f"(SELECT COALESCE(AVG(DATEDIFF('day', L_SHIPDATE, L_RECEIPTDATE)),0) "
        f"  FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.LINEITEM "
        f"  WHERE {_date_clause('L_SHIPDATE', d1, d2)} AND {_in_clause('L_SHIPMODE', ship_modes)}) AS avg_days, "
        f"(SELECT COUNT(DISTINCT C.C_CUSTKEY) FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"  JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"  WHERE {_in_clause('R.R_NAME', regions)}) AS cust_cnt"
    ))
    r = result.iloc[0]
    orders = {"CNT": r["ORDER_CNT"], "REV": r["REV"], "AOV": r["AOV"]}
    shipments = {"CNT": r["SHIP_CNT"], "AVG_DAYS": r["AVG_DAYS"]}
    customers = {"CNT": r["CUST_CNT"]}
    return orders, shipments, customers


@st.cache_data(ttl=600)
def load_revenue_by_region(date_range, regions):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT R.R_NAME AS region, SUM(O.O_TOTALPRICE) AS revenue "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)} "
        f"GROUP BY R.R_NAME ORDER BY revenue DESC"
    ))


@st.cache_data(ttl=600)
def load_shipmode_stats(date_range, ship_modes):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT L_SHIPMODE AS ship_mode, COUNT(*) AS shipments, "
        f"AVG(DATEDIFF('day', L_SHIPDATE, L_RECEIPTDATE)) AS avg_delivery_days "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.LINEITEM "
        f"WHERE {_date_clause('L_SHIPDATE', d1, d2)} AND {_in_clause('L_SHIPMODE', ship_modes)} "
        f"GROUP BY L_SHIPMODE ORDER BY shipments DESC"
    ))


@st.cache_data(ttl=600)
def load_segment_stats(date_range, regions):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT C.C_MKTSEGMENT AS segment, COUNT(DISTINCT O.O_ORDERKEY) AS orders, "
        f"SUM(O.O_TOTALPRICE) AS revenue "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)} "
        f"GROUP BY C.C_MKTSEGMENT ORDER BY revenue DESC"
    ))


@st.cache_data(ttl=600)
def load_monthly_revenue(date_range, regions, ship_modes):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT DATE_TRUNC('MONTH', O.O_ORDERDATE)::DATE AS month, "
        f"SUM(O.O_TOTALPRICE) AS revenue, COUNT(DISTINCT O.O_ORDERKEY) AS orders "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.LINEITEM L ON O.O_ORDERKEY = L.L_ORDERKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)} "
        f"AND {_in_clause('L.L_SHIPMODE', ship_modes)} "
        f"GROUP BY month ORDER BY month"
    ))


@st.cache_data(ttl=600)
def load_order_status(date_range, regions):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT CASE O.O_ORDERSTATUS WHEN 'F' THEN 'Fulfilled' WHEN 'O' THEN 'Open' "
        f"WHEN 'P' THEN 'Partial' ELSE O.O_ORDERSTATUS END AS status, "
        f"COUNT(*) AS orders "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)} "
        f"GROUP BY O.O_ORDERSTATUS ORDER BY orders DESC"
    ))


@st.cache_data(ttl=600)
def load_ontime_late(date_range, ship_modes):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT L_SHIPMODE AS ship_mode, "
        f"COUNT_IF(L_RECEIPTDATE <= L_COMMITDATE) AS on_time, "
        f"COUNT_IF(L_RECEIPTDATE > L_COMMITDATE) AS late "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.LINEITEM "
        f"WHERE {_date_clause('L_SHIPDATE', d1, d2)} AND {_in_clause('L_SHIPMODE', ship_modes)} "
        f"GROUP BY L_SHIPMODE ORDER BY L_SHIPMODE"
    ))


@st.cache_data(ttl=600)
def load_revenue_by_priority(date_range, regions):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT O.O_ORDERPRIORITY AS priority, SUM(O.O_TOTALPRICE) AS revenue, COUNT(*) AS orders "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)} "
        f"GROUP BY O.O_ORDERPRIORITY ORDER BY revenue DESC"
    ))


@st.cache_data(ttl=600)
def load_top_nations(date_range, regions):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT N.N_NAME AS nation, SUM(O.O_TOTALPRICE) AS revenue, COUNT(DISTINCT O.O_ORDERKEY) AS orders "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.ORDERS O "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.CUSTOMER C ON O.O_CUSTKEY = C.C_CUSTKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.NATION N ON C.C_NATIONKEY = N.N_NATIONKEY "
        f"JOIN CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.REGION R ON N.N_REGIONKEY = R.R_REGIONKEY "
        f"WHERE {_date_clause('O.O_ORDERDATE', d1, d2)} AND {_in_clause('R.R_NAME', regions)} "
        f"GROUP BY N.N_NAME ORDER BY revenue DESC"
    ))


@st.cache_data(ttl=600)
def load_shipping_volume(date_range, ship_modes):
    d1, d2 = date_range
    return to_float(conn.query(
        f"SELECT DATE_TRUNC('MONTH', L_SHIPDATE)::DATE AS month, COUNT(*) AS shipments "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.LOGISTICS_DATA.LINEITEM "
        f"WHERE {_date_clause('L_SHIPDATE', d1, d2)} AND {_in_clause('L_SHIPMODE', ship_modes)} "
        f"GROUP BY month ORDER BY month"
    ))


# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600)
def build_data_export(_date, _regions, _modes):
    datasets = {
        "Revenue by Region": load_revenue_by_region(_date, _regions),
        "Shipments by Mode": load_shipmode_stats(_date, _modes),
        "Revenue by Segment": load_segment_stats(_date, _regions),
        "Monthly Revenue Trend": load_monthly_revenue(_date, _regions, _modes),
        "Order Status": load_order_status(_date, _regions),
        "On-Time vs Late": load_ontime_late(_date, _modes),
        "Revenue by Priority": load_revenue_by_priority(_date, _regions),
        "Nations by Revenue": load_top_nations(_date, _regions),
        "Shipping Volume": load_shipping_volume(_date, _modes),
    }
    datasets = {k: v for k, v in datasets.items() if not v.empty}

    if len(datasets) <= 1:
        name, df = next(iter(datasets.items())) if datasets else ("empty", __import__("pandas").DataFrame())
        return df.to_csv(index=False).encode("utf-8"), \
            f"logistics_{name.lower().replace(' ', '_')}.csv", "text/csv"
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, df in datasets.items():
                fname = name.lower().replace(" ", "_") + ".csv"
                zf.writestr(fname, df.to_csv(index=False))
        return buf.getvalue(), "logistics_dashboard_export.zip", "application/zip"


EMAIL_INTEGRATION = "LOGISTICS_EMAIL_INT"


def send_dashboard_email(recipients, _date, _regions, _modes):
    session = conn.session()
    orders_kpi, shipments_kpi, customers_kpi = load_kpi_data(_date, _regions, _modes)
    kpis = [
        ("Total Revenue", fmt_number(orders_kpi["REV"], prefix="$")),
        ("Total Orders", fmt_number(orders_kpi["CNT"])),
        ("Avg Order Value", fmt_number(orders_kpi["AOV"], prefix="$")),
        ("Total Shipments", fmt_number(shipments_kpi["CNT"])),
        ("Avg Delivery Days", f"{shipments_kpi['AVG_DAYS']:.1f}"),
        ("Active Customers", fmt_number(customers_kpi["CNT"])),
    ]
    kpi_cells = ""
    for label, value in kpis:
        kpi_cells += (
            f'<td style="text-align:center;padding:10px 6px;background:#f7f9fc;'
            f'border-top:3px solid #418CF0;min-width:80px;">'
            f'<div style="font-size:10px;color:#888;">{label}</div>'
            f'<div style="font-size:18px;font-weight:bold;color:#333;">{value}</div></td>'
        )

    datasets = {
        "Revenue by Region": load_revenue_by_region(_date, _regions),
        "Revenue by Segment": load_segment_stats(_date, _regions),
        "Order Status": load_order_status(_date, _regions),
        "Revenue by Priority": load_revenue_by_priority(_date, _regions),
        "Nations by Revenue": load_top_nations(_date, _regions),
        "Shipments by Mode": load_shipmode_stats(_date, _modes),
    }
    tables_html = ""
    for name, df in datasets.items():
        if df.empty:
            continue
        headers = "".join(
            f'<th style="padding:5px 8px;background:#418CF0;color:#fff;font-size:10px;'
            f'text-align:left;">{c.replace("_", " ").title()}</th>' for c in df.columns)
        rows = ""
        for ri in range(min(len(df), 10)):
            bg = "#f9fafe" if ri % 2 else "#fff"
            cells = ""
            for ci, c in enumerate(df.columns):
                val = df.iloc[ri, ci]
                if isinstance(val, float):
                    is_d = any(k in c.upper() for k in ("REVENUE", "PRICE", "COST", "VALUE", "TOTAL"))
                    display = fmt_number(val, prefix="$") if is_d else fmt_number(val)
                else:
                    display = str(val)
                cells += f'<td style="padding:4px 8px;font-size:10px;background:{bg};">{display}</td>'
            rows += f"<tr>{cells}</tr>"
        tables_html += (
            f'<div style="margin-top:14px;">'
            f'<div style="background:#418CF0;color:#fff;padding:5px 10px;font-weight:bold;'
            f'font-size:11px;border-radius:3px 3px 0 0;">{name}</div>'
            f'<table style="width:100%;border-collapse:collapse;border:1px solid #e0e0e0;">'
            f'<tr>{headers}</tr>{rows}</table></div>'
        )

    html = f"""
    <div style="font-family:Helvetica,Arial,sans-serif;max-width:650px;margin:0 auto;">
        <div style="background:#418CF0;padding:14px 18px;border-radius:6px 6px 0 0;">
            <h2 style="color:#fff;margin:0;font-size:17px;">Logistics Dashboard Report</h2>
            <p style="color:#dce8fc;margin:3px 0 0;font-size:10px;">
                Date: {_date[0]} to {_date[1]} | Regions: {', '.join(_regions)} | Modes: {', '.join(_modes)}
            </p>
        </div>
        <div style="padding:14px;background:#fff;border:1px solid #e0e0e0;">
            <table style="width:100%;"><tr>{kpi_cells}</tr></table>
            {tables_html}
        </div>
        <div style="padding:6px 14px;background:#f5f5f5;font-size:9px;color:#999;text-align:center;
                     border-radius:0 0 6px 6px;border:1px solid #e0e0e0;border-top:0;">
            Sent from Logistics BI Assistant
        </div>
    </div>"""

    session.sql(
        "CALL SYSTEM$SEND_EMAIL(?, ?, ?, ?, 'text/html')",
        params=[EMAIL_INTEGRATION, recipients, "Logistics Dashboard Report", html],
    ).collect()

# ---------------------------------------------------------------------------
# PDF Export helper (pure-Python PDF with visual charts)
# ---------------------------------------------------------------------------


class _PDF(BasePDF):
    """Landscape Letter PDF for dashboard reports."""

    def __init__(self):
        super().__init__(width=792, height=612, margin=30)

    def kpi_cards(self, kpis):
        n = len(kpis)
        card_w = (self._W - 2 * self._M - (n - 1) * 5) / n
        card_h = 38
        x = self._M
        for label, value in kpis:
            self.rect(x, self._y - card_h, card_w, card_h, 0.97, 0.98, 1.0)
            self.rect(x, self._y, card_w, 2, *BRAND)
            self.text(x + 6, self._y - 14, label, size=6.5, r=0.5, g=0.5, b=0.5)
            self.text(x + 6, self._y - 30, value, size=13, bold=True)
            x += card_w + 5
        self._y -= card_h + 5

    def bar_h(self, x, y, w, h, labels, values, title, is_dollar=False, colors=None):
        self.border(x, y - h, w, h)
        self.section_label(x, y, w, title)
        y -= 18
        if not values:
            return
        max_val = max(values) if max(values) > 0 else 1
        label_w = 75
        val_w = 45
        chart_left = x + label_w
        chart_w = w - label_w - val_w
        n = len(labels)
        avail_h = h - 22
        bar_h_each = min(avail_h / n - 2, 13)
        gap = max((avail_h - n * bar_h_each) / max(n, 1), 1)
        for i, (lab, val) in enumerate(zip(labels, values)):
            by = y - (bar_h_each + gap) * (i + 1) + gap
            bw = (val / max_val) * chart_w if max_val else 0
            c = (colors or COLORS)[i % len(colors or COLORS)]
            self.rect(chart_left, by, max(bw, 1), bar_h_each, *c)
            self.text(x + 2, by + bar_h_each / 2 - 3, str(lab)[:14], size=6)
            display = fmt_number(val, prefix="$") if is_dollar else fmt_number(val)
            self.text(chart_left + bw + 3, by + bar_h_each / 2 - 3, display, size=6, bold=True)
            self.line(chart_left, by, chart_left + chart_w, by, w=0.2)

    def bar_v(self, x, y, w, h, labels, values, title, is_dollar=False, colors=None):
        self.border(x, y - h, w, h)
        self.section_label(x, y, w, title)
        if not values:
            return
        max_val = max(values) if max(values) > 0 else 1
        chart_top = y - 20
        axis_w = 38
        chart_left = x + axis_w
        chart_w = w - axis_w - 5
        chart_h = h - 32
        chart_bottom = chart_top - chart_h
        n = len(labels)
        bar_w = min(chart_w / n * 0.65, 30)
        slot_w = chart_w / n
        for frac in [0.25, 0.5, 0.75, 1.0]:
            gy = chart_bottom + chart_h * frac
            self.line(chart_left, gy, chart_left + chart_w, gy, w=0.2)
            gval = max_val * frac
            disp = fmt_number(gval, prefix="$") if is_dollar else fmt_number(gval)
            self.text(x, gy - 3, disp, size=5, r=0.5, g=0.5, b=0.5)
        for i, (lab, val) in enumerate(zip(labels, values)):
            bh = (val / max_val) * chart_h if max_val else 0
            bx = chart_left + i * slot_w + (slot_w - bar_w) / 2
            c = (colors or COLORS)[i % len(colors or COLORS)]
            self.rect(bx, chart_bottom, bar_w, max(bh, 1), *c)
            lbl = str(lab)[:7]
            self.text(bx + bar_w / 2 - len(lbl) * 1.5, chart_bottom - 9, lbl, size=5)

    def grouped_bar_v(self, x, y, w, h, labels, groups, title, group_colors=None):
        self.border(x, y - h, w, h)
        self.section_label(x, y, w, title)
        all_vals = [v for g in groups.values() for v in g]
        max_val = max(all_vals) if all_vals and max(all_vals) > 0 else 1
        legend_y = y - 22
        axis_w = 38
        chart_left = x + axis_w
        chart_w = w - axis_w - 5
        chart_top = legend_y - 14
        chart_h = h - 52
        chart_bottom = chart_top - chart_h
        n = len(labels)
        ng = len(groups)
        slot_w = chart_w / n
        bar_w = min(slot_w / (ng + 0.5), 20)
        gcolors = group_colors or [hex_to_rgb("#4CAF50"), hex_to_rgb("#F44336")]
        legend_x = chart_left
        for gi, (gname, _) in enumerate(groups.items()):
            c = gcolors[gi % len(gcolors)]
            self.rect(legend_x, legend_y, 8, 8, *c)
            self.text(legend_x + 11, legend_y + 1, gname, size=7, bold=True)
            legend_x += len(gname) * 5 + 24
        for frac in [0.25, 0.5, 0.75, 1.0]:
            gy = chart_bottom + chart_h * frac
            self.line(chart_left, gy, chart_left + chart_w, gy, w=0.2)
            self.text(x, gy - 3, fmt_number(max_val * frac), size=5, r=0.5, g=0.5, b=0.5)
        for gi, (gname, gvals) in enumerate(groups.items()):
            c = gcolors[gi % len(gcolors)]
            for i, val in enumerate(gvals):
                bh = (val / max_val) * chart_h if max_val else 0
                bx = chart_left + i * slot_w + gi * bar_w + (slot_w - ng * bar_w) / 2
                self.rect(bx, chart_bottom, bar_w, max(bh, 1), *c)
        for i, lab in enumerate(labels):
            lx = chart_left + i * slot_w + slot_w / 2 - len(str(lab)[:7]) * 1.5
            self.text(lx, chart_bottom - 9, str(lab)[:7], size=5)

    def donut(self, x, y, w, h, labels, values, title, colors=None):
        self.border(x, y - h, w, h)
        self.section_label(x, y, w, title)
        cy = y - h * 0.5
        cx = x + min(w * 0.32, h * 0.45)
        outer_r = min(w * 0.25, (h - 24) * 0.4)
        inner_r = outer_r * 0.55
        total = sum(values) if sum(values) > 0 else 1
        dc = colors or [hex_to_rgb("#4CAF50"), hex_to_rgb("#FF9800"), hex_to_rgb("#2196F3")]
        angle = 0
        segments = []
        for i, (lab, val) in enumerate(zip(labels, values)):
            sweep = (val / total) * 360
            segments.append((lab, val, angle, sweep, dc[i % len(dc)]))
            angle += sweep
        for lab, val, start_a, sweep, c in segments:
            steps = max(int(sweep / 2), 4)
            for step in range(steps):
                a1 = math.radians(start_a + sweep * step / steps)
                a2 = math.radians(start_a + sweep * (step + 1) / steps)
                x1 = cx + outer_r * math.cos(a1)
                y1 = cy + outer_r * math.sin(a1)
                x2 = cx + outer_r * math.cos(a2)
                y2 = cy + outer_r * math.sin(a2)
                ix1 = cx + inner_r * math.cos(a1)
                iy1 = cy + inner_r * math.sin(a1)
                ix2 = cx + inner_r * math.cos(a2)
                iy2 = cy + inner_r * math.sin(a2)
                self._cmd(f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f} rg "
                          f"{x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l "
                          f"{ix2:.1f} {iy2:.1f} l {ix1:.1f} {iy1:.1f} l f")
        lx = cx + outer_r + 15
        ly = cy + outer_r * 0.5
        for i, (lab, val, _, _, c) in enumerate(segments):
            pct = val / total * 100
            self.rect(lx, ly - 2, 7, 7, *c)
            self.text(lx + 10, ly, f"{lab}: {fmt_number(val)} ({pct:.0f}%)", size=6.5)
            ly -= 13


@st.cache_data(ttl=600)
def build_pdf_report(_date, _regions, _modes):
    pdf = _PDF()
    M = pdf._M
    W = pdf._W
    H = pdf._H
    col_gap = 10
    col_w = (W - 2 * M - col_gap) / 2
    left_x = M
    right_x = M + col_w + col_gap

    # ========= PAGE 1: Title + KPIs + Revenue by Nation (full width) =========
    pdf.title_bar(
        "Logistics Dashboard Report",
        f"Generated: {datetime.datetime.now().strftime('%B %d, %Y %H:%M')}   |   "
        f"Date: {_date[0]} to {_date[1]}   |   "
        f"Regions: {', '.join(_regions)}   |   Modes: {', '.join(_modes)}",
    )

    orders_kpi, shipments_kpi, customers_kpi = load_kpi_data(_date, _regions, _modes)
    pdf.kpi_cards([
        ("Total Revenue", fmt_number(orders_kpi["REV"], prefix="$")),
        ("Total Orders", fmt_number(orders_kpi["CNT"])),
        ("Avg Order Value", fmt_number(orders_kpi["AOV"], prefix="$")),
        ("Total Shipments", fmt_number(shipments_kpi["CNT"])),
        ("Avg Delivery Days", f"{shipments_kpi['AVG_DAYS']:.1f}"),
        ("Active Customers", fmt_number(customers_kpi["CNT"])),
    ])

    nations = load_top_nations(_date, _regions)
    if not nations.empty:
        df = nations.sort_values("REVENUE", ascending=True).tail(10)
        row_y = pdf._y
        full_w = W - 2 * M
        chart_h = row_y - M
        pdf.bar_h(M, row_y, full_w, chart_h, df["NATION"].tolist(),
                  df["REVENUE"].tolist(), "Revenue by Nation", is_dollar=True, colors=[BRAND])

    # ========= PAGE 2: 2x2 grid — Volume, Trend, Segment, Region =========
    pdf._new_page()
    pdf.rect(0, H - 3, W, 3, *BRAND)
    row_h = (H - 2 * M - 8) / 2
    top_y = H - M
    bot_y = top_y - row_h - 8

    volume = load_shipping_volume(_date, _modes)
    if not volume.empty:
        labels = [str(m)[:7] for m in volume["MONTH"].tolist()[-16:]]
        vals = volume["SHIPMENTS"].tolist()[-16:]
        pdf.bar_v(left_x, top_y, col_w, row_h, labels, vals,
                  "Shipping Volume Over Time", colors=[BRAND])

    monthly = load_monthly_revenue(_date, _regions, _modes)
    if not monthly.empty:
        labels = [str(m)[:7] for m in monthly["MONTH"].tolist()[-16:]]
        vals = monthly["REVENUE"].tolist()[-16:]
        pdf.bar_v(right_x, top_y, col_w, row_h, labels, vals,
                  "Revenue Trend", is_dollar=True, colors=[BRAND])

    segment = load_segment_stats(_date, _regions)
    if not segment.empty:
        df = segment.sort_values("REVENUE", ascending=False)
        pdf.bar_v(left_x, bot_y, col_w, row_h, df["SEGMENT"].tolist(),
                  df["REVENUE"].tolist(), "Revenue by Market Segment", is_dollar=True)

    region = load_revenue_by_region(_date, _regions)
    if not region.empty:
        df = region.sort_values("REVENUE", ascending=True)
        pdf.bar_h(right_x, bot_y, col_w, row_h, df["REGION"].tolist(),
                  df["REVENUE"].tolist(), "Revenue by Region", is_dollar=True)

    # ========= PAGE 3: 2x2 grid — Status, On-Time, Priority, Ship Mode =========
    pdf._new_page()
    pdf.rect(0, H - 3, W, 3, *BRAND)
    top_y = H - M
    bot_y = top_y - row_h - 8

    status = load_order_status(_date, _regions)
    if not status.empty:
        status_colors = [hex_to_rgb("#4CAF50"), hex_to_rgb("#FF9800"), hex_to_rgb("#2196F3")]
        pdf.donut(left_x, top_y, col_w, row_h, status["STATUS"].tolist(),
                  status["ORDERS"].tolist(), "Order Status Breakdown", colors=status_colors)

    ontime = load_ontime_late(_date, _modes)
    if not ontime.empty:
        pdf.grouped_bar_v(right_x, top_y, col_w, row_h,
                          ontime["SHIP_MODE"].tolist(),
                          {"On-Time": ontime["ON_TIME"].tolist(), "Late": ontime["LATE"].tolist()},
                          "On-Time vs Late Deliveries")

    priority = load_revenue_by_priority(_date, _regions)
    if not priority.empty:
        df = priority.sort_values("REVENUE", ascending=True)
        pdf.bar_h(left_x, bot_y, col_w, row_h, df["PRIORITY"].tolist(),
                  df["REVENUE"].tolist(), "Revenue by Priority", is_dollar=True, colors=[BRAND])

    shipmode = load_shipmode_stats(_date, _modes)
    if not shipmode.empty:
        df = shipmode.sort_values("SHIPMENTS", ascending=True)
        pdf.bar_h(right_x, bot_y, col_w, row_h, df["SHIP_MODE"].tolist(),
                  df["SHIPMENTS"].tolist(), "Shipments by Mode")

    return pdf.build()


# ---------------------------------------------------------------------------
# Title + Action buttons
# ---------------------------------------------------------------------------
# Initialize filter defaults (used by export before filters render)
if "f_date" not in st.session_state:
    st.session_state.f_date = (str(DATE_MIN), str(DATE_MAX))
if "f_regions" not in st.session_state:
    st.session_state.f_regions = ALL_REGIONS
if "f_ship_modes" not in st.session_state:
    st.session_state.f_ship_modes = ALL_SHIP_MODES

with st.container(horizontal=True):
    st.subheader(":material/dashboard: Logistics Dashboard")
    if st.button("BI Assistant", icon=":material/smart_toy:", key="dash_ai_btn", type="primary"):
        st.session_state.dash_chat_open = not st.session_state.dash_chat_open
        st.rerun()
    if st.button("Refresh Data", icon=":material/refresh:"):
        st.cache_data.clear()
        st.rerun()
    with st.popover("Export", icon=":material/download:"):
        _fd = st.session_state.f_date
        _fr = st.session_state.f_regions
        _fm = st.session_state.f_ship_modes
        export_data, export_name, export_mime = build_data_export(_fd, _fr, _fm)
        is_zip = export_name.endswith(".zip")
        st.download_button(
            label="Export ZIP" if is_zip else "Export CSV",
            data=export_data,
            file_name=export_name,
            mime=export_mime,
            icon=":material/folder_zip:" if is_zip else ":material/download:",
            use_container_width=True,
            help="ZIP of CSVs (multiple datasets)" if is_zip else "Single CSV",
        )
        st.download_button(
            label="Export PDF",
            data=build_pdf_report(_fd, _fr, _fm),
            file_name="logistics_dashboard_report.pdf",
            mime="application/pdf",
            icon=":material/picture_as_pdf:",
            use_container_width=True,
            help="Export Dashboard",
        )
        st.divider()
        st.caption("Email Dashboard")
        email_to = st.text_input("Recipient email(s)", placeholder="user@example.com",
                                 key="dash_email_to", label_visibility="collapsed")
        if st.button("Send Email", icon=":material/send:", use_container_width=True,
                     disabled=not email_to):
            with st.spinner("Sending..."):
                try:
                    send_dashboard_email(email_to.strip(), _fd, _fr, _fm)
                    st.success("Email sent!")
                except Exception as e:
                    st.error(f"Failed: {e}")

# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
filter_cols = st.columns(3)
with filter_cols[0]:
    date_range = st.date_input(
        "Date Range",
        value=(DATE_MIN, DATE_MAX),
        min_value=DATE_MIN,
        max_value=DATE_MAX,
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        f_date = (str(date_range[0]), str(date_range[1]))
    else:
        f_date = (str(DATE_MIN), str(DATE_MAX))
with filter_cols[1]:
    selected_regions = st.multiselect("Region", ALL_REGIONS)
    f_regions = tuple(selected_regions) if selected_regions else ALL_REGIONS
with filter_cols[2]:
    selected_modes = st.multiselect("Ship Mode", ALL_SHIP_MODES)
    f_ship_modes = tuple(selected_modes) if selected_modes else ALL_SHIP_MODES

# Update session state so export picks up latest filters on next run
st.session_state.f_date = f_date
st.session_state.f_regions = f_regions
st.session_state.f_ship_modes = f_ship_modes

# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
with st.spinner("Loading metrics..."):
    orders_kpi, shipments_kpi, customers_kpi = load_kpi_data(f_date, f_regions, f_ship_modes)

kpi_row1 = st.columns(3)
with kpi_row1[0]:
    st.metric("Total Revenue", fmt_number(orders_kpi['REV'], prefix="$"), border=True)
with kpi_row1[1]:
    st.metric("Total Orders", fmt_number(orders_kpi['CNT']), border=True)
with kpi_row1[2]:
    st.metric("Avg Order Value", fmt_number(orders_kpi['AOV'], prefix="$"), border=True)

kpi_row2 = st.columns(3)
with kpi_row2[0]:
    st.metric("Total Shipments", fmt_number(shipments_kpi['CNT']), border=True)
with kpi_row2[1]:
    st.metric("Avg Delivery Days", f"{shipments_kpi['AVG_DAYS']:.1f}", border=True)
with kpi_row2[2]:
    st.metric("Total Customers", fmt_number(customers_kpi['CNT']), border=True)

# ---------------------------------------------------------------------------
# Nation coordinates for map
# ---------------------------------------------------------------------------
NATION_COORDS = {
    "ALGERIA": (28.0, 3.0), "ARGENTINA": (-34.0, -64.0), "BRAZIL": (-14.0, -51.0),
    "CANADA": (56.0, -106.0), "CHINA": (35.0, 105.0), "EGYPT": (26.0, 30.0),
    "ETHIOPIA": (9.0, 38.7), "FRANCE": (46.0, 2.0), "GERMANY": (51.0, 10.0),
    "INDIA": (21.0, 78.0), "INDONESIA": (-5.0, 120.0), "IRAN": (32.0, 53.0),
    "IRAQ": (33.0, 44.0), "JAPAN": (36.0, 138.0), "JORDAN": (31.0, 36.0),
    "KENYA": (0.0, 38.0), "MOROCCO": (32.0, -5.0), "MOZAMBIQUE": (-18.0, 35.0),
    "PERU": (-10.0, -76.0), "ROMANIA": (46.0, 25.0), "RUSSIA": (61.0, 105.0),
    "SAUDI ARABIA": (24.0, 45.0), "UNITED KINGDOM": (55.0, -3.0),
    "UNITED STATES": (37.0, -96.0), "VIETNAM": (14.0, 108.0),
}

# ---------------------------------------------------------------------------
# Row 1: Revenue by Nation (full-width map)
# ---------------------------------------------------------------------------
with st.container(border=True):
    st.subheader("Revenue by Nation")
    nations_data = load_top_nations(f_date, f_regions)
    if not nations_data.empty:
        map_records = []
        rev_max = float(nations_data["REVENUE"].max())
        for _, row in nations_data.iterrows():
            coords = NATION_COORDS.get(row["NATION"], (0, 0))
            map_records.append({
                "NATION": row["NATION"],
                "lat": float(coords[0]),
                "lon": float(coords[1]),
                "radius": float(row["REVENUE"]) / rev_max * 600000,
                "revenue_fmt": fmt_number(float(row["REVENUE"]), prefix="$"),
                "orders_fmt": fmt_number(float(row["ORDERS"])),
            })
        st.pydeck_chart(pdk.Deck(
            layers=[pdk.Layer("ScatterplotLayer", data=map_records,
                get_position=["lon", "lat"], get_radius="radius",
                get_fill_color=[65, 140, 240, 180], pickable=True)],
            initial_view_state=pdk.ViewState(latitude=20, longitude=20, zoom=1.2),
            tooltip={"html": "<b>{NATION}</b><br/>Revenue: {revenue_fmt}<br/>Orders: {orders_fmt}",
                     "style": {"backgroundColor": "rgba(0,0,0,0.75)", "color": "white",
                               "fontSize": "13px", "padding": "8px 12px", "borderRadius": "6px"}},
            map_style="light",
        ), height=400)
    else:
        st.info("No data for selected filters.")

# ---------------------------------------------------------------------------
# Row 2: Shipping Volume Over Time + Revenue Trend
# ---------------------------------------------------------------------------
col1, col2 = st.columns(2)

with col1:
    with st.container(border=True):
        st.subheader("Shipping Volume Over Time")
        volume_data = load_shipping_volume(f_date, f_ship_modes)
        if not volume_data.empty:
            chart_df = volume_data.copy()
            chart_df["Shipment Count"] = chart_df["SHIPMENTS"].apply(lambda v: fmt_number(v))
            st.vega_lite_chart(chart_df, {
                "height": CHART_H,
                "mark": {"type": "area", "opacity": 0.6, "line": True},
                "encoding": {
                    "x": {"field": "MONTH", "type": "temporal", "title": "Month"},
                    "y": {"field": "SHIPMENTS", "type": "quantitative", "title": "Shipments",
                          "axis": {"labelExpr": BMK_EXPR}},
                    "tooltip": [
                        {"field": "MONTH", "type": "temporal", "title": "Month", "format": "%b %Y"},
                        {"field": "Shipment Count", "title": "Shipments", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

with col2:
    with st.container(border=True):
        st.subheader("Revenue Trend")
        monthly_data = load_monthly_revenue(f_date, f_regions, f_ship_modes)
        if not monthly_data.empty:
            trend_df = monthly_data.copy()
            trend_df["Revenue"] = trend_df["REVENUE"].apply(lambda v: fmt_number(v, prefix="$"))
            trend_df["Orders"] = trend_df["ORDERS"].apply(lambda v: fmt_number(v))
            st.vega_lite_chart(trend_df, {
                "height": CHART_H,
                "mark": {"type": "area", "opacity": 0.3, "line": {"strokeWidth": 2}, "color": "#4285F4"},
                "encoding": {
                    "x": {"field": "MONTH", "type": "temporal", "title": "Month",
                          "axis": {"format": "%b %Y"}},
                    "y": {"field": "REVENUE", "type": "quantitative", "title": "Revenue",
                          "axis": {"labelExpr": BMK_EXPR_DOLLAR, "tickCount": 3}},
                    "tooltip": [
                        {"field": "MONTH", "type": "temporal", "title": "Month", "format": "%b %Y"},
                        {"field": "Revenue", "title": "Revenue", "type": "nominal"},
                        {"field": "Orders", "title": "Orders", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

# ---------------------------------------------------------------------------
# Row 3: Revenue by Market Segment + Revenue by Region
# ---------------------------------------------------------------------------
col3, col4 = st.columns(2)

with col3:
    with st.container(border=True):
        st.subheader("Revenue by Market Segment")
        segment_data = load_segment_stats(f_date, f_regions)
        if not segment_data.empty:
            chart_df = segment_data.copy()
            chart_df["Order Count"] = chart_df["ORDERS"].apply(lambda v: fmt_number(v))
            chart_df["Total Revenue"] = chart_df["REVENUE"].apply(lambda v: fmt_number(v, prefix="$"))
            st.vega_lite_chart(chart_df, {
                "height": CHART_H,
                "mark": {"type": "bar", "cornerRadiusEnd": 4},
                "encoding": {
                    "x": {"field": "SEGMENT", "type": "nominal", "sort": "-y", "title": "Market Segment"},
                    "y": {"field": "REVENUE", "type": "quantitative", "title": "Revenue",
                          "axis": {"labelExpr": BMK_EXPR_DOLLAR}},
                    "tooltip": [
                        {"field": "SEGMENT", "title": "Segment", "type": "nominal"},
                        {"field": "Order Count", "title": "Orders", "type": "nominal"},
                        {"field": "Total Revenue", "title": "Revenue", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

with col4:
    with st.container(border=True):
        st.subheader("Revenue by Region")
        region_data = load_revenue_by_region(f_date, f_regions)
        if not region_data.empty:
            chart_df = region_data.copy()
            chart_df["Revenue"] = chart_df["REVENUE"].apply(lambda v: fmt_number(v, prefix="$"))
            st.vega_lite_chart(chart_df, {
                "height": CHART_H,
                "mark": {"type": "bar", "cornerRadiusEnd": 4},
                "encoding": {
                    "y": {"field": "REGION", "type": "nominal", "title": None, "sort": "-x"},
                    "x": {"field": "REVENUE", "type": "quantitative", "title": "Revenue",
                          "axis": {"labelExpr": BMK_EXPR_DOLLAR}},
                    "color": {"field": "REGION", "type": "nominal", "legend": None},
                    "tooltip": [
                        {"field": "REGION", "title": "Region", "type": "nominal"},
                        {"field": "Revenue", "title": "Revenue", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

# ---------------------------------------------------------------------------
# Row 4: Order Status Breakdown + On-Time vs Late Deliveries
# ---------------------------------------------------------------------------
col5, col6 = st.columns(2)

with col5:
    with st.container(border=True):
        st.subheader("Order Status Breakdown")
        status_data = load_order_status(f_date, f_regions)
        if not status_data.empty:
            chart_df = status_data.copy()
            chart_df["Order Count"] = chart_df["ORDERS"].apply(lambda v: fmt_number(v))
            st.vega_lite_chart(chart_df, {
                "height": CHART_H,
                "mark": {"type": "arc", "innerRadius": 60, "outerRadius": 120},
                "encoding": {
                    "theta": {"field": "ORDERS", "type": "quantitative", "stack": True},
                    "color": {"field": "STATUS", "type": "nominal", "title": "Status",
                              "scale": {"domain": ["Fulfilled", "Open", "Partial"],
                                        "range": ["#4CAF50", "#FF9800", "#2196F3"]}},
                    "tooltip": [
                        {"field": "STATUS", "title": "Status", "type": "nominal"},
                        {"field": "Order Count", "title": "Orders", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

with col6:
    with st.container(border=True):
        st.subheader("On-Time vs Late Deliveries")
        ontime_data = load_ontime_late(f_date, f_ship_modes)
        if not ontime_data.empty:
            melted = ontime_data.melt(id_vars=["SHIP_MODE"], value_vars=["ON_TIME", "LATE"],
                                       var_name="STATUS", value_name="COUNT")
            melted["STATUS"] = melted["STATUS"].map({"ON_TIME": "On-Time", "LATE": "Late"})
            melted["Shipments"] = melted["COUNT"].apply(lambda v: fmt_number(v))
            st.vega_lite_chart(melted, {
                "height": CHART_H,
                "mark": {"type": "bar", "cornerRadiusEnd": 3},
                "encoding": {
                    "x": {"field": "SHIP_MODE", "type": "nominal", "title": "Ship Mode"},
                    "y": {"field": "COUNT", "type": "quantitative", "title": "Shipments",
                          "axis": {"labelExpr": BMK_EXPR}},
                    "color": {"field": "STATUS", "type": "nominal", "title": "Delivery",
                              "scale": {"domain": ["On-Time", "Late"], "range": ["#4CAF50", "#F44336"]}},
                    "tooltip": [
                        {"field": "SHIP_MODE", "title": "Ship Mode", "type": "nominal"},
                        {"field": "STATUS", "title": "Status", "type": "nominal"},
                        {"field": "Shipments", "title": "Count", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

# ---------------------------------------------------------------------------
# Row 5: Revenue by Priority + Shipments by Mode
# ---------------------------------------------------------------------------
col7, col8 = st.columns(2)

with col7:
    with st.container(border=True):
        st.subheader("Revenue by Priority")
        priority_data = load_revenue_by_priority(f_date, f_regions)
        if not priority_data.empty:
            chart_df = priority_data.copy()
            chart_df["Total Revenue"] = chart_df["REVENUE"].apply(lambda v: fmt_number(v, prefix="$"))
            chart_df["Order Count"] = chart_df["ORDERS"].apply(lambda v: fmt_number(v))
            st.vega_lite_chart(chart_df, {
                "height": CHART_H,
                "mark": {"type": "bar", "cornerRadiusEnd": 4},
                "encoding": {
                    "y": {"field": "PRIORITY", "type": "nominal", "title": "Priority", "sort": "-x"},
                    "x": {"field": "REVENUE", "type": "quantitative", "title": "Revenue",
                          "axis": {"labelExpr": BMK_EXPR_DOLLAR}},
                    "tooltip": [
                        {"field": "PRIORITY", "title": "Priority", "type": "nominal"},
                        {"field": "Total Revenue", "title": "Revenue", "type": "nominal"},
                        {"field": "Order Count", "title": "Orders", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

with col8:
    with st.container(border=True):
        st.subheader("Shipments by Mode")
        shipmode_data = load_shipmode_stats(f_date, f_ship_modes)
        if not shipmode_data.empty:
            chart_df = shipmode_data.copy()
            chart_df["Shipments"] = chart_df["SHIPMENTS"].apply(lambda v: fmt_number(v))
            chart_df["Avg Delivery Days"] = chart_df["AVG_DELIVERY_DAYS"].apply(lambda v: f"{v:.1f}")
            st.vega_lite_chart(chart_df, {
                "height": CHART_H,
                "mark": {"type": "bar", "cornerRadiusEnd": 4},
                "encoding": {
                    "y": {"field": "SHIP_MODE", "type": "nominal", "title": "Ship Mode", "sort": "-x"},
                    "x": {"field": "SHIPMENTS", "type": "quantitative", "title": "Shipments",
                          "axis": {"labelExpr": BMK_EXPR}},
                    "color": {"field": "SHIP_MODE", "type": "nominal", "title": "Ship Mode"},
                    "tooltip": [
                        {"field": "SHIP_MODE", "title": "Ship Mode", "type": "nominal"},
                        {"field": "Shipments", "title": "Shipments", "type": "nominal"},
                        {"field": "Avg Delivery Days", "title": "Avg Delivery Days", "type": "nominal"},
                    ],
                },
                "config": VEGA_CONFIG,
            }, use_container_width=True)
        else:
            st.info("No data for selected filters.")

# ---------------------------------------------------------------------------
# BI Assistant chat — right-side panel using sidebar
# ---------------------------------------------------------------------------
if st.session_state.dash_chat_open:
    st.markdown("""
    <style>
    /* Move sidebar to the right */
    section[data-testid="stSidebar"] {
        position: fixed !important;
        top: 0 !important;
        right: 0 !important;
        left: auto !important;
        width: 380px !important;
        min-width: 380px !important;
        height: 100vh !important;
        z-index: 999999 !important;
        box-shadow: -4px 0 24px rgba(0,0,0,0.15) !important;
        transform: none !important;
    }
    section[data-testid="stSidebar"] > div {
        width: 380px !important;
    }
    /* Keep main content in place */
    .stMainBlockContainer, section.stMain {
        margin-left: 0 !important;
        margin-right: 0 !important;
    }
    /* Smaller text in chat */
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] li,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] .stMarkdown {
        font-size: 13px !important;
        line-height: 1.4 !important;
    }
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3,
    section[data-testid="stSidebar"] h1 span,
    section[data-testid="stSidebar"] h2 span,
    section[data-testid="stSidebar"] h3 span {
        font-size: 20px !important;
    }
    section[data-testid="stSidebar"] .stChatMessage {
        padding: 6px 10px !important;
    }
    /* No outer scroll */
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] > div,
    section[data-testid="stSidebar"] > div > div,
    section[data-testid="stSidebar"] > div > div > div {
        overflow: hidden !important;
    }
    </style>
    """, unsafe_allow_html=True)

    with st.sidebar:
        hdr1, hdr2 = st.columns([4, 1])
        with hdr1:
            st.subheader(":material/smart_toy: BI Assistant")
        with hdr2:
            if st.button(":material/close:", key="dash_close_chat", help="Close"):
                st.session_state.dash_chat_open = False
                st.rerun()

        chat_box = st.container(height=550)
        last_followups = []
        with chat_box:
            if not st.session_state.dash_messages:
                suggestions = [
                    (":material/trending_up:", "Revenue by region"),
                    (":material/schedule:", "Avg delivery time by ship mode"),
                    (":material/group:", "Market segments by revenue"),
                    (":material/priority_high:", "Top order priorities by revenue"),
                    (":material/inventory:", "Late shipments by ship mode"),
                    (":material/public:", "Top 5 nations by revenue"),
                ]
                for icon, label in suggestions:
                    if st.button(label, icon=icon, key=f"dsug_{label[:10]}",
                                 use_container_width=True):
                        st.session_state.dash_messages.append(
                            {"role": "user", "content": f"What is the {label.lower()}?"})
                        st.rerun()
            else:
                for msg in st.session_state.dash_messages:
                    with st.chat_message(msg["role"]):
                        if msg["role"] == "assistant":
                            display_text, followups = extract_followups(msg["content"])
                            st.markdown(display_text)
                            last_followups = followups
                        else:
                            st.markdown(msg["content"])

                if last_followups and st.session_state.dash_messages[-1]["role"] == "assistant":
                    for i, q in enumerate(last_followups[:3]):
                        if st.button(q, icon=":material/chat:", key=f"dfup_{i}_{q[:20]}",
                                     use_container_width=True):
                            st.session_state.dash_messages.append({"role": "user", "content": q})
                            st.rerun()

        if prompt := st.chat_input("Ask about the data...", key="dash_chat_input"):
            st.session_state.dash_messages.append({"role": "user", "content": prompt})

        if st.session_state.dash_messages and st.session_state.dash_messages[-1]["role"] == "user":
            user_q = st.session_state.dash_messages[-1]["content"]
            selected_schema = st.session_state.get("b_data_source", LOGISTICS_SCHEMA)
            use_agent = selected_schema == LOGISTICS_SCHEMA
            with chat_box:
                with st.chat_message("assistant"):
                    with st.spinner("Thinking..."):
                        try:
                            if use_agent:
                                answer = call_agent(user_q)
                            else:
                                answer = llm_query(conn, user_q, selected_schema)
                        except Exception as e:
                            answer = f"Error: {e}"
                    display_answer = strip_sql_blocks(answer) if not use_agent else answer
                    st.markdown(display_answer)
            st.session_state.dash_messages.append({"role": "assistant", "content": display_answer})
            st.rerun()

        if st.session_state.dash_messages:
            if st.button("Clear chat", icon=":material/delete:", use_container_width=True,
                         key="dash_clear_chat"):
                st.session_state.dash_messages = []
                st.rerun()
else:
    st.markdown("""
    <style>
    section[data-testid="stSidebar"] {
        display: none !important;
    }
    button[data-testid="stSidebarNavCollapseButton"],
    button[data-testid="stSidebarCollapseButton"],
    button[data-testid="collapsedControl"],
    div[data-testid="collapsedControl"] {
        display: none !important;
    }
    </style>
    """, unsafe_allow_html=True)
