"""
Storage layer for confirmed (supplier, supplier_sku) -> Cash On Tab catalogue
item mappings, used by item_matcher.py so Kateryna is never asked to confirm
the same product twice.

Backed by Postgres (same DATABASE_URL as pension_store.py / invoice_store.py).

Three kinds of confirmed rows:
  matched     -- resolves to a real catalogue item (catalog_code set)
  skip        -- this line item should never be entered into מלАי at all
                 (e.g. a fee line, or a product she doesn't stock)
  new_pending -- a real product, but not yet in the Cash On Tab catalogue;
                 she needs to add it there herself before it can be matched.
                 Surfaced as a flagged/red item in the review UI until then.
"""
import os

import psycopg2
import psycopg2.extras


def _get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url)


# Confirmed by Kateryna directly (2026-08-16), from the first batch of
# ambiguous/unmatched line items surfaced by item_matcher.py across the
# 10 real sample invoices. Seeded on startup so nothing here gets asked
# again after this deploy.
_SEED_MAPPINGS = [
    # (supplier_domain, supplier_sku, action, catalog_code, catalog_name, notes)
    ("emi-1.com", "CW020002", "matched", "1080042", 'כלור טבליות 15 ק"ג', None),
    ("pavilion-spark.co.il", "110MAT5019BK", "matched", "7290114461469", "סט שטיחים שחור גומי NBR", None),
    ("pavilion-spark.co.il", "110TIRE8307", "matched", None, None,
     "ambiguous barcode chosen by Kateryna: 6923432583078 (over 6923432603078)"),
    ("pavilion-spark.co.il", "110FUNNEL109", "matched", None, None,
     "ambiguous barcode chosen by Kateryna: 7290104691638 (over 2026080308605)"),
    ("emi-1.com", "VC020008", "matched", "1080065", "פיה לשואב אבק", None),
    ("pavilion-spark.co.il", "VSNR14-PLS", "skip", None, None, "לא להכניס למלАי"),
    ("pavilion-spark.co.il", "FIX627-24S", "new_pending", None, 'מגב יחיד שטוח 24" ALL IN 1',
     "לא נמצא בקטלוג, מוצר אמיתי 24 אינץ' - להוסיף לקטלוג"),
    ("pavilion-spark.co.il", "110HOLD042", "new_pending", None, "מעמד לטלפון נייד קליפס למזגן SANKYO",
     "לא נמצא בקטלוג (יש רק מגנט, זה קליפס) - להוסיף לקטלוג"),
    ("emi-1.com", "CW010078", "matched", "1080018", 'ווקס אדום 210 ליטר',
     "invoice sku CW010078, catalog barcode is CW010077 (off-by-one) - confirmed by Kateryna"),
    ("moshaev-inv.com", "695-20", "new_pending", None, "Afb 23KG", "עדיין לא הוכנס למערכת - Kateryna תוסיף בעצמה"),
    ("oz-b-g.com", "DE207500500PowerWringer", "skip", None, None, "לא להכניס למערכת"),
]

# The barcode entry for 110TIRE8307 and 110FUNNEL109 needs the actual
# catalog_code, not just barcode -- filled in via _resolve_seed_codes()
# below by looking the barcode up in the catalogue at seed time, so this
# file doesn't have to hardcode item_matcher's internal codes.
_SEED_BARCODES = {
    "110TIRE8307": "6923432583078",
    "110FUNNEL109": "7290104691638",
}


def init_db():
    """Creates the item_mappings table if it doesn't exist yet, and seeds
    the confirmed mappings above (idempotent -- ON CONFLICT DO NOTHING).
    Safe to call on every startup."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS item_mappings (
                    id SERIAL PRIMARY KEY,
                    supplier_domain TEXT NOT NULL,
                    supplier_sku TEXT NOT NULL,
                    action TEXT NOT NULL,
                    catalog_code TEXT,
                    catalog_name TEXT,
                    notes TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (supplier_domain, supplier_sku)
                );
            """)
        conn.commit()

    _resolve_seed_codes()
    for domain, sku, action, code, name, notes in _SEED_MAPPINGS:
        upsert_mapping(domain, sku, action, catalog_code=code, catalog_name=name,
                        notes=notes, only_if_missing=True)


def _resolve_seed_codes():
    """Fills in catalog_code/catalog_name for the two ambiguous-barcode
    seed rows by looking the chosen barcode up in the catalogue, so we
    don't have to hardcode item_matcher's internal item codes here."""
    try:
        import item_matcher
        item_matcher._load()
        by_barcode = item_matcher._by_barcode
    except Exception:
        return
    for i, (domain, sku, action, code, name, notes) in enumerate(_SEED_MAPPINGS):
        if code is None and sku in _SEED_BARCODES:
            item = by_barcode.get(_SEED_BARCODES[sku])
            if item:
                _SEED_MAPPINGS[i] = (domain, sku, action, item["code"], item["name"], notes)


def get_mapping(supplier_domain: str, supplier_sku: str) -> dict:
    if not supplier_sku:
        return None
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM item_mappings WHERE supplier_domain=%s AND supplier_sku=%s",
                (supplier_domain, supplier_sku),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def get_mappings_for_supplier(supplier_domain: str) -> dict:
    """{supplier_sku: mapping_row} for every confirmed mapping under this
    supplier -- used to bulk-resolve a whole invoice's line items at once
    without one query per line."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM item_mappings WHERE supplier_domain=%s",
                (supplier_domain,),
            )
            rows = cur.fetchall()
    return {r["supplier_sku"]: dict(r) for r in rows}


def list_mappings() -> list:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM item_mappings ORDER BY supplier_domain, supplier_sku")
            return [dict(r) for r in cur.fetchall()]


def upsert_mapping(supplier_domain: str, supplier_sku: str, action: str,
                    catalog_code: str = None, catalog_name: str = None,
                    notes: str = None, only_if_missing: bool = False) -> int:
    """Confirms (or re-confirms) a mapping. `action` is one of
    "matched" / "skip" / "new_pending". When only_if_missing=True (used by
    the startup seed), an existing row is left untouched rather than
    overwritten -- so any correction Kateryna makes later in the UI sticks."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if only_if_missing:
                cur.execute("""
                    INSERT INTO item_mappings
                        (supplier_domain, supplier_sku, action, catalog_code, catalog_name, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (supplier_domain, supplier_sku) DO NOTHING
                    RETURNING id
                """, (supplier_domain, supplier_sku, action, catalog_code, catalog_name, notes))
            else:
                cur.execute("""
                    INSERT INTO item_mappings
                        (supplier_domain, supplier_sku, action, catalog_code, catalog_name, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (supplier_domain, supplier_sku) DO UPDATE SET
                        action=EXCLUDED.action, catalog_code=EXCLUDED.catalog_code,
                        catalog_name=EXCLUDED.catalog_name, notes=EXCLUDED.notes,
                        updated_at=now()
                    RETURNING id
                """, (supplier_domain, supplier_sku, action, catalog_code, catalog_name, notes))
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def update_mapping_by_id(mapping_id: int, action: str, catalog_code: str = None,
                          catalog_name: str = None, notes: str = None) -> bool:
    """Edits an existing mapping row in place (used by the review page --
    supplier_domain/supplier_sku are the identity of the row and are never
    changed here, only what it resolves to)."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE item_mappings
                SET action=%s, catalog_code=%s, catalog_name=%s, notes=%s, updated_at=now()
                WHERE id=%s
            """, (action, catalog_code, catalog_name, notes, mapping_id))
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def delete_mapping(mapping_id: int):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM item_mappings WHERE id=%s", (mapping_id,))
        conn.commit()
