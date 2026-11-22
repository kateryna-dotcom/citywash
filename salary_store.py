"""
Monthly salary work sheets for the שכר tab -- the table Kateryna keeps per
company and month (one block per branch: employee, ת.ז, rate, hours at
100/125/150%, days, travel, bonus, advance, notes). Stored as one JSON
document per (company, month) so the sheet's columns can evolve without
schema changes. Same Postgres database as invoice_store.py.
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
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS salary_sheets (
                    company TEXT NOT NULL,
                    month TEXT NOT NULL,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (company, month)
                );
            """)
        conn.commit()


def get_sheet(company: str, month: str):
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT data, updated_at FROM salary_sheets WHERE company=%s AND month=%s",
                        (company, month))
            row = cur.fetchone()
    return dict(row) if row else None


def list_months(company: str) -> list:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT month FROM salary_sheets WHERE company=%s ORDER BY month", (company,))
            return [r[0] for r in cur.fetchall()]


def save_sheet(company: str, month: str, data: dict):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO salary_sheets (company, month, data) VALUES (%s, %s, %s)
                ON CONFLICT (company, month) DO UPDATE SET data=EXCLUDED.data, updated_at=now()
            """, (company, month, psycopg2.extras.Json(data)))
        conn.commit()


def seed_sheet(company: str, month: str, data: dict) -> bool:
    """Insert a sheet only if that month has nothing saved yet."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO salary_sheets (company, month, data) VALUES (%s, %s, %s)
                ON CONFLICT (company, month) DO NOTHING
            """, (company, month, psycopg2.extras.Json(data)))
            inserted = cur.rowcount == 1
        conn.commit()
    return inserted


def move_loan_notes(company: str) -> int:
    """Move "הלוואה" parts of a row's notes into its own `loan` field
    (sheets saved before the loan column existed). Idempotent."""
    moved = 0
    for month in list_months(company):
        sheet = get_sheet(company, month)
        data = (sheet or {}).get("data") or {}
        changed = False
        for rows in (data.get("branches") or {}).values():
            for row in rows:
                if row.get("loan") or "הלוואה" not in (row.get("notes") or ""):
                    continue
                parts = [p.strip() for p in row["notes"].split("|")]
                loan = [p for p in parts if "הלוואה" in p]
                rest = [p for p in parts if "הלוואה" not in p and p]
                row["loan"] = " | ".join(loan)
                if rest:
                    row["notes"] = " | ".join(rest)
                else:
                    row.pop("notes", None)
                changed = True
                moved += 1
        if changed:
            save_sheet(company, month, data)
    return moved
