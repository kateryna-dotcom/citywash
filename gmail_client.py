"""
Minimal Gmail API client used to auto-detect new supplier invoices and
download their PDF attachments -- runs server-side (on Render), independent
of any interactive session.

Auth: OAuth "installed app" refresh-token flow. One-time setup was done via
Google Cloud Console + OAuth Playground (see project notes); the resulting
long-lived refresh token is exchanged for a short-lived access token on
every call.

Env vars required (set in Render, never in git):
    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN

Scope needed: https://www.googleapis.com/auth/gmail.readonly
"""
import base64
import os
from datetime import datetime

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Domains of the suppliers whose invoices should feed into מלАי. Matching by
# domain (not exact address) because some suppliers send from several
# different employee addresses at the same company (e.g. emi-1.com).
SUPPLIER_DOMAINS = [
    "hadarrosen.com",
    "emi-1.com",
    "moshaev-inv.com",
    "oz-b-g.com",
]


def _get_access_token() -> str:
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError(
            "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN "
            "environment variables are not all set"
        )
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_access_token()}"}


def _build_supplier_query(after: datetime | None) -> str:
    domain_clause = " OR ".join(f"from:{d}" for d in SUPPLIER_DOMAINS)
    query = f"({domain_clause}) has:attachment"
    if after:
        # Gmail's `after:` operator is date-only (no time-of-day), so we
        # over-fetch by using the date and rely on the caller to filter out
        # messages already processed (tracked by message id in our DB).
        query += f" after:{after.strftime('%Y/%m/%d')}"
    return query


def list_new_invoice_messages(after: datetime | None = None, max_results: int = 50) -> list[dict]:
    """Returns [{id, threadId}, ...] for candidate invoice emails from the
    watched supplier domains. Caller is responsible for de-duplicating
    against already-processed message ids (stored in Postgres)."""
    params = {"q": _build_supplier_query(after), "maxResults": max_results}
    resp = requests.get(f"{API_BASE}/messages", headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("messages", [])


def get_message(message_id: str) -> dict:
    """Full message resource (format=full): headers, snippet, and the MIME
    part tree needed to locate PDF attachment ids."""
    resp = requests.get(
        f"{API_BASE}/messages/{message_id}",
        headers=_headers(),
        params={"format": "full"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _walk_parts(part: dict):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk_parts(sub)


def extract_header(message: dict, name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def find_pdf_attachments(message: dict) -> list[dict]:
    """Returns [{filename, attachmentId}, ...] for every PDF part in the message."""
    out = []
    payload = message.get("payload", {})
    for part in _walk_parts(payload):
        filename = part.get("filename") or ""
        body = part.get("body", {})
        att_id = body.get("attachmentId")
        if att_id and filename.lower().endswith(".pdf"):
            out.append({"filename": filename, "attachmentId": att_id})
    return out


def get_attachment_bytes(message_id: str, attachment_id: str) -> bytes:
    resp = requests.get(
        f"{API_BASE}/messages/{message_id}/attachments/{attachment_id}",
        headers=_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    data_b64url = resp.json()["data"]
    # Gmail uses URL-safe base64 without padding.
    return base64.urlsafe_b64decode(data_b64url + "=" * (-len(data_b64url) % 4))


def supplier_domain_for(sender_header: str) -> str | None:
    sender_header = sender_header.lower()
    for domain in SUPPLIER_DOMAINS:
        if domain in sender_header:
            return domain
    return None
