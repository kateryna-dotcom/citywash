"""
Storage layer for the "פנסיה" (pension) section: per-employee pension-fund
records, including a login/password for the fund's website.

Backed by Postgres (DATABASE_URL env var, e.g. Render's free Postgres).
Passwords are encrypted at rest with a symmetric key (PENSION_ENCRYPTION_KEY
env var, a Fernet key) -- never stored in plain text in the database.

Set these env vars in Render (Settings -> Environment), never in git:
  DATABASE_URL            postgres connection string (Render provides this)
  PENSION_ENCRYPTION_KEY  a Fernet key, generate once with:
                             python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
                           and keep it constant -- changing it makes existing
                           stored passwords undecryptable.
"""
import os

import psycopg2
import psycopg2.extras
from cryptography.fernet import Fernet, InvalidToken


def _get_fernet() -> Fernet:
    key = os.environ.get("PENSION_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("PENSION_ENCRYPTION_KEY environment variable is not set")
    return Fernet(key.encode())


def _get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    # Render's free Postgres sometimes gives a URL starting with postgres://
    # which older psycopg2 versions reject; normalize to postgresql://.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url)


def init_db():
    """Creates the pension_records table if it doesn't exist yet. Safe to call on every startup."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pension_records (
                    id SERIAL PRIMARY KEY,
                    employee_name TEXT NOT NULL,
                    company_name TEXT,
                    fund_name TEXT,
                    portal_url TEXT,
                    portal_username TEXT,
                    portal_password_encrypted TEXT,
                    notes TEXT,
                    has_issue BOOLEAN NOT NULL DEFAULT FALSE,
                    issue_description TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
        conn.commit()


def list_records() -> list:
    fernet = _get_fernet()
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pension_records ORDER BY has_issue DESC, employee_name ASC")
            rows = [dict(r) for r in cur.fetchall()]

    for row in rows:
        enc = row.pop("portal_password_encrypted", None)
        if enc:
            try:
                row["portal_password"] = fernet.decrypt(enc.encode()).decode()
            except InvalidToken:
                row["portal_password"] = None
        else:
            row["portal_password"] = ""
    return rows


def create_record(fields: dict) -> int:
    fernet = _get_fernet()
    password = fields.get("portal_password") or ""
    encrypted = fernet.encrypt(password.encode()).decode() if password else None

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pension_records
                    (employee_name, company_name, fund_name, portal_url, portal_username,
                     portal_password_encrypted, notes, has_issue, issue_description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                fields.get("employee_name", ""),
                fields.get("company_name", ""),
                fields.get("fund_name", ""),
                fields.get("portal_url", ""),
                fields.get("portal_username", ""),
                encrypted,
                fields.get("notes", ""),
                bool(fields.get("has_issue")),
                fields.get("issue_description", ""),
            ))
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def update_record(record_id: int, fields: dict):
    fernet = _get_fernet()
    password = fields.get("portal_password")
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if password:
                encrypted = fernet.encrypt(password.encode()).decode()
                cur.execute("""
                    UPDATE pension_records SET
                        employee_name=%s, company_name=%s, fund_name=%s, portal_url=%s,
                        portal_username=%s, portal_password_encrypted=%s, notes=%s,
                        has_issue=%s, issue_description=%s, updated_at=now()
                    WHERE id=%s
                """, (
                    fields.get("employee_name", ""), fields.get("company_name", ""),
                    fields.get("fund_name", ""), fields.get("portal_url", ""),
                    fields.get("portal_username", ""), encrypted, fields.get("notes", ""),
                    bool(fields.get("has_issue")), fields.get("issue_description", ""),
                    record_id,
                ))
            else:
                # password left blank in the edit form -> keep the existing one
                cur.execute("""
                    UPDATE pension_records SET
                        employee_name=%s, company_name=%s, fund_name=%s, portal_url=%s,
                        portal_username=%s, notes=%s, has_issue=%s, issue_description=%s,
                        updated_at=now()
                    WHERE id=%s
                """, (
                    fields.get("employee_name", ""), fields.get("company_name", ""),
                    fields.get("fund_name", ""), fields.get("portal_url", ""),
                    fields.get("portal_username", ""), fields.get("notes", ""),
                    bool(fields.get("has_issue")), fields.get("issue_description", ""),
                    record_id,
                ))
        conn.commit()


def delete_record(record_id: int):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pension_records WHERE id=%s", (record_id,))
        conn.commit()
