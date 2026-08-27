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

# Cash On Tab's own ספק search sometimes doesn't match SUPPLIER_CASHONTAB_NAMES
# verbatim (e.g. the supplier is registered under a plain code, or a
# differently-spelled name) -- this overrides what cashontab_bot.py actually
# searches with, per domain, while SUPPLIER_CASHONTAB_NAMES above stays the
# readable name shown in the מלАי supplier filter.
SUPPLIER_CASHONTAB_SEARCH_OVERRIDES = {
    # Registered in Cash On Tab under supplier code 5, not under the name in
    # SUPPLIER_CASHONTAB_NAMES -- searching that name returned zero matches
    # live (confirmed by Kateryna 2026-08-27). Search by code instead.
    "victoriascent.co.il": "5",
}


def cashontab_search_value(domain):
    """What cashontab_bot.py should actually type into the ספק search box
    for this supplier domain -- an override (code or corrected spelling) if
    one's needed, otherwise the same name shown in the מלАי supplier
    filter."""
    return SUPPLIER_CASHONTAB_SEARCH_OVERRIDES.get(domain) or SUPPLIER_CASHONTAB_NAMES.get(domain)
