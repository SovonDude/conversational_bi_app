# Logistics BI Assistant v3
import datetime
import json
import os

import streamlit as st

st.set_page_config(
    page_title="Logistics BI Assistant",
    page_icon=":material/local_shipping:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Reduce top padding
st.markdown("""
<style>
[data-testid="stMainBlockContainer"] {
    padding-top: 1rem !important;
}
[data-testid="stHeader"] {
    height: 0 !important;
    min-height: 0 !important;
    padding: 0 !important;
    visibility: hidden !important;
}
</style>
""", unsafe_allow_html=True)

conn = st.connection("snowflake", ttl=os.getenv("SNOWFLAKE_CONNECTION_TTL"))

# ---------------------------------------------------------------------------
# Shared constants and helpers (available to pages via st.session_state)
# ---------------------------------------------------------------------------
AGENT_FQN = "CONVERSATIONAL_BI_ASSISTANT.SEMANTIC_MODELS.LOGISTICS_BI_AGENT"
ALL_REGIONS = ("AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST")
ALL_SHIP_MODES = ("AIR", "FOB", "MAIL", "RAIL", "REG AIR", "SHIP", "TRUCK")
DATE_MIN = datetime.date(1992, 1, 1)
DATE_MAX = datetime.date(1998, 8, 2)

BMK_EXPR = "datum.value >= 1e9 ? format(datum.value / 1e9, '.1f') + 'B' : datum.value >= 1e6 ? format(datum.value / 1e6, '.1f') + 'M' : datum.value >= 1e3 ? format(datum.value / 1e3, '.1f') + 'K' : format(datum.value, ',.0f')"
BMK_EXPR_DOLLAR = "datum.value >= 1e9 ? '$' + format(datum.value / 1e9, '.1f') + 'B' : datum.value >= 1e6 ? '$' + format(datum.value / 1e6, '.1f') + 'M' : datum.value >= 1e3 ? '$' + format(datum.value / 1e3, '.1f') + 'K' : '$' + format(datum.value, ',.0f')"


def fmt_number(value, prefix="", decimal=1):
    abs_val = abs(value)
    if abs_val >= 1_000_000_000:
        return f"{prefix}{value / 1_000_000_000:,.{decimal}f}B"
    if abs_val >= 1_000_000:
        return f"{prefix}{value / 1_000_000:,.{decimal}f}M"
    if abs_val >= 1_000:
        return f"{prefix}{value / 1_000:,.{decimal}f}K"
    return f"{prefix}{value:,.{decimal}f}"


def to_float(df):
    for col in df.select_dtypes(include=["object", "number"]).columns:
        try:
            df[col] = df[col].astype(float)
        except (ValueError, TypeError):
            pass
    return df


def _date_clause(col, d1, d2):
    return f"{col} BETWEEN '{d1}' AND '{d2}'"


def _in_clause(col, values):
    quoted = ",".join(f"'{v}'" for v in values)
    return f"{col} IN ({quoted})"


def call_agent(question: str) -> str:
    session = conn.session()
    request_body = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": question}],
                }
            ],
            "stream": False,
        }
    )
    result = session.sql(
        "SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(?, ?) AS resp",
        params=[AGENT_FQN, request_body],
    ).collect()
    resp = json.loads(result[0]["RESP"])
    parts = []
    for item in resp.get("content", []):
        if item.get("type") == "text":
            parts.append(item["text"])
    return "\n\n".join(parts) if parts else "No response from agent."


# Store shared objects in session state for page access
st.session_state.conn = conn
st.session_state.shared = {
    "AGENT_FQN": AGENT_FQN,
    "ALL_REGIONS": ALL_REGIONS,
    "ALL_SHIP_MODES": ALL_SHIP_MODES,
    "DATE_MIN": DATE_MIN,
    "DATE_MAX": DATE_MAX,
    "BMK_EXPR": BMK_EXPR,
    "BMK_EXPR_DOLLAR": BMK_EXPR_DOLLAR,
    "fmt_number": fmt_number,
    "to_float": to_float,
    "_date_clause": _date_clause,
    "_in_clause": _in_clause,
    "call_agent": call_agent,
}

# ---------------------------------------------------------------------------
# Theme toggle (dark/light)
# ---------------------------------------------------------------------------
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = False

