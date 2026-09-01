"""
Ties gmail_client + invoice_store together: finds new supplier invoice
emails, downloads the PDF, extracts its text, parses out the branch name,
invoice number, and line items (product, quantity, price), and stores
everything in Postgres for review on the מלАי dashboard tab.

Per-supplier line-item parsing was built from real sample invoices
Kateryna provided (2 per supplier). Formats are consistent within a
supplier but may have edge cases we haven't seen yet, so every parsed item
carries a `confident` flag (quantity * unit_price reconciles with the
printed line total or not) and every invoice still keeps its full
raw_text -- nothing is silently mis-entered into Cash On Tab from this
step alone; Kateryna reviews on the dashboard before anything is entered.
"""
import io
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from pypdf import PdfReader

import branches
import gmail_client
import invoice_store
import item_matcher
import item_mapping_store

# hadarrosen sends one email per branch with the branch name in the
# subject, e.g. "חשבונית לסניף עכו" -> branch "עכו".
_BRANCH_SUBJECT_RE = re.compile(r"לסניף\s+(.+)$")

# Common Hebrew invoice-number label variants, e.g. "חשבונית מס 213044",
# "חשבונית מס' IN264002223", "מספר תעודה: 108745".
_INVOICE_NUMBER_RE = re.compile(
    r"(?:חשבונית\s*מס'?|מספר\s*תעודה|תעודה\s*מס'?)\s*[:\-]?\s*([A-Za-z0-9\-]+)"
)

_HEBREW_CHAR_RE = re.compile(r"[֐-׿]")


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Per-supplier line-item parsers.
# ---------------------------------------------------------------------------

# emi-1.com ("אי.אמ.איי מערכות שטיפה לרכב"): one line per item, columns
# (right-to-left as printed, so left-to-right in extracted text) total /
# unit_price ש"ח / qty יח' / description / sku / row#. Matched against
# pdftotext -layout output -- pypdf's plain extract_text() badly truncates
# this invoice's wide table (it was dropping half the description, which
# is why CW010078 used to come through unmatchable against the catalogue).
_EMI_LAYOUT_ROW_RE = re.compile(
    r'(?P<total>[\d,]+\.\d{2})\s*'
    r'(?P<unit_price>[\d,]+\.\d{2})\s*ש"ח\s*'
    r'(?P<qty>[\d,]+\.\d{2})\s*יח\'\s*'
    r'(?P<desc>.+?)\s*'
    r'(?P<sku>[A-Za-z]{1,3}\d{5,})\s+'
    r'(?P<row_num>\d+)\s*$',
    re.MULTILINE,
)

# Legacy pattern, built against pypdf's plain (often-truncated) extraction
# -- kept as a fallback for when pdf_bytes isn't available (e.g. re-parsing
# an invoice ingested before this fix) or pdftotext fails for some reason.
_EMI_ROW_RE = re.compile(
    r'(?P<total>[\d,]+\.\d{2})\s*ש"ח'
    r'(?P<unit_price>[\d,]+\.\d{2})\s*יח'
    r'(?P<qty>[\d,]+\.\d{2})\s*'
    r'(?P<middle>.*?)'
    r'(?P<sku>[A-Za-z]{1,3}\d{5,})\s+(?P<row_num>\d+)\s*$',
    re.MULTILINE,
)


def _parse_emi_layout(text: str) -> list:
    items = []
    for m in _EMI_LAYOUT_ROW_RE.finditer(text):
        qty, unit_price, total = _num(m.group("qty")), _num(m.group("unit_price")), _num(m.group("total"))
        confident = None not in (qty, unit_price, total) and abs(round(qty * unit_price, 2) - total) < 0.5
        items.append({
            "sku": m.group("sku"), "description": m.group("desc").strip(), "quantity": qty,
            "unit_price": unit_price, "total": total, "confident": confident,
        })
    return items


def parse_emi(text: str, pdf_bytes: bytes = None) -> list:
    if pdf_bytes:
        layout_text = _extract_pdf_text_layout(pdf_bytes)
        if layout_text:
            items = _parse_emi_layout(layout_text)
            if items:
                return items
    items = []
    for m in _EMI_ROW_RE.finditer(text):
        middle = m.group("middle")
        hm = _HEBREW_CHAR_RE.search(middle)
        desc = middle[hm.start():].strip() if hm else middle.strip()
        qty, unit_price, total = _num(m.group("qty")), _num(m.group("unit_price")), _num(m.group("total"))
        confident = None not in (qty, unit_price, total) and abs(round(qty * unit_price, 2) - total) < 0.5
        items.append({
            "sku": m.group("sku"), "description": desc, "quantity": qty,
            "unit_price": unit_price, "total": total, "confident": confident,
        })
    return items


