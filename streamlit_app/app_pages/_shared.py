"""Shared helpers for dashboard and ai_assistant pages."""
import json
import re

import streamlit as st


# ---------------------------------------------------------------------------
# Color utilities
# ---------------------------------------------------------------------------
def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


BRAND = hex_to_rgb("#418CF0")
COLORS = [hex_to_rgb(c) for c in
          ["#418CF0", "#E85D75", "#F6A623", "#4CAF50", "#9C27B0", "#00BCD4", "#FF5722"]]


# ---------------------------------------------------------------------------
# Vega-Lite theme config
# ---------------------------------------------------------------------------
def vega_config(dark=False):
    lbl = "#c9d1d9" if dark else "#333333"
    grd = "#30363d" if dark else "#e0e0e0"
    return {
        "axis": {
            "labelFontWeight": "bold", "labelFontSize": 11,
            "titleFontWeight": "bold", "titleFontSize": 12,
            "labelColor": lbl, "titleColor": lbl,
            "gridColor": grd, "domainColor": grd, "tickColor": grd,
        },
        "legend": {
            "labelFontWeight": "bold", "labelFontSize": 11,
            "titleFontWeight": "bold",
            "labelColor": lbl, "titleColor": lbl,
        },
        "header": {"labelFontWeight": "bold", "labelColor": lbl, "titleColor": lbl},
        "view": {"stroke": "transparent"},
        "background": "transparent",
    }