DARK_CSS = """
<style>
:root { color-scheme: dark; }
/* App background */
.stApp, .stMainBlockContainer, section.stMain,
[data-testid="stAppViewContainer"] {
    background-color: #0d1117 !important;
    color: #c9d1d9 !important;
}
/* Keep top header/nav bar light */
[data-testid="stHeader"] {
    background-color: #ffffff !important;
    color: #333333 !important;
}
[data-testid="stHeader"] button,
[data-testid="stHeader"] span,
[data-testid="stHeader"] a,
nav[data-testid="stPageNav"],
nav[data-testid="stPageNav"] a,
nav[data-testid="stPageNav"] span,
[data-testid="stPageLink"],
[data-testid="stPageLink"] span {
    color: #333333 !important;
}
/* Sidebar / chat panel */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] > div,
section[data-testid="stSidebar"] > div > div {
    background-color: #161b22 !important;
    color: #c9d1d9 !important;
}
/* Cards and bordered containers */
[data-testid="stVerticalBlockBorderWrapper"] {
    background-color: #161b22 !important;
    border-color: #30363d !important;
}
[data-testid="stVerticalBlockBorderWrapper"] > div,
[data-testid="stVerticalBlockBorderWrapper"] > div > div,
[data-testid="stVerticalBlockBorderWrapper"] > div > div > div {
    background-color: #161b22 !important;
}
/* Scrollable containers (st.container with height) */
[data-testid="stScrollableBlockContainer"],
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlock"] {
    background-color: #161b22 !important;
}
/* Text */
.stMarkdown, .stMarkdown p, .stMarkdown li, .stMarkdown span,
[data-testid="stMetricValue"], [data-testid="stMetricDelta"],
[data-testid="stMetricLabel"],
[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] *,
.stCaption, .stCaption * {
    color: #c9d1d9 !important;
}
h1, h2, h3, h4, h5, h6,
.stSubheader, [data-testid="stHeading"] {
    color: #f0f6fc !important;
}
/* Filter labels */
label, .stSelectbox label, .stMultiSelect label,
.stDateInput label {
    color: #c9d1d9 !important;
}
/* Selectbox, multiselect, date input — readable in dark mode */
.stSelectbox > div > div,
.stMultiSelect > div > div,
[data-testid="stDateInput"] > div > div,
.stSelectbox [data-baseweb="select"],
.stMultiSelect [data-baseweb="select"],
[data-baseweb="input"],
[data-baseweb="popover"] > div,
/* Fix inner black patch in multiselect/selectbox */
.stMultiSelect [data-baseweb="select"] > div,
.stMultiSelect [data-baseweb="select"] > div > div,
.stSelectbox [data-baseweb="select"] > div,
.stSelectbox [data-baseweb="select"] > div > div,
[data-baseweb="input"] > div,
[data-baseweb="input"] input,
.stMultiSelect input,
.stSelectbox input {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
    border-color: #30363d !important;
}
/* Placeholder text — catch all nested text inside selects */
.stMultiSelect [data-baseweb="select"] *,
.stSelectbox [data-baseweb="select"] * {
    color: #c9d1d9 !important;
}
/* Selectbox indicator/arrow icon */
.stSelectbox [data-baseweb="select"] svg,
.stMultiSelect [data-baseweb="select"] svg {
    fill: #c9d1d9 !important;
    color: #c9d1d9 !important;
}
/* Selectbox clear/indicator container — prevent it from covering text */
.stSelectbox [data-baseweb="select"] > div > div:last-child,
.stMultiSelect [data-baseweb="select"] > div > div:last-child {
    background: transparent !important;
}
.stMultiSelect input::placeholder,
.stSelectbox input::placeholder,
[data-baseweb="input"] input::placeholder {
    color: #8b949e !important;
}
/* Dropdown menu items */
[data-baseweb="menu"], [data-baseweb="menu"] li,
[role="listbox"], [role="listbox"] li,
[role="option"] {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
}
[role="option"]:hover, [data-baseweb="menu"] li:hover {
    background-color: #30363d !important;
}
/* Selected tags in multiselect */
[data-baseweb="tag"] {
    background-color: #30363d !important;
    color: #c9d1d9 !important;
}
/* Text inputs */
input, textarea, [data-testid="stTextInput"] input,
.stTextInput > div > div > input {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
    border-color: #30363d !important;
}
/* Buttons — all types */
.stButton > button, .stDownloadButton > button,
button[kind="secondary"], button[kind="tertiary"],
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-tertiary"],
[data-testid="stBaseButton-header"] {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
    border: 1px solid #30363d !important;
    padding: 0.4rem 0.8rem !important;
    margin: 0 4px !important;
}
/* Popover trigger button — clean styling */
[data-testid="stPopover"] button {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
    border: 1px solid #30363d !important;
    margin: 0 !important;
    padding: 0.4rem 0.8rem !important;
    outline: none !important;
    box-shadow: none !important;
}
/* Popover wrapper — strip ALL visual properties so no rectangular box shows */
[data-testid="stPopover"] {
    background: none !important;
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
    overflow: visible !important;
}
/* Every div between the popover root and the button */
[data-testid="stPopover"] *:not(button):not(button *) {
    background: transparent !important;
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
}
.stButton > button:hover, .stDownloadButton > button:hover,
button[kind="secondary"]:hover, button[kind="tertiary"]:hover {
    background-color: #30363d !important;
}
/* Keep primary buttons accent-colored */
button[kind="primary"],
[data-testid="stBaseButton-primary"] {
    background-color: #418CF0 !important;
    color: #ffffff !important;
    border-color: #418CF0 !important;
}
button[kind="primary"]:hover,
[data-testid="stBaseButton-primary"]:hover {
    background-color: #3070d4 !important;
}
/* Button text */
button p, button span, .stButton > button p,
.stButton > button span {
    color: inherit !important;
}
/* Chat messages */
[data-testid="stChatMessage"], .stChatMessage {
    background-color: #161b22 !important;
    border-color: #30363d !important;
    color: #c9d1d9 !important;
}
/* User messages — slightly different shade */
[data-testid="stChatMessage"][data-testid-type="user"],
[data-testid="stChatMessage"]:has(.stChatMessageAvatarUser) {
    background-color: #1c2333 !important;
}
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] span,
[data-testid="stChatMessage"] li,
[data-testid="stChatMessage"] code,
[data-testid="stChatMessage"] .stMarkdown,
[data-testid="stChatMessage"] div,
.stChatMessage p, .stChatMessage span, .stChatMessage li,
.stChatMessage div {
    color: #c9d1d9 !important;
}
[data-testid="stChatMessage"] code {
    background-color: #30363d !important;
}
/* Chat container / scrollable area */
[data-testid="stChatMessageContainer"],
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlock"] {
    background-color: transparent !important;
}
/* Chat avatar */
[data-testid="stChatMessage"] [data-testid="stChatMessageAvatar"],
[data-testid="stChatMessage"] img {
    opacity: 0.9;
}
/* Chat input box */
[data-testid="stChatInput"],
[data-testid="stChatInput"] > div,
[data-testid="stChatInput"] textarea,
[data-testid="stChatInput"] > div > div,
.stChatInput, .stChatInput > div,
.stChatInput textarea {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
    border-color: #30363d !important;
}
[data-testid="stChatInput"] textarea::placeholder,
.stChatInput textarea::placeholder {
    color: #8b949e !important;
}
/* Chat input send button */
[data-testid="stChatInput"] button,
.stChatInput button {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
    border: none !important;
}
/* Chat input bottom bar area */
[data-testid="stBottom"],
[data-testid="stBottom"] > div,
[data-testid="stBottom"] > div > div,
[data-testid="stBottomBlockContainer"],
[data-testid="stBottomBlockContainer"] > div,
.stBottom, .stBottom > div {
    background-color: #0d1117 !important;
}
/* Metrics */
[data-testid="stMetric"] {
    background-color: #161b22 !important;
}
/* Popover */
[data-testid="stPopover"] > div {
    background-color: #161b22 !important;
}
/* Popover floating content panel */
[data-testid="stPopoverBody"],
[data-testid="stPopoverBody"] > div,
div[data-baseweb="popover"] > div {
    background-color: #161b22 !important;
    color: #c9d1d9 !important;
}
/* All text inside popover */
[data-testid="stPopoverBody"] p,
[data-testid="stPopoverBody"] span,
[data-testid="stPopoverBody"] code,
[data-testid="stPopoverBody"] small,
[data-testid="stPopoverBody"] li,
[data-testid="stPopoverBody"] label {
    color: #c9d1d9 !important;
}
[data-testid="stPopoverBody"] code {
    background-color: #30363d !important;
}
/* Info/alert boxes */
.stAlert, .stAlert > div,
[data-testid="stAlert"],
[data-testid="stNotification"] {
    background-color: #21262d !important;
    color: #c9d1d9 !important;
    border-color: #30363d !important;
}
.stAlert p, .stAlert span, .stAlert div,
[data-testid="stAlert"] p, [data-testid="stAlert"] span {
    color: #c9d1d9 !important;
}
/* Vega charts */
.vega-embed, .vega-embed canvas {
    background-color: transparent !important;
}
</style>
"""