# moshaev-inv.com ("מושייב השקעות" / חשבשבת "HashDoc" PDFs -- also used by
# oz-b-g.com "עוז בת גלים"), matched against pdftotext -layout output:
# one line per item, columns (right-to-left as printed) barcode / total /
# discount% / price / qty / name / sku. Name sometimes wraps onto its own
# following line (e.g. a size like "23KG") which pypdf's plain
# extract_text() lost entirely -- that's the "695-20 Afb 23KG" item that
# used to come through with no usable description at all.
_MOSHAEV_LAYOUT_ROW_RE = re.compile(
    r'^[ \t]*(?P<barcode>\d{13})\s+'
    r'(?P<total>[\d,]+\.\d{2})\s+'
    r'(?P<discount>[\d,]+\.\d{2})\s+'
    r'(?P<price>[\d,]+\.\d{2})\s+'
    r'(?P<qty>[\d,]+\.\d{2})\s+'
    r'(?P<desc>.+?)\s+'
    r'(?P<sku>[A-Za-z0-9][A-Za-z0-9\-]{3,})[ \t]*$',
    re.MULTILINE,
)
# Lines that mean "this isn't a wrapped description continuation" when
# peeking at the line right after a matched row.
_MOSHAEV_NOT_CONTINUATION_RE = re.compile(r'^\s*(\d{13}\s|יתרת|תאריך תשלום|סה"כ)')


def _consume_wrapped_continuation(text: str, match_end: int, desc: str) -> str:
    """Some suppliers' item names wrap onto a lone following line (no
    numbers, just the rest of the description, e.g. a size like "23KG" or
    a model name). If the line right after this row match looks like that
    (not a new data row, not blank, not a totals/footer line), fold it
    into the description."""
    after = text[match_end:match_end + 300].split("\n", 2)
    cont = after[1].strip() if len(after) > 1 else ""
    if cont and not _MOSHAEV_NOT_CONTINUATION_RE.match(cont):
        return f"{desc} {cont}".strip()
    return desc


def _parse_moshaev_layout(text: str) -> list:
    items = []
    for m in _MOSHAEV_LAYOUT_ROW_RE.finditer(text):
        qty, price, discount, total = (
            _num(m.group("qty")), _num(m.group("price")),
            _num(m.group("discount")), _num(m.group("total")),
        )
        expected = round(qty * price * (1 - (discount or 0) / 100), 2) if qty is not None and price is not None else None
        confident = expected is not None and total is not None and abs(expected - total) < 0.5
        desc = _consume_wrapped_continuation(text, m.end(), m.group("desc").strip())
        items.append({
            "sku": m.group("sku"), "description": desc,
            "quantity": qty, "unit_price": price, "discount_pct": discount,
            "total": total, "barcode": m.group("barcode"), "confident": confident,
        })
    return items


# Legacy line-by-line parser, built against pypdf's plain (often-truncated)
# extraction -- kept as a fallback for when pdf_bytes isn't available or
# pdftotext fails.
_MOSHAEV_NUM_LINE_RE = re.compile(
    r'^(?P<qty>\d+\.\d{2})(?P<price>[\d,]+\.\d{2})(?P<discount>\d+\.\d{2})(?P<total>[\d,]+\.\d{2})(?P<barcode>\d{13})\s*$'
)
# Same numeric block, but not anchored to the start of the line -- some
# suppliers put the item key + name + numbers all on a single line instead
# of the number block getting its own line.
_MOSHAEV_TRAILING_NUM_RE = re.compile(
    r'(?P<qty>\d+\.\d{2})(?P<price>[\d,]+\.\d{2})(?P<discount>\d+\.\d{2})(?P<total>[\d,]+\.\d{2})(?P<barcode>\d{13})\s*$'
)
# Key must be >= 4 chars so we don't misfire on short header fields like
# "1מחסן" (warehouse: 1) or "2סוכן" (agent: 2). Name can start in Hebrew
# (most items) or Latin (a few English-only product names).
_MOSHAEV_KEY_LINE_RE = re.compile(r'^(?P<key>[A-Za-z0-9][A-Za-z0-9\-]{3,}?)(?P<name_start>[^\d\s].*)$')


