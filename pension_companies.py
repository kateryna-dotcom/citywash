"""
Fixed reference data for the redesigned פנסיה section: the group's legal
entities (with their ח.פ / תיק ניכויים) and the pension fund companies they
report to. Kateryna picks a company, then a fund, then fills in that one
cell's login/link details -- these lists drive both levels of navigation.
"""

COMPANIES = [
    {"name": "י.ב.י.אר שטיפת רכב בע\"מ", "hp": "516770773", "tik": "925569709"},
    {"name": "א.ב.י. שטיפת רכב בע\"מ", "hp": "516090289", "tik": "925510984"},
    {"name": "בי. יו .אייו שטיפת רכבים", "hp": "516975687", "tik": "918178393"},
    {"name": "א.א. רכב ורכש בע\"מ", "hp": "517118485", "tik": "951756964"},
    {"name": "ג'ייקובס שירותי שטיפה בע\"מ", "hp": "516774312", "tik": "951711605"},
    {"name": "רמי מרדכייב בע\"מ", "hp": "516518289", "tik": "924662489"},
    {"name": "א.ב.ת שירותי שטיפה בע\"מ", "hp": "514896737", "tik": "925448052"},
]

FUNDS = ["מיטב דש", "מנורה", "כלל", "אלטשולר", "מגדל", "פניקס", "הראל", "מור", "אנליסט"]