LIGHT_CSS = """
<style>
:root {
    color-scheme: light;
}
/* Add borders to tertiary buttons in light mode */
button[kind="tertiary"],
[data-testid="stBaseButton-tertiary"] {
    border: 1px solid #d0d7de !important;
    padding: 0.4rem 0.8rem !important;
    margin: 0 4px !important;
}
button[kind="tertiary"]:hover,
[data-testid="stBaseButton-tertiary"]:hover {
    background-color: #f3f4f6 !important;
}
</style>
"""

if st.session_state.dark_mode:
    st.markdown(DARK_CSS, unsafe_allow_html=True)
else:
    st.markdown(LIGHT_CSS, unsafe_allow_html=True)

# Store theme in shared state for pages
st.session_state.shared["dark_mode"] = st.session_state.dark_mode

# ---------------------------------------------------------------------------
# Navigation — custom tab bar with theme toggle on the right
# ---------------------------------------------------------------------------
dashboard_page = st.Page("app_pages/dashboard.py", title="Logistics Dashboard", icon=":material/dashboard:")
assistant_page = st.Page("app_pages/ai_assistant.py", title="BI Assistant Data Analysis", icon=":material/smart_toy:")
builder_page = st.Page("app_pages/dashboard_builder.py", title="BI Assistant Dashboard Builder", icon=":material/dashboard_customize:")