def parse_moshaev(text: str, pdf_bytes: bytes = None) -> list:
    if pdf_bytes:
        layout_text = _extract_pdf_text_layout(pdf_bytes)
        if layout_text:
            items = _parse_moshaev_layout(layout_text)
            if items:
                return items
    items = []
    pending_key = None
    pending_name_parts = []

    def close_item(num_match):
        nonlocal pending_key, pending_name_parts
        qty, price, discount, total = (
            _num(num_match.group("qty")), _num(num_match.group("price")),
            _num(num_match.group("discount")), _num(num_match.group("total")),
        )
        expected = round(qty * price * (1 - (discount or 0) / 100), 2) if qty is not None and price is not None else None
        confident = expected is not None and total is not None and abs(expected - total) < 0.5
        items.append({
            "sku": pending_key, "description": " ".join(pending_name_parts).strip(),
            "quantity": qty, "unit_price": price, "discount_pct": discount,
            "total": total, "barcode": num_match.group("barcode"), "confident": confident,
        })
        pending_key, pending_name_parts = None, []

    for line in text.split("\n"):
        line_stripped = line.strip()
        num_m = _MOSHAEV_NUM_LINE_RE.match(line_stripped)
        if num_m and pending_key:
            close_item(num_m)
            continue
        key_m = _MOSHAEV_KEY_LINE_RE.match(line_stripped)
        if key_m and not pending_key:
            pending_key = key_m.group("key")
            name_start = key_m.group("name_start")
            trailing_m = _MOSHAEV_TRAILING_NUM_RE.search(name_start)
            if trailing_m:
                # key + name + numbers all on one line -- close immediately.
                pending_name_parts = [name_start[:trailing_m.start()].strip()]
                close_item(trailing_m)
            else:
                pending_name_parts = [name_start.strip()]
        elif pending_key and line_stripped:
            pending_name_parts.append(line_stripped)
    return items


# oz-b-g.com ("עוז בת גלים") -- also חשבשבת-produced, matched against
# pdftotext -layout output. Two sub-templates seen on real invoices,
# distinguished by whether the header includes a "מס' סיריאלי" column:
#   no סיריאלי: ship-doc# / total / unit_price / qty / description / sku / row#
#      (this is the "DE207500500 PowerWringer" template -- its description
#      used to come through as an unusable fragment under pypdf)
#   with סיריאלי: ship-doc# / total / unit_price / qty / sku / description / row#
#      (sku sits before the description instead of after it)
_OZBG_LAYOUT_ROW_A_RE = re.compile(  # no סיריאלי column
    r'^[ \t]*(?P<ship>\d+)\s+'
    r'(?P<total>[\d,]+\.\d{2})\s+'
    r'(?P<price>[\d,]+\.\d{2})\s+'
    r'(?P<qty>[\d,]+\.\d{2})\s+'
    r'(?P<desc>.+?)\s+'
    r'(?P<sku>[A-Za-z0-9][A-Za-z0-9\-]{3,})\s+'
    r'(?P<row_num>\d{1,3})[ \t]*$',
    re.MULTILINE,
)
_OZBG_LAYOUT_ROW_B_RE = re.compile(  # with סיריאלי column
    r'^[ \t]*(?P<ship>\d+)\s+'
    r'(?P<total>[\d,]+\.\d{2})\s+'
    r'(?P<price>[\d,]+\.\d{2})\s+'
    r'(?P<qty>[\d,]+\.\d{2})\s+'
    r'(?P<sku>[A-Za-z0-9][A-Za-z0-9\-]{4,})\s*'
    r'(?P<desc>.+?)\s+'
    r'(?P<row_num>\d{1,3})[ \t]*$',
    re.MULTILINE,
)
_OZBG_NOT_CONTINUATION_RE = re.compile(r'^\s*(\d+\s|סה"כ|האחריות|יש צורך)')


def _parse_oz_bg_layout(text: str) -> list:
    row_re = _OZBG_LAYOUT_ROW_B_RE if "סיריאלי" in text else _OZBG_LAYOUT_ROW_A_RE
    items = []
    for m in row_re.finditer(text):
        qty, price, total = _num(m.group("qty")), _num(m.group("price")), _num(m.group("total"))
        confident = None not in (qty, price, total) and abs(round(qty * price, 2) - total) < 0.5
        desc = m.group("desc").strip()
        after = text[m.end():m.end() + 200].split("\n", 2)
        cont = after[1].strip() if len(after) > 1 else ""
        if cont and not _OZBG_NOT_CONTINUATION_RE.match(cont):
            desc = f"{desc} {cont}".strip()
        items.append({
            "sku": m.group("sku"), "description": desc,
            "quantity": qty, "unit_price": price, "total": total, "confident": confident,
        })
    return items


