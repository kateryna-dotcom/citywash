"""
Cash On Tab "דו"ח נוכחות" (attendance report) PDF -> one row per shift.

The report is one block per employee ("שם עובד: ...  קוד עובד: ...")
followed by shift lines. In pdftotext -layout output a shift line reads
left-to-right as: exceptions, level-2, level-1, regular, total hours,
branch, register, report type, [exit], [entry], weekday letter, date.
When only one of exit/entry was punched, its column tells which: the entry
sits right next to the weekday letter, the exit further left.
"""
import re
import subprocess

_BIDI = re.compile("[\u200e\u200f\u202a-\u202e]")
_EMPLOYEE_RE = re.compile(r"שם עובד\s*:\s*(.+?)(?:\s{2,}|$)")
_CODE_RE = re.compile(r"קוד עובד\s*:?\s*(\d+)")
_SHIFT_RE = re.compile(
    r"^\s*(?:\d{1,3}:\d\d\s+){5}"          # exceptions .. total hours
    r"(?P<branch>.+?)\s+(?P<register>\d+)\s+(?P<type>\d+)"
    r"(?P<times>.*?)"
    r"(?P<day>[\u05d0-\u05ea])\s+(?P<date>\d\d/\d\d/\d{4})\s*$"
)
_TIME_RE = re.compile(r"\d\d:\d\d")
# A lone time this close to the weekday letter is the entry column.
_ENTRY_MAX_GAP = 6


def pdf_text(pdf_bytes: bytes) -> str:
    out = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf_bytes,
                         capture_output=True, timeout=60, check=True)
    return out.stdout.decode("utf-8", "replace")


def parse_text(text: str) -> list:
    rows, name, code = [], "", ""
    for raw in text.splitlines():
        line = _BIDI.sub("", raw)
        m = _EMPLOYEE_RE.search(line)
        if m:
            name = m.group(1).strip()
            c = _CODE_RE.search(line)
            code = c.group(1) if c else ""
            continue
        m = _SHIFT_RE.match(line)
        if not m:
            continue
        times = m.group("times")
        found = list(_TIME_RE.finditer(times))
        tin = tout = None
        if len(found) >= 2:
            tout, tin = found[0].group(), found[1].group()
        elif len(found) == 1:
            gap = len(times) - found[0].end()
            if gap <= _ENTRY_MAX_GAP:
                tin = found[0].group()
            else:
                tout = found[0].group()
        rows.append({
            "name": name, "code": code, "date": m.group("date"),
            "day": m.group("day"), "branch": re.sub(r"\s+", " ", m.group("branch")).strip(),
            "in": tin, "out": tout,
        })
    return rows


def parse_pdf(pdf_bytes: bytes) -> list:
    return parse_text(pdf_text(pdf_bytes))
