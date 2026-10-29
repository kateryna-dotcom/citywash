"""
Invoices transcribed by hand from scans that have no text layer, so the
Gmail/PDF pipeline can't read them. import_manual_invoices() (run on
startup) adds each one to מלאי as its own needs_review card -- unless a
card for the same supplier + invoice number already exists, in which case
it's skipped. Idempotent.

Source: victoriascent "חשבוניות אוקטובר" scan (13 pages, 11 invoices,
"העתק משוחזר"), uploaded by Kateryna 2026-10-29. Every invoice's line sum
was checked against the סה"כ printed on it.
"""
import datetime

import companies
import invoice_store
import item_mapping_store
import item_matcher

ABT = "א.ב.ת. שירותי שטיפה בע\"מ"
UBR = "יו.בי.אר שטיפת רכב בע\"מ"

# Shared barcodes / descriptions used below.
AREON_X = "3800034958943"
AREON_MON = "3800034952330"
AREON_DRY = "3800034950824"
AREON_GEL = "3800034953979"
BOTTLE_35 = "3800034954129"
PEARLS = "3800034967600"
WILD = "3800034981446"
SPRAY_BLACK = ("3800034980708", "בקבוק ספרי 30 מ\"ל פרמיום שחור - Areon Spray 30 ml -Premium black")
GILDA = ("7290016750294", "ONE גילדה צהובה באיכות מעולה")
V002 = ("7290016750737", "ONECARE V002 - ניקוי ריפודים")
V003 = ("7290016750744", "ONECARE V003 - ניקוי צמיגים")
FL303 = ("6959375197799", "FLAMINGO 303 - נוזל ניקוי לוח מחוונים, פנים הרכב")
FL304 = ("6959375198000", "FLAMINGO 304 - מנקה עור")


def _x(name):
    return (AREON_X, f"AREON X עץ ריח - {name}")


def _mon(name):
    return (AREON_MON, f"AREON MON מוון עץ ריח - {name}")


