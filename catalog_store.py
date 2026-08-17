"""
Storage layer for the Cash On Tab product catalogue (used by item_matcher.py
to resolve invoice line items to real products).

Originally this catalogue was a static file (cashontab_catalog.json) baked
into the Docker image -- refreshing it meant a re-export, a git commit, and
a full redeploy. Kateryna wanted updates to show up immediately when she
adds a product in Cash On Tab, so the catalogue now lives in Postgres and
can be replaced live via /api/inventory/sync-catalog, no deploy needed.

Cash On Tab has no public API (checked 2026-08-17), so a sync is driven by
Kateryna's own already-logged-in browser session (Claude in Chrome reads
the exported product list -- never her password) rather than a background
job.

Backed by the same Postgres database as everything else (DATABASE_URL).
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


def init_db():
    """Creates the cashontab_catalog table if it doesn't exist yet. Safe to
    call on every startup. Does NOT seed it -- see item_matcher.py, which
    falls back to bundled cashontab_catalog.json until the first real sync."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cashontab_catalog (
                    code TEXT PRIMARY KEY,
                    barcode TEXT,
                    name TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_catalog_barcode ON cashontab_catalog (barcode);")
        conn.commit()


def get_catalog() -> list:
    """Full catalogue, or [] if it hasn't been synced into the DB yet
    (caller -- item_matcher.py -- falls back to the bundled JSON snapshot
    in that case)."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT code, barcode, name, active FROM cashontab_catalog")
            return [dict(r) for r in cur.fetchall()]


def last_synced_at():
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(synced_at) FROM cashontab_catalog")
            row = cur.fetchone()
    return row[0] if row else None


def replace_catalog(items: list) -> int:
    """Wipes and reloads the whole catalogue in one transaction -- used by
    a full sync (she just re-exported the product list from Cash On Tab).
    `items` is a list of {code, barcode, name, active}. Returns the number
    of rows loaded."""
    rows = [
        (
            str(it.get("code") or "").strip(),
            str(it.get("barcode") or "").strip() or None,
            str(it.get("name") or "").strip(),
            bool(it.get("active", True)),
        )
        for it in items
        if str(it.get("code") or "").strip() and str(it.get("name") or "").strip()
    ]
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cashontab_catalog")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO cashontab_catalog (code, barcode, name, active) VALUES %s",
                rows,
            )
        conn.commit()
    return len(rows)


def upsert_item(code: str, barcode: str, name: str, active: bool = True):
    """Adds/updates a single product -- used when she confirms a one-off
    new item that isn't worth a full re-sync for."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cashontab_catalog (code, barcode, name, active, synced_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (code) DO UPDATE SET
                    barcode=EXCLUDED.barcode, name=EXCLUDED.name,
                    active=EXCLUDED.active, synced_at=now()
            """, (code, barcode, name, active))
        conn.commit()