# Load published dashboards as dynamic tabs
def _load_published_names():
    try:
        session = conn.session()
        rows = session.sql(
            "SELECT DISTINCT NAME FROM CONVERSATIONAL_BI_ASSISTANT.PUBLISHED_DASHBOARDS.DASHBOARDS "
            "ORDER BY NAME"
        ).collect()
        return [r["NAME"] for r in rows] if rows else []
    except Exception:
        return []

st.session_state.published_dashboards = _load_published_names()

published_pages = []
for pub_name in st.session_state.published_dashboards:
    published_pages.append(
        st.Page("app_pages/published_dashboard.py", title=pub_name,
                icon=":material/space_dashboard:",
                url_path=f"pub_{pub_name.lower().replace(' ', '_')}"))

all_pages = [assistant_page, builder_page, dashboard_page] + published_pages
page = st.navigation(all_pages, position="hidden")

# Render custom nav bar
nav_left, nav_spacer, nav_right = st.columns([6, 3, 1])
with nav_left:
    with st.container(horizontal=True):
        if st.button(":material/smart_toy: Data Analysis",
                     type="primary" if page == assistant_page else "tertiary",
                     key="nav_assistant"):
            st.switch_page(assistant_page)
        if st.button(":material/dashboard_customize: Dashboard Builder",
                     type="primary" if page == builder_page else "tertiary",
                     key="nav_builder"):
            st.switch_page(builder_page)
        # All dashboards dropdown
        with st.popover(":material/space_dashboard: My Dashboards"):
            if st.button("Logistics Dashboard", use_container_width=True,
                         key="nav_dashboard",
                         type="primary" if page == dashboard_page else "secondary"):
                st.switch_page(dashboard_page)
            for pub_page in published_pages:
                if st.button(pub_page.title, use_container_width=True,
                             key=f"nav_pub_{pub_page.title}",
                             type="primary" if page == pub_page else "secondary"):
                    st.query_params["dash"] = pub_page.title
                    st.switch_page(pub_page)
with nav_right:
    _t_icon = ":material/light_mode:" if st.session_state.dark_mode else ":material/dark_mode:"
    if st.button("", icon=_t_icon, type="tertiary", key="global_theme"):
        st.session_state.dark_mode = not st.session_state.dark_mode
        st.rerun()

# Set query params for published dashboards
if page in published_pages:
    st.query_params["dash"] = page.title

page.run()