# ---------------------------------------------------------------------------
# Follow-up question extraction
# ---------------------------------------------------------------------------
FOLLOWUP_PATTERNS = [
    re.compile(r"(?:follow[- ]?up questions?.*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
    re.compile(r"(?:you (?:might|could|can|may) (?:also )?(?:explore|ask|try|consider).*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
    re.compile(r"(?:here are (?:some|a few).*?(?:questions?|options?|charts?|visualizations?|suggestions?|ideas?).*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
    re.compile(r"(?:questions?\s+(?:to|you\s+could)\s+explore.*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
    re.compile(r"(?:(?:additional|further|suggested|related)\s+(?:questions?|charts?|options?).*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
    re.compile(r"(?:want to (?:explore|know|ask).*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
    re.compile(r"(?:(?:some|a few)\s+(?:examples?|options?|charts?|ideas?).*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
    re.compile(r"(?:(?:try|consider|start with).*?:\s*\n)((?:\s*[-*\d.]+\s*.+\n?)+)", re.I),
]

_FOLLOWUP_KEYWORDS = (
    "question", "explore", "ask", "follow", "consider",
    "chart", "option", "suggestion", "idea", "try", "example",
    "visualization", "analysis", "here are",
)


def extract_followups(text):
    """Return (clean_text, [follow-up strings]).

    Works for both dashboard sidebar chat and the AI assistant tab.
    Uses pattern matching first, then falls back to trailing bulleted lists.
    """
    for pat in FOLLOWUP_PATTERNS:
        m = pat.search(text)
        if m:
            block = m.group(1)
            questions = []
            for line in block.strip().split("\n"):
                q = re.sub(r"^\s*[-*\d.)\]]+\s*", "", line).strip().strip("*_`\"'")
                if len(q) > 10:
                    questions.append(q)
            if questions:
                return text[:m.start()].rstrip(), questions
    # Fallback: trailing bulleted list
    lines = text.rstrip().split("\n")
    bullet_lines = []
    for line in reversed(lines):
        stripped = re.sub(r"^\s*[-*\d.)\]]+\s*", "", line).strip().strip("*_`\"'")
        is_bullet = line.strip()[:1] in ("-", "*") or (line.strip()[:1].isdigit())
        if is_bullet and len(stripped) > 10:
            bullet_lines.insert(0, stripped)
        elif bullet_lines and stripped:
            break
        elif not stripped:
            continue
    if len(bullet_lines) >= 2:
        cut = len(lines) - len(bullet_lines)
        while cut > 0 and not lines[cut - 1].strip():
            cut -= 1
        if cut > 0:
            prev = lines[cut - 1].strip().rstrip(":")
            if any(kw in prev.lower() for kw in _FOLLOWUP_KEYWORDS):
                cut -= 1
        return "\n".join(lines[:cut]).rstrip(), bullet_lines
    return text, []


def strip_sql_blocks(text):
    """Remove all ``` code blocks and standalone SQL statements from display text."""
    # Remove fenced code blocks
    cleaned = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)
    # Remove standalone SQL SELECT statements (lines starting with SELECT through semicolon or blank line)
    cleaned = re.sub(r"(?m)^SELECT\b.*?(?:;\s*$|\n\n)", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Base PDF 1.4 writer — shared primitives for dashboard and chat exports
# ---------------------------------------------------------------------------
class BasePDF:
    """Minimal PDF 1.4 writer with shared drawing primitives."""

    def __init__(self, width=612, height=792, margin=40):
        self._objs = []
        self._pages = []
        self._page_streams = []
        self._W = width
        self._H = height
        self._M = margin
        self._y = self._H - self._M
        self._new_page()

    # --- internals ---
    def _obj(self, data):
        self._objs.append(data)
        return len(self._objs)

    def _esc(self, t):
        return str(t).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def _cmd(self, s):
        self._page_streams.append(s)

    def _new_page(self):
        if self._page_streams:
            self._flush_page()
        self._y = self._H - self._M
        self._page_streams = []

    def _flush_page(self):
        raw = "\n".join(self._page_streams).encode("latin-1", errors="replace")
        oid = self._obj(f"<< /Length {len(raw)} >>\nstream\n".encode() + raw + b"\nendstream")
        self._pages.append(oid)
        self._page_streams = []

    def _check_space(self, needed):
        if self._y - needed < self._M:
            self._new_page()
            self.rect(0, self._H - 2, self._W, 2, *BRAND)
            self._y = self._H - self._M

    # --- primitives ---
    def rect(self, x, y, w, h, r, g, b, fill=True):
        op = "f" if fill else "S"
        self._cmd(f"{r:.3f} {g:.3f} {b:.3f} {'rg' if fill else 'RG'} {x:.1f} {y:.1f} {w:.1f} {h:.1f} re {op}")

    def text(self, x, y, t, size=10, bold=False, r=0.2, g=0.2, b=0.2):
        f = "/F2" if bold else "/F1"
        self._cmd(f"{r:.2f} {g:.2f} {b:.2f} rg BT {f} {size} Tf {x:.1f} {y:.1f} Td ({self._esc(t)}) Tj ET")

    def line(self, x1, y1, x2, y2, r=0.85, g=0.85, b=0.85, w=0.5):
        self._cmd(f"{r:.2f} {g:.2f} {b:.2f} RG {w} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def border(self, x, y, w, h, r=0.82, g=0.82, b=0.82, lw=0.6):
        self._cmd(f"{r:.2f} {g:.2f} {b:.2f} RG {lw} w {x:.1f} {y:.1f} {w:.1f} {h:.1f} re S")

    def title_bar(self, title, subtitle):
        self.rect(0, self._H - 44, self._W, 44, *BRAND)
        self.text(self._M, self._H - 28, title, size=16, bold=True, r=1, g=1, b=1)
        self.text(self._M, self._H - 40, subtitle, size=7, r=0.85, g=0.9, b=1)
        self._y = self._H - 50

    def section_label(self, x, y, w, title):
        self.rect(x, y - 14, w, 14, *BRAND)
        self.text(x + 5, y - 11, title, size=8, bold=True, r=1, g=1, b=1)

    # --- build final PDF bytes ---
    def build(self):
        if self._page_streams:
            self._flush_page()
        out = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
        f1 = self._obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        f2 = self._obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
        kids = []
        res = f"<< /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >>"
        for sid in self._pages:
            kids.append(self._obj(None))
        pages_id = self._obj(None)
        cat_id = self._obj(None)
        final = []
        for i, o in enumerate(self._objs):
            n = i + 1
            if n == pages_id:
                ks = " ".join(f"{k} 0 R" for k in kids)
                final.append(f"<< /Type /Pages /Kids [{ks}] /Count {len(kids)} >>".encode())
            elif n == cat_id:
                final.append(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
            elif n in kids:
                sid = self._pages[kids.index(n)]
                final.append(f"<< /Type /Page /Parent {pages_id} 0 R "
                             f"/MediaBox [0 0 {self._W} {self._H}] "
                             f"/Resources {res} /Contents {sid} 0 R >>".encode())
            else:
                final.append(o if isinstance(o, bytes) else o.encode() if isinstance(o, str) else o)
        body = b""
        offsets = []
        for i, od in enumerate(final):
            offsets.append(len(out[0]) + len(body))
            body += f"{i+1} 0 obj\n".encode() + od + b"\nendobj\n"
        out.append(body)
        xoff = len(out[0]) + len(out[1])
        xr = f"xref\n0 {len(final)+1}\n0000000000 65535 f \n"
        for o in offsets:
            xr += f"{o:010d} 00000 g \n"
        out.append(xr.encode())
        out.append(f"trailer\n<< /Size {len(final)+1} /Root {cat_id} 0 R >>\nstartxref\n{xoff}\n%%EOF\n".encode())
        return b"".join(out)


# ---------------------------------------------------------------------------
# LLM-based SQL generation for non-agent schemas
# ---------------------------------------------------------------------------
LOGISTICS_SCHEMA = "LOGISTICS_DATA"


@st.cache_data(ttl=300)
def get_schema_metadata(_conn, schema):
    """Fetch table and column metadata for a schema."""
    df = _conn.query(
        f"SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE "
        f"FROM CONVERSATIONAL_BI_ASSISTANT.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{schema}' "
        f"ORDER BY TABLE_NAME, ORDINAL_POSITION"
    )
    if df.empty:
        return ""
    lines = []
    current_table = None
    for _, row in df.iterrows():
        tbl = row["TABLE_NAME"]
        if tbl != current_table:
            current_table = tbl
            lines.append(f"\nTable: CONVERSATIONAL_BI_ASSISTANT.{schema}.{tbl}")
        lines.append(f"  - {row['COLUMN_NAME']} ({row['DATA_TYPE']})")
    return "\n".join(lines)


def llm_query(_conn, question, schema, metadata=None):
    """Use CORTEX.COMPLETE to answer a question about a given schema."""
    if metadata is None:
        metadata = get_schema_metadata(_conn, schema)
    if not metadata:
        return "No tables found in this schema."
    prompt = (
        f"You are a Snowflake SQL expert. The user wants to analyze data in schema "
        f"CONVERSATIONAL_BI_ASSISTANT.{schema}.\n\n"
        f"Available tables and columns:\n{metadata}\n\n"
        f"User question: {question}\n\n"
        f"Instructions:\n"
        f"- Answer the question directly and concisely.\n"
        f"- In explanations, refer to tables by their short name only (e.g. ORDERS, not the full path).\n"
        f"- If the question requires querying data, write a Snowflake SQL query.\n"
        f"- In SQL only, use fully qualified table names (CONVERSATIONAL_BI_ASSISTANT.{schema}.<TABLE>).\n"
        f"- Wrap any SQL in a ```sql code block.\n"
        f"- If the question is about listing tables, showing structure, or metadata, "
        f"answer from the schema info above. Do NOT generate any SQL for such questions.\n"
        f"- Only generate SQL when the user asks for actual data, aggregations, charts, or analysis.\n"
        f"- Keep queries simple — no CTEs unless necessary."
    )
    session = _conn.session()
    result = session.sql(
        "SELECT SNOWFLAKE.CORTEX.COMPLETE('llama3.1-70b', ?) AS resp",
        params=[prompt],
    ).collect()
    return result[0]["RESP"] if result else "No response from LLM."
