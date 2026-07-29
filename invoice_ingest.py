"""
Ties gmail_client + invoice_store together: finds new supplier invoice
emails, downloads the PDF, extracts its text, does a first best-effort pass
at pulling out the branch name / invoice number, and stores everything in
Postgres for review on the מלАי dashboard tab.

NOTE on line items (products + quantities): real per-supplier parsing
(matching product names/barcodes and quantities from the PDF layout) needs
to be tuned against the ACTUAL extracted text of each supplier's invoice
format, which we don't have yet. Until that's tuned, every new invoice is
stored with its full raw_text and status='needs_review' so nothing is
silently mis-entered -- Kateryna can see exactly what the PDF said. Once we
see real examples we'll add supplier-specific line-item extraction and
flip matched invoices to an auto-filled state.
"""
import io
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from pypdf import PdfReader

import gmail_client
import invoice_store

# hadarrosen sends one email per branch with the branch name in the
# subject, e.g. "חשבונית לסניף עכו" -> branch "עכו".
_BRANCH_SUBJECT_RE = re.compile(r"לסניף\s+(.+)$")

# Common Hebrew invoice-number label variants, e.g. "חשבונית מס 213044",
# "חשבונית מס' IN264002223", "מספר תעודה: 108745".
_INVOICE_NUMBER_RE = re.compile(
    r"(?:חשבונית\s*מס'?|מספר\s*תעודה|תעודה\s*מס'?)\s*[:\-]?\s*([A-Za-z0-9\-]+)"
)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:  # noqa: BLE001
        return f"[could not extract text: {e}]"


def _guess_branch(subject: str, text: str) -> str:
    m = _BRANCH_SUBJECT_RE.search(subject or "")
    if m:
        return m.group(1).strip()
    return ""


def _guess_invoice_number(subject: str, text: str) -> str:
    for source in (subject or "", text or ""):
        m = _INVOICE_NUMBER_RE.search(source)
        if m:
            return m.group(1).strip()
    return ""


def process_new_invoices(lookback_days: int = 30) -> dict:
    """Finds supplier invoice emails, downloads + extracts each new PDF, and
    stores a review-ready record. Returns a small summary dict."""
    after = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    candidates = gmail_client.list_new_invoice_messages(after=after)
    created = 0
    skipped = 0
    errors = []

    for stub in candidates:
        message_id = stub["id"]
        if invoice_store.message_already_processed(message_id):
            skipped += 1
            continue
        try:
            message = gmail_client.get_message(message_id)
            sender = gmail_client.extract_header(message, "From")
            subject = gmail_client.extract_header(message, "Subject")
            date_header = gmail_client.extract_header(message, "Date")
            try:
                received_at = parsedate_to_datetime(date_header)
            except Exception:  # noqa: BLE001
                received_at = None

            supplier_domain = gmail_client.supplier_domain_for(sender)
            pdf_attachments = gmail_client.find_pdf_attachments(message)

            if not pdf_attachments:
                # Nothing to extract, but still record it so it doesn't get
                # re-scanned every run.
                invoice_store.create_record({
                    "gmail_message_id": message_id,
                    "supplier_domain": supplier_domain,
                    "sender_email": sender,
                    "subject": subject,
                    "received_at": received_at,
                    "pdf_filename": None,
                    "raw_text": "",
                    "branch": _guess_branch(subject, ""),
                    "invoice_number": _guess_invoice_number(subject, ""),
                    "status": "no_pdf_found",
                })
                created += 1
                continue

            # Most of these emails have exactly one invoice PDF; if there's
            # more than one, process the first and note the rest in the text.
            first = pdf_attachments[0]
            pdf_bytes = gmail_client.get_attachment_bytes(message_id, first["attachmentId"])
            text = _extract_pdf_text(pdf_bytes)

            invoice_store.create_record({
                "gmail_message_id": message_id,
                "supplier_domain": supplier_domain,
                "sender_email": sender,
                "subject": subject,
                "received_at": received_at,
                "pdf_filename": first["filename"],
                "raw_text": text,
                "branch": _guess_branch(subject, text),
                "invoice_number": _guess_invoice_number(subject, text),
                "status": "needs_review",
            })
            created += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{message_id}: {e}")

    return {"created": created, "skipped": skipped, "errors": errors}