# Legacy line-by-line parser, built against pypdf's plain (often-truncated)
# extraction -- kept as a fallback for when pdf_bytes isn't available or
# pdftotext fails. Item key spans 1-2 short lines, then
# "<name><qty.XX><price.XX><total.XX><serial#>" closes the item.
_OZBG_CLOSE_RE = re.compile(
    r'^(?P<name>[^\d]*?)(?P<qty>\d+\.\d{2})(?P<price>[\d,]+\.\d{2})(?P<total>[\d,]+\.\d{2})\s+(?P<serial>\d+)\s*$'
)
_OZBG_HEADER_MARKER = "מפתח פריט"


def parse_oz_bat_galim(text: str, pdf_bytes: bytes = None) -> list:
    if pdf_bytes:
        layout_text = _extract_pdf_text_layout(pdf_bytes)
        if layout_text:
            items = _parse_oz_bg_layout(layout_text)
            if items:
                return items
    items = []
    key_lines = []
    seen_header = False
    for line in text.split("\n"):
        line_stripped = line.strip()
        if not seen_header:
            if _OZBG_HEADER_MARKER in line_stripped:
                seen_header = True
            continue
        if not line_stripped:
            continue
        m = _OZBG_CLOSE_RE.match(line_stripped)
        if m:
            qty, price, total = _num(m.group("qty")), _num(m.group("price")), _num(m.group("total"))
            confident = None not in (qty, price, total) and abs(round(qty * price, 2) - total) < 0.5
            code_parts = [l for l in key_lines if not _HEBREW_CHAR_RE.search(l)]
            name_parts = [l for l in key_lines if _HEBREW_CHAR_RE.search(l)]
            trailing_name = m.group("name").strip()
            if trailing_name:
                name_parts.append(trailing_name)
            items.append({
                "sku": "".join(code_parts) or "".join(key_lines),
                "description": " ".join(name_parts).strip(),
                "quantity": qty, "unit_price": price, "total": total, "confident": confident,
            })
            key_lines = []
        else:
            key_lines.append(line_stripped)
    return items


# hadarrosen.com (per-branch invoices; printed vendor name on the PDF
# varies, e.g. "ברקו סנטס"): "₪<total> ₪<price> <scent name><12-13 digit
# barcode> <qty> <description, may continue on next line>"
_HADARROSEN_ROW_RE = re.compile(
    r'₪(?P<total>[\d,]+\.\d{2})\s*'
    r'₪(?P<price>[\d,]+\.\d{2})\s*'
    r'(?P<scent>.*?)'
    r'(?P<barcode>\d{12,13})\s*'
    r'(?P<qty>\d+)\s*'
    r'(?P<desc>[^\n₪]+)'
)


def parse_hadarrosen(text: str, pdf_bytes: bytes = None) -> list:
    items = []
    for m in _HADARROSEN_ROW_RE.finditer(text):
        qty, price, total = _num(m.group("qty")), _num(m.group("price")), _num(m.group("total"))
        confident = None not in (qty, price, total) and abs(round(qty * price, 2) - total) < 0.5
        items.append({
            "sku": m.group("barcode"),
            "description": (m.group("desc").strip() + " " + m.group("scent").strip()).strip(),
            "quantity": qty, "unit_price": price, "total": total, "confident": confident,
        })
    if items:
        return items
    # Some hadarrosen invoices (e.g. wiper-blade orders) use a completely
    # different table layout -- no barcode column, and pypdf's plain
    # extract_text() actually DROPS the size/model text this layout has
    # (e.g. 'TITANIUM + "16'), because it can't follow this particular
    # table's column order. pdftotext -layout preserves columns properly
    # and gets the full description, so re-extract with that instead of
    # working from the (incomplete) text pypdf already gave us.
    if pdf_bytes:
        layout_text = _extract_pdf_text_layout(pdf_bytes)
        if layout_text:
            items = parse_hadarrosen_wipers(layout_text)
            if items:
                return items
    return parse_hadarrosen_wipers(text)