# (invoice number, date, branch, company, [(barcode, description, qty, unit price)])
VICTORIA_OCT_2026 = [
    ("39466", "2026-10-11", "ראשון לציון", UBR, [
        ("7640186844079", "LITTLE JOE ליטל ג'ו כלבלב - MAUVE MAGIC", 6, 7.0),
        ("7640186844079", "LITTLE JOE ליטל ג'ו כלבלב - VANILLA", 6, 7.0),
        ("7290020367044", "ONE מגן שמש מהודר M - 2025", 10, 15.0),
        (*FL303, 12, 13.0),
        (*FL304, 12, 15.0),
    ]),
    ("39457", "2026-10-11", "באר יעקב", UBR, [
        ("3800034965217", "wips מגבונים עור", 6, 7.5),
        (*FL304, 12, 15.0),
        ("6959375198352", "FLAMINGO 201 - מיקרו מטלית איכותית פלמינגו 3 יח'", 12, 9.5),
        ("6959375199342", "FLAMINGO 003 - קצף לניקוי צמיגים 650 מ\"ל", 12, 14.0),
    ]),
    ("39328", "2026-10-04", "באר יעקב", UBR, [
        (*GILDA, 36, 10.0),
    ]),
    ("39500", "2026-10-12", "ירושלים", ABT, [
        (*SPRAY_BLACK, 24, 10.2),
        (*GILDA, 12, 10.0),
        (*V002, 12, 11.0),
        (*V003, 12, 12.0),
    ]),
    ("39358", "2026-10-05", "ירושלים", ABT, [
        (*_x("שחור"), 50, 2.0),
        (AREON_DRY, "AREON DRY עץ ריח יבש לתליה - קוקוס", 50, 2.0),
        (*FL303, 24, 13.0),
        (*FL304, 12, 15.0),
        ("7290020367501", "ONE כבל 2 מטר טעינה מהירה 3A 30W VQ-D54", 5, 15.0),
        ("7290020367556", "ONE כבל 1 מטר 3A טעינה מהיר סי טו סי VQ-D02 60W", 5, 13.0),
        ("7290020367600", "ONE כבל טעינה 2 מטר שחור 60W TYPE C TO C VQ-D022", 5, 15.0),
    ]),
    ("39695", "2026-10-22", "יבנה", ABT, [
        (*_x("מרכך כביסה חדש"), 50, 2.0),
        (*_mon("תות"), 50, 2.1),
        (*_mon("עור"), 50, 2.1),
        (*_mon("קוקוס"), 50, 2.1),
        ("3800034979818", "בקבוק ספרי 30 מ\"ל - מיקס Areon Spray 30 ml", 24, 9.5),
        (*SPRAY_BLACK, 24, 10.2),
        ("3800034967884", "AROMA BOX ארומה בקופסא - ונילה", 36, 14.5),
        (*V002, 24, 11.0),
    ]),
    ("39479", "2026-10-12", "חדרה", ABT, [
        (*_x("שחור"), 50, 2.0),
        (*_x("KISS נשיקה"), 30, 2.0),
        (*_x("צהוב"), 30, 2.0),
        (*_x("לבן"), 40, 2.0),
        (*_mon("בלק קריסטל"), 30, 2.1),
        (*_mon("תות"), 20, 2.1),
        (*_mon("דאבל בבל"), 30, 2.1),
        (*_mon("סילבר לנין"), 10, 2.1),
        (*_mon("ניו קאר"), 20, 2.1),
        (*_mon("אבטיח"), 20, 2.1),
        (*_mon("סאמר דרים"), 20, 2.1),
        (*_mon("קוקוס"), 20, 2.1),
        (AREON_DRY, "AREON DRY עץ ריח יבש לתליה - לימון", 20, 2.0),
        (AREON_DRY, "AREON DRY עץ ריח יבש לתליה - אורן", 20, 2.0),
        (AREON_GEL, "AREON GEL פחית ג'ל - בלק קריסטל", 12, 15.0),
        (AREON_GEL, "AREON GEL פחית ג'ל - תות", 12, 15.0),
        (AREON_GEL, "AREON GEL פחית ג'ל - ונילה", 12, 15.0),
        ("3800034978989", "AREON GEL LUX פחית ג'ל - גולד", 12, 15.0),
        (*V003, 12, 12.0),
    ]),
    ("39485", "2026-10-12", "באר שבע", ABT, [
        (AREON_GEL, "AREON GEL פחית ג'ל - אבטיח", 12, 15.0),
        (AREON_GEL, "AREON GEL פחית ג'ל - פאשון", 12, 15.0),
        ("3800034967181", "בושם 80 מ\"ל - מקלות X לרכב - מיקס", 24, 11.5),
        (*V002, 12, 11.0),
        (*V003, 24, 12.0),
    ]),
    ("39444", "2026-10-08", "נס ציונה", ABT, [
        (*_x("שחור"), 60, 2.0),
        (*_x("מרכך כביסה חדש"), 30, 2.0),
        (*_x("צהוב"), 30, 2.0),
        (*_x("כחול"), 30, 2.0),
        (*_mon("סילבר לנין"), 40, 2.1),
        (*_mon("דובדבן"), 40, 2.1),
        (*_mon("קוקוס"), 40, 2.1),
        ("3800034979382", "AREON GARDEN DRY SPA - עץ גרדן", 40, 3.5),
        (WILD, "WILD CUTE MONKEY - עץ ריח ווילד קוף מתוק", 40, 2.5),
        (WILD, "WILD BLACK CAT - עץ ריח ווילד חתול שחור", 40, 2.5),
        (WILD, "WILD PROFESSOR - עץ ריח ווילד קוף פרופסור", 40, 2.5),
        (BOTTLE_35, "בקבוק 35 מ\"ל - בלק קריסטל", 12, 9.5),
        (BOTTLE_35, "בקבוק 35 מ\"ל - מסטיק", 12, 9.5),
        (BOTTLE_35, "בקבוק 35 מ\"ל - ונילה", 12, 9.5),
        (BOTTLE_35, "בקבוק 35 מ\"ל - דובדבן", 12, 9.5),
        (PEARLS, "Areon Pearls שקית פנינים מפיץ ריח - בלק קריסטל", 12, 6.5),
        (PEARLS, "Areon Pearls שקית פנינים מפיץ ריח - ונילה בלק", 12, 6.5),
        (PEARLS, "Areon Pearls שקית פנינים מפיץ ריח - תות", 12, 6.5),
        (PEARLS, "Areon Pearls שקית פנינים מפיץ ריח - ניו קאר", 12, 6.5),
        (PEARLS, "Areon Pearls שקית פנינים מפיץ ריח - אבטיח", 12, 6.5),
        (PEARLS, "Areon Pearls שקית פנינים מפיץ ריח - ונילה", 12, 6.5),
        (*SPRAY_BLACK, 24, 10.2),
        ("3800034961677", "PERFUME 50 ML - בושם יוקרתי 50 מ\"ל - גולד", 24, 27.0),
        ("3800034967884", "AROMA BOX ארומה בקופסא - בלק קריסטל", 30, 14.5),
    ]),
    ("39701", "2026-10-22", "נחלים", ABT, [
        (*_x("שחור"), 50, 2.0),
        (*_x("צהוב"), 50, 2.0),
        (*_x("לבן"), 50, 2.0),
        (*_mon("דובדבן"), 50, 2.1),
        (AREON_DRY, "AREON DRY עץ ריח יבש לתליה - לימון", 50, 2.0),
        ("7290020367365", "ONE מפוח אוויר TF-40A1", 1, 129.0),
    ]),
    ("39374", "2026-10-06", "נחלים", ABT, [
        (*_x("שחור"), 50, 2.0),
        (*_x("צהוב"), 50, 2.0),
        (*_x("לבן"), 50, 2.0),
        (*_mon("מסטיק"), 50, 2.1),
        (*_mon("דובדבן"), 50, 2.1),
        (*GILDA, 12, 10.0),
        ("7290020367099", "ONE מגן שמש מהודר L - 2025", 5, 15.0),
        ("7290020367143", "ONE מגן שמש מהודר XL - 2025", 5, 15.0),
        (*V003, 12, 12.0),
    ]),
]

