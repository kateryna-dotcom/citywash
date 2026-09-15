"""
The legal entities (חברות) City Wash issues/receives invoices under --
distinct from suppliers.py (who City Wash buys from) and from
pension_companies.py (pension/insurance providers, unrelated domain).
List given by Kateryna 2026-09-15.

Used to: (1) best-effort auto-detect which company an invoice was billed
to, and (2) populate the company picker in the מלАי review UI so Kateryna
can assign or correct it in one click -- same two-part pattern as
branches.py's BRANCHES/detect_branch.
"""

# {name: (ח.פ, תיק ניכויים)}. Detection matches on ח.פ (see detect_company)
# since it's plain digits -- unlike the Hebrew company name, digits survive
# pypdf's RTL text extraction intact (confirmed against a real אмפייר אс
# sample: the exact ח.פ 514896737 printed on the invoice matches א.ב.ת.
# שירותי שטיפה בע"מ below character-for-character), so it's a far more
# reliable signal than trying to fuzzy-match the company name text itself.
COMPANIES = {
    "יו.בי.אר שטיפת רכב בע\"מ": ("516770773", "925569709"),
    "א.ב.י. שטיפת רכב בע\"מ": ("516090289", "925510984"),
    "בי. יו .אי. שטיפת רכבים": ("516975687", "918178393"),
    "א.א. רכב ורכש בע\"מ": ("517118485", "951756964"),
    "ג'יקובס שירותי שטיפה בע\"מ": ("516774312", "951711605"),
    "רמי מרדכיב שירותי שטיפה בע\"מ": ("516518289", "924662489"),
    "א.ב.ת. שירותי שטיפה בע\"מ": ("514896737", "925448052"),
}

# name -> ח.פ, for display/lookup convenience.
COMPANY_NAMES = list(COMPANIES.keys())

_HP_TO_COMPANY = {hp: name for name, (hp, _nikuim) in COMPANIES.items()}


def detect_company(text: str) -> str | None:
    """Best-effort guess at which City Wash company an invoice was billed
    to, by looking for one of the known ח.פ numbers anywhere in the
    extracted text. Always just a starting guess -- shown as an editable
    dropdown in the UI so Kateryna can fix it in one click if it's wrong
    or missing (e.g. a supplier whose invoice doesn't print the client's
    ח.פ at all)."""
    if not text:
        return None
    for hp, name in _HP_TO_COMPANY.items():
        if hp in text:
            return name
    return None