def _extract_pdf_text_layout(pdf_bytes: bytes) -> str:
    """Re-extracts a PDF's text via poppler's pdftotext -layout, which
    keeps table columns in their visual order -- pypdf's extract_text()
    sometimes drops or garbles tokens in wide/multi-column tables. Returns
    "" (never raises) if poppler isn't installed or extraction fails, so
    callers can fall back to the pypdf text."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(pdf_bytes)
            f.flush()
            result = subprocess.run(
                ["pdftotext", "-layout", f.name, "-"],
                capture_output=True, timeout=30,
            )
        if result.returncode != 0:
            return ""
        text = result.stdout.decode("utf-8", errors="replace")
        # Strip bidi embedding/mark control chars (LRM/RLM/LRE/RLE/PDF/
        # LRI/RLI/FSI/PDI) that pdftotext wraps RTL runs in -- invisible
        # but break substring/regex matching otherwise.
        return re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    except Exception:  # noqa: BLE001
        return ""


# hadarrosen.com, second layout seen on some invoices (e.g. wiper blades):
# columns (right-to-left as printed, so left-to-right in extracted text)
# are total, price, qty, description (may include an English model name
# like "TITANIUM + \"16"), item code, row#. Only matched against
# pdftotext -layout output (see parse_hadarrosen) -- the column spacing
# this regex relies on isn't there in pypdf's plain-text extraction.
_HADARROSEN_WIPER_ROW_RE = re.compile(
    r'(?P<total>[\d,]+\.\d{2})\s*₪\s*'
    r'(?P<price>[\d,]+\.\d{2})\s*₪\s*'
    r'(?P<qty>\d+)\s+'
    r'(?P<desc>.+?)\s+'
    r'(?P<code>\d{6,9})\s+'
    r'(?P<row>\d+)\s*$',
    re.MULTILINE,
)
_HADARROSEN_WIPER_HEADER = "מס' פריט"


def parse_hadarrosen_wipers(text: str) -> list:
    start_idx = text.find(_HADARROSEN_WIPER_HEADER)
    if start_idx == -1:
        return []
    end_m = re.search(r'סה"כ\s*:?\s*\d', text[start_idx:])
    end = start_idx + end_m.start() if end_m else len(text)
    section = text[start_idx:end]

    items = []
    for m in _HADARROSEN_WIPER_ROW_RE.finditer(section):
        qty, price, total = _num(m.group("qty")), _num(m.group("price")), _num(m.group("total"))
        confident = None not in (qty, price, total) and abs(round(qty * price, 2) - total) < 0.5
        items.append({
            "sku": m.group("code"),
            "description": m.group("desc").strip(),
            "quantity": qty, "unit_price": price, "total": total, "confident": confident,
        })
    return items


# pavilion-spark.co.il ("ביתן ספארק סחר בע"מ" -- Priority-software invoices),
# matched against pdftotext -layout output: one line per item, columns
# (right-to-left as printed) total / net_price שח / discount% / gross_price
# שח / qty יח / description / sku+document-ref / row#. Reconciles as
# qty * net_price == total (net_price already has the discount applied).
_PAVILION_LAYOUT_ROW_RE = re.compile(
    r'(?P<total>[\d,]+\.\d{2})\s*'
    r'(?P<net_price>[\d,]+\.\d{2})\s*שח\s*'
    r'(?P<discount>[\d,]+\.\d{2})%\s*'
    r'(?P<gross_price>[\d,]+\.\d{2})\s*שח\s*'
    r'(?P<qty>\d+)\s*יח\s*'
    r'(?P<desc>.+?)\s+'
    r'(?P<sku>[A-Za-z0-9][A-Za-z0-9\-]{2,})\s+SH\d+\s+'
    r'(?P<row_num>\d+)\s*$',
    re.MULTILINE,
)
_PAVILION_INVOICE_RE = re.compile(r'SI\d{6,}')


def _parse_pavilion_layout(text: str) -> list:
    items = []
    for m in _PAVILION_LAYOUT_ROW_RE.finditer(text):
        qty, net, total = _num(m.group("qty")), _num(m.group("net_price")), _num(m.group("total"))
        confident = None not in (qty, net, total) and abs(round(qty * net, 2) - total) < 0.5
        items.append({
            "sku": m.group("sku"), "description": m.group("desc").strip(),
            "quantity": qty, "unit_price": net, "total": total, "confident": confident,
        })
    return items


# Legacy pattern, built against pypdf's plain (often-truncated) extraction
# -- kept as a fallback for when pdf_bytes isn't available or pdftotext
# fails. Structurally near-identical to the layout regex above; pavilion's
# invoices happened to hold up reasonably well under pypdf, but keeping
# this separate avoids re-verifying that assumption under time pressure.
_PAVILION_ROW_RE = re.compile(
    r'(?P<total>[\d,]+\.\d{2})\s*שח'
    r'(?P<net_price>[\d,]+\.\d{2})\s*'
    r'(?P<discount>[\d,]+\.\d{2})%\)?\s*שח'
    r'(?P<gross_price>[\d,]+\.\d{2})\s*יח'
    r'(?P<qty>\d+)\s*'
    r'(?P<desc>.*?)'
    r'(?P<sku>[A-Za-z0-9][A-Za-z0-9\-]{2,})\s+SH\d+\s+\d+\s*$',
    re.MULTILINE,
)


def parse_pavilion_spark(text: str, pdf_bytes: bytes = None) -> list:
    if pdf_bytes:
        layout_text = _extract_pdf_text_layout(pdf_bytes)
        if layout_text:
            items = _parse_pavilion_layout(layout_text)
            if items:
                return items
    items = []
    for m in _PAVILION_ROW_RE.finditer(text):
        qty, net, total = _num(m.group("qty")), _num(m.group("net_price")), _num(m.group("total"))
        confident = None not in (qty, net, total) and abs(round(qty * net, 2) - total) < 0.5
        items.append({
            "sku": m.group("sku"), "description": m.group("desc").strip(),
            "quantity": qty, "unit_price": net, "total": total, "confident": confident,
        })
    return items


# victoriascent.co.il ("ויקטוריה מוצרי ריח בע"מ" -- also חשבשבת). Under
# pdftotext -layout the column order (as it appears in the raw text stream)
# is: total, discount%, "ש"ח", price (3 decimals), qty, description, then
# the 13-digit barcode (which doubles as this supplier's sku -- Victoria's
# invoices don't carry a separate alpha sku). Some descriptions wrap onto a
# lone following line (e.g. "Spray 30 ml -Premium black").
_VICTORIA_LAYOUT_ROW_RE = re.compile(
    r'(?P<total>[\d,]+\.\d{2})[ \t]+'
    r'(?P<discount>[\d,]+\.\d{2})[ \t]+'
    r'ש"ח[ \t]+'
    r'(?P<price>[\d,]+\.\d{3})[ \t]+'
    r'(?P<qty>[\d,]+\.\d{2})[ \t]+'
    r'(?P<desc>.+?)[ \t]+'
    r'(?P<barcode>\d{12,13})[ \t]*$',
    re.MULTILINE,
)
# A continuation line is a bare description fragment. Anything starting
# with a barcode, a totals/footer marker, or a "total" amount (i.e. the
# first column of the NEXT data row) is not a continuation.
_VICTORIA_NOT_CONTINUATION_RE = re.compile(r'^\s*(\d{12,13}|סה"כ|תאריך|הנחה|\d+\s*%|[\d,]+\.\d{2}\s)')


def _parse_victoria_layout(text: str) -> list:
    items = []
    for m in _VICTORIA_LAYOUT_ROW_RE.finditer(text):
        qty, price, total = _num(m.group("qty")), _num(m.group("price")), _num(m.group("total"))
        confident = None not in (qty, price, total) and abs(round(qty * price, 2) - total) < 0.5
        desc = m.group("desc").strip()
        after = text[m.end():m.end() + 200].split("\n", 2)
        cont = after[1].strip() if len(after) > 1 else ""
        if cont and not _VICTORIA_NOT_CONTINUATION_RE.match(cont):
            desc = f"{desc} {cont}".strip()
        items.append({
            "sku": m.group("barcode"), "description": desc,
            "quantity": qty, "unit_price": price, "total": total, "confident": confident,
        })
    return items


# Legacy pattern, built against pypdf's plain extraction -- kept as a
# fallback for when pdf_bytes isn't available.
_VICTORIA_ROW_RE = re.compile(
    r'(?P<name>.+?)'
    r'(?P<qty>-?\d+\.\d{2})'
    r'(?P<price>\d+\.\d{3})'
    r'ש"ח'
    r'(?P<discount>\d+\.\d{2})'
    r'(?P<total>\d+\.\d{2})\s*'
    r'(?P<barcode>\d{12,13})',
    re.DOTALL,
)


def parse_victoria_scent(text: str, pdf_bytes: bytes = None) -> list:
    if pdf_bytes:
        layout_text = _extract_pdf_text_layout(pdf_bytes)
        if layout_text:
            items = _parse_victoria_layout(layout_text)
            if items:
                return items

    start_marker = "בר קוד"
    start_idx = text.find(start_marker)
    start = start_idx + len(start_marker) if start_idx != -1 else 0
    end_m = re.search(r'סה"כ\d', text[start:])
    end = start + end_m.start() if end_m else len(text)
    section = text[start:end]

    items = []
    for m in _VICTORIA_ROW_RE.finditer(section):
        qty, price, total = _num(m.group("qty")), _num(m.group("price")), _num(m.group("total"))
        confident = None not in (qty, price, total) and abs(round(abs(qty) * price, 2) - total) < 0.5
        items.append({
            "sku": m.group("barcode"), "description": m.group("name").strip().replace("\n", " "),
            "quantity": qty, "unit_price": price, "total": total, "confident": confident,
        })
    return items


# grow.security ("אמפייר אס" -- e-invoices sent via the Grow billing
# platform). Under a layout-preserving extraction the column order (as it
# appears in the raw text stream) is: total, price (both "₪"-prefixed),
# description (פירוט), qty, then מק"ט -- but built from the one real sample
# seen so far (invoice 3741) the מק"ט cell is genuinely blank; this
# supplier's invoices don't print a product code at all (confirmed by
# Kateryna 2026-09-01), so sku always comes back None here and these items
# rely on the review UI's name-search matching instead of a learned
# per-sku mapping. No legacy (pypdf plain-extract, no layout mode) fallback:
# unlike the other suppliers, pypdf's plain extraction reverses this PDF's
# Hebrew character-by-character in unpredictable fragment lengths (not just
# per-line), which no regex here could reliably parse -- a layout-preserving
# extraction is required. Column gaps can be many spaces wide (layout mode
# pads to align columns), hence the liberal [ \t]+ and the whitespace
# collapse on desc below.
_GROW_LAYOUT_ROW_RE = re.compile(
    r'₪\s*(?P<total>[\d,]+\.\d{2})[ \t]+'
    r'₪\s*(?P<price>[\d,]+\.\d{2})[ \t]+'
    r'(?P<desc>.+?)[ \t]+'
    r'(?P<qty>\d+)'
    r'(?:[ \t]+(?P<sku>\S+))?'
    r'[ \t]*$',
    re.MULTILINE,
)


def _parse_grow_layout(text: str) -> list:
    items = []
    for m in _GROW_LAYOUT_ROW_RE.finditer(text):
        qty, price, total = _num(m.group("qty")), _num(m.group("price")), _num(m.group("total"))
        confident = None not in (qty, price, total) and abs(round(qty * price, 2) - total) < 0.5
        items.append({
            "sku": m.group("sku"), "description": re.sub(r"\s+", " ", m.group("desc")).strip(),
            "quantity": qty, "unit_price": price, "total": total, "confident": confident,
        })
    return items


def parse_grow_security(text: str, pdf_bytes: bytes = None) -> list:
    if not pdf_bytes:
        return []
    # Primary: pypdf's own layout-preserving extraction -- pure Python, no
    # dependency on the pdftotext binary actually being reachable/working
    # in whatever container this runs in (unlike every other supplier's
    # parser here, which shells out via _extract_pdf_text_layout).
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        layout_text = "\n".join((page.extract_text(extraction_mode="layout") or "") for page in reader.pages)
    except Exception:  # noqa: BLE001
        layout_text = ""
    if layout_text:
        items = _parse_grow_layout(layout_text)
        if items:
            return items
    # Fallback: the pdftotext -layout subprocess other suppliers use, in
    # case a future אמפייר אס invoice renders differently under pypdf's
    # layout mode than it does under poppler's.
    layout_text = _extract_pdf_text_layout(pdf_bytes)
    if layout_text:
        return _parse_grow_layout(layout_text)
    return []


_LINE_ITEM_PARSERS = {
    "emi-1.com": parse_emi,
    "moshaev-inv.com": parse_moshaev,
    "oz-b-g.com": parse_oz_bat_galim,
    "hadarrosen.com": parse_hadarrosen,
    "pavilion-spark.co.il": parse_pavilion_spark,
    "victoriascent.co.il": parse_victoria_scent,
    "grow.security": parse_grow_security,
}


def parse_line_items(supplier_domain: str, text: str, pdf_bytes: bytes = None) -> list:
    parser = _LINE_ITEM_PARSERS.get(supplier_domain)
    if not parser or not text:
        return []
    try:
        # Only parse_hadarrosen currently accepts pdf_bytes (to re-extract
        # via pdftotext -layout when needed) -- other parsers' signatures
        # don't take it, so fall back to the text-only call for those.
        items = parser(text, pdf_bytes=pdf_bytes) if pdf_bytes is not None else parser(text)
    except TypeError:
        items = parser(text)
    except Exception:  # noqa: BLE001
        return []
    return _match_to_catalog(supplier_domain, items)


def _match_to_catalog(supplier_domain: str, items: list) -> list:
    """Enriches each parsed line item with its Cash On Tab catalogue match
    (matched_code/matched_name/match_source/needs_confirmation), using any
    mapping Kateryna has already confirmed for this supplier so the same
    item is never asked about twice. Doesn't fail the whole invoice if the
    catalogue or mapping table isn't reachable -- items just come back
    unmatched, still reviewable by hand."""
    if not items:
        return items
    try:
        learned = item_mapping_store.get_mappings_for_supplier(supplier_domain)
    except Exception:  # noqa: BLE001
        learned = {}
    try:
        return item_matcher.match_invoice_items(items, learned)
    except Exception:  # noqa: BLE001
        return items


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:  # noqa: BLE001
        return f"[could not extract text: {e}]"


def _guess_branch(subject: str, text: str) -> str:
    # hadarrosen sends one email per branch with the branch name spelled
    # out in the subject -- that's the most reliable signal when present.
    m = _BRANCH_SUBJECT_RE.search(subject or "")
    if m:
        return m.group(1).strip()
    # Otherwise, best-effort match against the canonical branch list inside
    # the invoice text itself (e.g. moshaev/oz-b-g PDFs print the branch
    # name on the "לכבוד" / bill-to line). Just a starting guess -- always
    # shown as an editable dropdown in the review UI so it can be corrected
    # in one click if wrong or missing.
    return branches.detect_branch(text) or ""


def _guess_invoice_number(subject: str, text: str) -> str:
    for source in (subject or "", text or ""):
        m = _INVOICE_NUMBER_RE.search(source)
        if m:
            return m.group(1).strip()
        # pavilion-spark (Priority software) prints the invoice number as
        # "SI<digits>" with no "חשבונית מס" label nearby it in the
        # extracted text order -- catch it as a fallback.
        m = _PAVILION_INVOICE_RE.search(source)
        if m:
            return m.group(0)
    return ""


def process_new_invoices(lookback_days: int = 30) -> dict:
    """Finds supplier invoice emails, downloads + extracts each new PDF, and
    stores a review-ready record. Returns a small summary dict."""
    after = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    candidates = gmail_client.list_new_invoice_messages(after=after)
    created = 0
    skipped = 0
    errors = []

    for stub in candidates:
        message_id = stub["id"]
        if invoice_store.message_already_processed(message_id):
            skipped += 1
            continue
        try:
            message = gmail_client.get_message(message_id)
            sender = gmail_client.extract_header(message, "From")
            subject = gmail_client.extract_header(message, "Subject")
            date_header = gmail_client.extract_header(message, "Date")
            try:
                received_at = parsedate_to_datetime(date_header)
            except Exception:  # noqa: BLE001
                received_at = None

            supplier_domain = gmail_client.supplier_domain_for(sender)
            pdf_attachments = gmail_client.find_pdf_attachments(message)

            if not pdf_attachments:
                # Nothing to extract, but still record it so it doesn't get
                # re-scanned every run.
                invoice_store.create_record({
                    "gmail_message_id": message_id,
                    "supplier_domain": supplier_domain,
                    "sender_email": sender,
                    "subject": subject,
                    "received_at": received_at,
                    "pdf_filename": None,
                    "raw_text": "",
                    "branch": _guess_branch(subject, ""),
                    "invoice_number": _guess_invoice_number(subject, ""),
                    "line_items": None,
                    "status": "no_pdf_found",
                })
                created += 1
                continue

            # Most of these emails have exactly one invoice PDF; if there's
            # more than one, process the first and note the rest in the text.
            first = pdf_attachments[0]
            pdf_bytes = gmail_client.get_attachment_bytes(message_id, first["attachmentId"])
            text = _extract_pdf_text(pdf_bytes)
            line_items = parse_line_items(supplier_domain, text, pdf_bytes=pdf_bytes)

            invoice_store.create_record({
                "gmail_message_id": message_id,
                "supplier_domain": supplier_domain,
                "sender_email": sender,
                "subject": subject,
                "received_at": received_at,
                "pdf_filename": first["filename"],
                "raw_text": text,
                "pdf_data": pdf_bytes,
                "branch": _guess_branch(subject, text),
                "invoice_number": _guess_invoice_number(subject, text),
                "line_items": line_items or None,
                "status": "needs_review",
            })
            created += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{message_id}: {e}")

    return {"created": created, "skipped": skipped, "errors": errors}
