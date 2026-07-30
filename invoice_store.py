"""
Storage layer for the "מלАי" (inventory) section: incoming supplier
invoices detected in Gmail, their extracted text, and (once parsing is
tuned per-supplier) structured line items ready for review before entry
into Cash On Tab.

Backed by the same Postgres database as pension_store.py (DATABASE_URL).
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
    """Creates the invoice_records table if it doesn't exist yet. Safe to call on every startup."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS invoice_records (
                    id SERIAL PRIMARY KEY,
                    gmail_message_id TEXT UNIQUE NOT NULL,
                    supplier_domain TEXT,
                    sender_email TEXT,
                    subject TEXT,
                    received_at TIMESTAMPTZ,
                    pdf_filename TEXT,
                    raw_text TEXT,
                    branch TEXT,
                    invoice_number TEXT,
                    line_items JSONB,
                    status TEXT NOT NULL DEFAULT 'needs_review',
                    note TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
        conn.commit()


def message_already_processed(gmail_message_id: str) -> bool:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM invoice_records WHERE gmail_message_id=%s", (gmail_message_id,))
            return cur.fetchone() is not None


def create_record(fields: dict) -> int:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO invoice_records
                    (gmail_message_id, supplier_domain, sender_email, subject, received_at,
                     pdf_filename, raw_text, branch, invoice_number, line_items, status, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gmail_message_id) DO NOTHING
                RETURNING id
            """, (
                fields.get("gmail_message_id"),
                fields.get("supplier_domain"),
                fields.get("sender_email"),
                fields.get("subject"),
                fields.get("received_at"),
                fields.get("pdf_filename"),
                fields.get("raw_text"),
                fields.get("branch"),
                fields.get("invoice_number"),
                psycopg2.extras.Json(fields.get("line_items")) if fields.get("line_items") is not None else None,
                fields.get("status", "needs_review"),
                fields.get("note"),
            ))
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def list_records(branch: str = None, limit: int = 200) -> list:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if branch:
                cur.execute("""
                    SELECT * FROM invoice_records WHERE branch=%s
                    ORDER BY (status = 'needs_review') DESC, received_at DESC LIMIT %s
                """, (branch, limit))
            else:
                cur.execute("""
                    SELECT * FROM invoice_records
                    ORDER BY (status = 'needs_review') DESC, received_at DESC LIMIT %s
                """, (limit,))
            return [dict(r) for r in cur.fetchall()]


def list_branches() -> list:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT branch FROM invoice_records
                WHERE branch IS NOT NULL AND branch <> '' ORDER BY branch
            """)
            return [r[0] for r in cur.fetchall()]


def update_line_item(record_id: int, item_index: int, updates: dict):
    """Merges `updates` into line_items[item_index] for one invoice record
    (used e.g. to mark a low-confidence / price-mismatch item as resolved
    once Kateryna has fixed or confirmed it)."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT line_items FROM invoice_records WHERE id=%s", (record_id,))
            row = cur.fetchone()
            if not row or not row["line_items"]:
                return
            items = row["line_items"]
            if item_index < 0 or item_index >= len(items):
                return
            items[item_index].update(updates)
            cur.execute(
                "UPDATE invoice_records SET line_items=%s, updated_at=now() WHERE id=%s",
                (psycopg2.extras.Json(items), record_id),
            )
        conn.commit()


def update_branch(record_id: int, branch: str):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE invoice_records SET branch=%s, updated_at=now() WHERE id=%s",
                (branch, record_id),
            )
        conn.commit()


def update_status(record_id: int, status: str, note: str = None):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE invoice_records SET status=%s, note=%s, updated_at=now() WHERE id=%s
            """, (status, note, record_id))
        conn.commit()
