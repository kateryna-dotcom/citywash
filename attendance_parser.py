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
    r"^\s*(?P<exc>\d{1,3}:\d\d)\s+(?P<l2>\d{1,3}:\d\d)\s+(?P<l1>\d{1,3}:\d\d)\s+"
    r"(?P<reg>\d{1,3}:\d\d)\s+(?P<total>\d{1,3}:\d\d)\s+"
    r"(?P<branch>.+?)\s+(?P<register>\d+)\s+(?P<type>\d+)"
    r"(?P<times>.*?)"
    r"(?P<day>[\u05d0-\u05ea])\s+(?P<date>\d\d/\d\d/\d{4})\s*$"
)
_TIME_RE = re.compile(r"\d\d:\d\d")
# Per-employee summary: "<n>  סה"כ ימי נוכחות א-ה / שישי / שבת".
_DAYS_RE = re.compile(r"(\d+)\s+סה\"כ ימי נוכחות")
# The summary's hour totals ("סה"כ שעות" line) under the column titles
# "חריגות / רמה 2 / רמה 1 / רגילות"; empty columns are left blank, so each
# value is assigned to the nearest title by position. Cash On Tab counts
# seconds, so these are a little more exact than summing the shift lines.
_SUMMARY_COLS = (("exceptions", "חריגות"), ("level2", "רמה 2"), ("level1", "רמה 1"), ("regular", "רגילות"))
_HOURS_RE = re.compile(r"\d{1,3}:\d\d")
# A lone time this close to the weekday letter is the entry column.
_ENTRY_MAX_GAP = 6


def pdf_text(pdf_bytes: bytes) -> str:
    out = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf_bytes,
                         capture_output=True, timeout=60, check=True)
    return out.stdout.decode("utf-8", "replace")


def parse_text(text: str) -> list:
    rows, name, code = [], "", ""
    days, totals, cols = {}, {}, None
    for raw in text.splitlines():
        line = _BIDI.sub("", raw)
        if "סיכום לדו\"ח" in line:      # whole-report totals: not an employee
            name, code = "", ""
            continue
        if all(t in line for _, t in _SUMMARY_COLS) and "סה\"כ שעות" not in line:
            cols = [(k, line.index(t) + len(t) / 2) for k, t in _SUMMARY_COLS]
            continue
        if cols and name and line.strip().endswith("סה\"כ שעות"):
            tot = {}
            for m in _HOURS_RE.finditer(line):
                center = (m.start() + m.end()) / 2
                key, pos = min(cols, key=lambda c: abs(c[1] - center))
                if abs(pos - center) < 9:
                    tot[key] = _minutes(m.group())
            totals[(name, code)] = tot
            cols = None
            continue
        d = _DAYS_RE.search(line)
        if d and name:
            days[(name, code)] = days.get((name, code), 0) + int(d.group(1))
            continue
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
            "regular": _minutes(m.group("reg")), "level1": _minutes(m.group("l1")),
            "level2": _minutes(m.group("l2")), "exceptions": _minutes(m.group("exc")),
        })
    return rows, days, totals


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def employees(rows: list, days: dict, totals: dict = None) -> list:
    """Totals per employee for the monthly sheet: hours at 100% (regular),
    125% (level 1) and 150% (level 2) in decimal hours, attendance days as
    Cash On Tab counts them, and the branch with most shifts."""
    out = {}
    for r in rows:
        e = out.setdefault((r["name"], r["code"]), {
            "name": r["name"], "code": r["code"], "regular": 0, "level1": 0,
            "level2": 0, "exceptions": 0, "branches": {}})
        for k in ("regular", "level1", "level2", "exceptions"):
            e[k] += r[k]
        e["branches"][r["branch"]] = e["branches"].get(r["branch"], 0) + 1
    result = []
    for key, e in out.items():
        if totals and key in totals:
            for k in ("regular", "level1", "level2", "exceptions"):
                e[k] = totals[key].get(k, 0)
        result.append({
            "name": e["name"], "code": e["code"],
            "h100": round(e["regular"] / 60, 2), "h125": round(e["level1"] / 60, 2),
            "h150": round(e["level2"] / 60, 2), "exceptions": round(e["exceptions"] / 60, 2),
            "days": days.get(key, len({r["date"] for r in rows if (r["name"], r["code"]) == key})),
            "branch": max(e["branches"], key=e["branches"].get),
        })
    return result


def parse_pdf(pdf_bytes: bytes) -> dict:
    rows, days, totals = parse_text(pdf_text(pdf_bytes))
    return {"rows": rows, "employees": employees(rows, days, totals)}
