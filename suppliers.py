"""
Reference data collected for Phase 2 (auto-entry into Cash On Tab):
maps each tracked supplier email domain to the exact name that supplier is
registered under as a ספק inside Cash On Tab, so the future write-automation
can select the right one without asking Kateryna each time.

Not used by the Gmail-read/parse pipeline (Phase 1) -- gmail_client.py's
SUPPLIER_DOMAINS is the list that actually drives which emails get picked up.
"""

SUPPLIER_CASHONTAB_NAMES = {
    "hadarrosen.com": "הדר רוזן",
    "emi-1.com": "אי.אם.איי מערכות שטיפה",
    "moshaev-inv.com": "וואש סנטר",
    "oz-b-g.com": "עוז בת גלים",
    "pavilion-spark.co.il": "ביתן ספארק סחר בע\"מ",
    "victoriascent.co.il": "ויקטוריה מוצרי ריח בע\"מ",
}