SUPPLIER = "victoriascent.co.il"


def _existing_numbers(supplier_domain: str) -> set:
    import invoice_ingest  # local: invoice_ingest imports a lot at module load
    out = set()
    for r in invoice_store.list_records(limit=5000):
        if r.get("supplier_domain") != supplier_domain:
            continue
        number = r.get("invoice_number") or invoice_ingest.guess_invoice_number(
            r.get("subject") or "", r.get("raw_text") or "")
        if number:
            out.add(number.strip())
    return out


def import_manual_invoices() -> dict:
    """Adds every transcribed invoice that isn't in מלאי yet. Returns
    {"added": [...numbers], "skipped": [...numbers already there]}."""
    existing = _existing_numbers(SUPPLIER)
    try:
        learned = item_mapping_store.get_mappings_for_supplier(SUPPLIER)
    except Exception:  # noqa: BLE001
        learned = {}
    added, skipped = [], []
    for number, date, branch, company, rows in VICTORIA_OCT_2026:
        key = f"manual:{SUPPLIER}:{number}"
        if number in existing or invoice_store.message_already_processed(key):
            skipped.append(number)
            continue
        items = [{
            "sku": barcode, "description": desc, "quantity": float(qty),
            "unit_price": price, "total": round(qty * price, 2), "confident": True,
        } for barcode, desc, qty, price in rows]
        try:
            items = item_matcher.match_invoice_items(items, learned)
        except Exception:  # noqa: BLE001
            pass
        invoice_store.create_record({
            "gmail_message_id": key,
            "supplier_domain": SUPPLIER,
            "sender_email": "ויקטוריה מוצרי ריח (סריקה)",
            "subject": f"חשבונית מס {number} - ויקטוריה מוצרי ריח (מסריקת חשבוניות אוקטובר)",
            "received_at": datetime.datetime.fromisoformat(date + "T12:00:00+03:00"),
            "pdf_filename": None,
            "raw_text": "הוזן ידנית מסריקה (העתק משוחזר) -- אין טקסט מקורי.",
            "branch": branch,
            "invoice_number": number,
            "company": company if company in companies.COMPANY_NAMES else None,
            "line_items": items,
            "status": "needs_review",
        })
        added.append(number)
    return {"added": added, "skipped": skipped}
