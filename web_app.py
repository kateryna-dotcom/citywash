
"""
Standalone web page (no WhatsApp/Meta/Telegram needed) that looks like a
WhatsApp chat and generates HR documents for א.ב.ת. שירותי שטיפה:
  - חוזה עבודה חדש (employment contract)
  - מכתב פיטורים (termination letter)
  - הזמנה לשימוע (hearing invitation)
  - אישור העסקה (employment confirmation)
  - הצהרת בטיחות (safety acknowledgment)

Run locally:
    pip install -r requirements.txt
    uvicorn web_app:app --reload --port 8000
    open http://localhost:8000

Deploy anywhere that runs Docker (Render, Railway, Fly.io, a VPS, ...).
Requires LibreOffice on the host for the docx -> pdf conversion (see Dockerfile).
"""
import io
import os
import re
import threading
import time
import zipfile
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile, File
from typing import List
from fastapi.responses import HTMLResponse, Response, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from contract_filler import (
    fill_contract,
    fill_worker_contract,
    fill_document,
    fill_incident_notice,
    fill_hearing_invitation,
    docx_to_pdf,
    merge_pdfs,
)
from esign import send_for_sms_signature
import pension_store
import pension_companies
import branches
import invoice_store
import invoice_ingest
import suppliers
import item_matcher
import item_mapping_store
import catalog_store
import cashontab_bot

# doc_type keys that support the "send for SMS signature" option -- each of
# these templates has an invisible marker (§) placed at the signature spot.
SMS_SIGNABLE_DOC_TYPES = {"contract_manager", "contract_worker", "termination", "hearing", "confirmation"}

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-only-change-me"),
    max_age=60 * 60 * 24 * 14,  # 2 weeks
)

BASE_DIR = os.path.dirname(__file__)
HTML_PATH = os.path.join(BASE_DIR, "chat_ui.html")
DASHBOARD_HTML_PATH = os.path.join(BASE_DIR, "dashboard.html")
LOGIN_HTML_PATH = os.path.join(BASE_DIR, "login.html")
PENSION_HTML_PATH = os.path.join(BASE_DIR, "pension.html")
INVENTORY_HTML_PATH = os.path.join(BASE_DIR, "inventory.html")
MAPPINGS_HTML_PATH = os.path.join(BASE_DIR, "mappings.html")

# How often the background thread checks Gmail for new supplier invoices.
INVOICE_POLL_INTERVAL_SECONDS = 15 * 60


def _invoice_poll_loop():
    while True:
        try:
            result = invoice_ingest.process_new_invoices()
            if result.get("created"):
                print(f"[invoice-poll] created={result['created']} skipped={result['skipped']}")
            if result.get("errors"):
                print(f"[invoice-poll] errors={result['errors']}")
        except Exception as e:  # noqa: BLE001
            print(f"[invoice-poll] skipped this cycle: {e}")
        time.sleep(INVOICE_POLL_INTERVAL_SECONDS)


@app.on_event("startup")
def _startup():
    # Creates the pension_records table if it doesn't exist yet. If the DB
    # isn't configured yet (e.g. first deploy before Postgres is connected),
    # don't crash the whole app -- the /api/pension/* routes will just fail
    # until DATABASE_URL / PENSION_ENCRYPTION_KEY are set.
    try:
        pension_store.init_db()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] pension_store.init_db() skipped: {e}")

    try:
        invoice_store.init_db()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] invoice_store.init_db() skipped: {e}")

    try:
        # Creates item_mappings and seeds the product-match confirmations
        # Kateryna has already given (see item_mapping_store._SEED_MAPPINGS)
        # -- idempotent, only inserts rows that don't exist yet.
        item_mapping_store.init_db()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] item_mapping_store.init_db() skipped: {e}")

    try:
        catalog_store.init_db()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] catalog_store.init_db() skipped: {e}")

    # Background polling for new supplier invoices -- runs regardless of
    # whether anyone has the dashboard open. If GMAIL_* env vars aren't set
    # yet, each cycle just logs and retries later instead of crashing.
    threading.Thread(target=_invoice_poll_loop, daemon=True).start()


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def _require_page_auth(request: Request):
    """Returns a redirect Response if not logged in, otherwise None."""
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _require_api_auth(request: Request):
    """Returns a 401 Response if not logged in, otherwise None."""
    if not _is_authenticated(request):
        return Response("Unauthorized - please log in again", status_code=401)
    return None

# doc_type key -> template file. Must match the keys used in chat_ui.html's DOC_TYPES.
DOCUMENT_REGISTRY = {
    "contract_manager": {"template": "employment_contract_template_ABT.docx", "kind": "contract"},
    "contract_worker": {"template": "employment_contract_template_worker.docx", "kind": "worker_contract"},
    "termination": {"template": "template_piturim.docx", "kind": "generic"},
    "hearing": {"template": "template_shimua.docx", "kind": "hearing_invitation"},
    "confirmation": {"template": "template_ishur_haaskaa.docx", "kind": "generic"},
    "safety": {"template": "template_betichut.docx", "kind": "generic"},
    "incident_notice": {"template": "template_incident_notice.docx", "kind": "incident_notice"},
}

# When the key document is generated, also generate and append these documents.
# EMPLOYEE_NAME / EMPLOYEE_ID are shared automatically; SIGN_DATE for the safety
# declaration defaults to the contract's START_DATE (signed on the first day).
BUNDLES = {
    "contract_manager": ["safety"],
    "contract_worker": ["safety"],
}


def _build_pdf_for(doc_type: str, fields: dict) -> bytes:
    entry = DOCUMENT_REGISTRY[doc_type]
    if entry["kind"] == "contract":
        pay_type = fields.pop("PAY_TYPE", None)
        amount = fields.pop("AMOUNT", None)
        if pay_type not in ("hourly", "monthly"):
            raise ValueError("PAY_TYPE must be 'hourly' or 'monthly'")
        if pay_type == "hourly":
            fields["HOURLY_WAGE"] = amount
        else:
            fields["MONTHLY_SALARY"] = amount
        docx_bytes = fill_contract(entry["template"], fields, pay_type)
    elif entry["kind"] == "worker_contract":
        pay_type = fields.pop("PAY_TYPE", None)
        amount = fields.pop("AMOUNT", None)
        schedule_type = fields.pop("SCHEDULE_TYPE", None)
        if pay_type not in ("hourly", "monthly"):
            raise ValueError("PAY_TYPE must be 'hourly' or 'monthly'")
        if schedule_type not in ("fixed", "shift"):
            raise ValueError("SCHEDULE_TYPE must be 'fixed' or 'shift'")
        if pay_type == "hourly":
            fields["HOURLY_WAGE"] = amount
        else:
            fields["MONTHLY_SALARY"] = amount
        docx_bytes = fill_worker_contract(entry["template"], fields, pay_type, schedule_type)
    elif entry["kind"] == "incident_notice":
        docx_bytes = fill_incident_notice(entry["template"], fields)
    elif entry["kind"] == "hearing_invitation":
        docx_bytes = fill_hearing_invitation(entry["template"], fields)
    else:
        docx_bytes = fill_document(entry["template"], fields)
    return docx_to_pdf(docx_bytes)


def _fields_for_bundle_doc(bundle_doc_type: str, main_fields: dict) -> dict:
    """Derive the field set for an auto-attached document from the main document's answers."""
    if bundle_doc_type == "safety":
        return {
            "EMPLOYEE_NAME": main_fields.get("EMPLOYEE_NAME", ""),
            "EMPLOYEE_ID": main_fields.get("EMPLOYEE_ID", ""),
            "SIGN_DATE": main_fields.get("START_DATE", ""),
        }
    return {}


def _build_final_pdf(doc_type: str, fields: dict) -> bytes:
    """Builds the main document PDF and merges in any bundled documents (e.g. safety declaration)."""
    # main_fields is consumed (mutated) by _build_pdf_for for "contract" kind,
    # so derive bundle fields from a copy first.
    bundle_source_fields = dict(fields)
    pdf_bytes = _build_pdf_for(doc_type, fields)

    for bundle_doc_type in BUNDLES.get(doc_type, []):
        bundle_fields = _fields_for_bundle_doc(bundle_doc_type, bundle_source_fields)
        bundle_pdf = _build_pdf_for(bundle_doc_type, bundle_fields)
        pdf_bytes = merge_pdfs([pdf_bytes, bundle_pdf])
    return pdf_bytes


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    redirect = _require_page_auth(request)
    if redirect:
        return redirect
    with open(DASHBOARD_HTML_PATH, encoding="utf-8") as f:
        return f.read()


@app.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request):
    redirect = _require_page_auth(request)
    if redirect:
        return redirect
    with open(HTML_PATH, encoding="utf-8") as f:
        return f.read()


@app.get("/pension", response_class=HTMLResponse)
def pension_page(request: Request):
    redirect = _require_page_auth(request)
    if redirect:
        return redirect
    with open(PENSION_HTML_PATH, encoding="utf-8") as f:
        return f.read()


@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request):
    redirect = _require_page_auth(request)
    if redirect:
        return redirect
    with open(INVENTORY_HTML_PATH, encoding="utf-8") as f:
        return f.read()


@app.get("/mappings", response_class=HTMLResponse)
def mappings_page(request: Request):
    redirect = _require_page_auth(request)
    if redirect:
        return redirect
    with open(MAPPINGS_HTML_PATH, encoding="utf-8") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
def login_page():
    with open(LOGIN_HTML_PATH, encoding="utf-8") as f:
        return f.read().replace("ERROR_PLACEHOLDER", "")


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""

    expected_user = os.environ.get("DASHBOARD_USERNAME")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD")

    if expected_user and expected_pass and username == expected_user and password == expected_pass:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)

    with open(LOGIN_HTML_PATH, encoding="utf-8") as f:
        html = f.read()
    error_html = '<div class="error">שם משתמש או סיסמה שגויים</div>'
    return HTMLResponse(html.replace("ERROR_PLACEHOLDER", error_html), status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/api/generate")
async def generate(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    doc_type = payload.get("doc_type")
    fields = payload.get("fields") or {}

    entry = DOCUMENT_REGISTRY.get(doc_type)
    if entry is None:
        return Response(f"Unknown doc_type: {doc_type}", status_code=400)

    try:
        pdf_bytes = _build_final_pdf(doc_type, fields)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error generating document: {e}", status_code=500)

    name_part = fields.get("EMPLOYEE_NAME") or fields.get("BRANCH_NAME") or "document"
    filename = f"{doc_type}_{name_part}.pdf"
    # HTTP headers must be latin-1; Hebrew names aren't, so send an ASCII
    # fallback plus the real UTF-8 name via the filename* parameter (RFC 5987).
    ascii_filename = re.sub(r"[^\x20-\x7E]", "_", filename)
    utf8_filename = quote(filename)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{utf8_filename}"
            )
        },
    )


# doc_type per bulk-row "type" -- worker uses the same contract_worker
# template/flow as the one-by-one chat, manager uses contract_manager.
BULK_DOC_TYPE_BY_ROW_TYPE = {"worker": "contract_worker", "manager": "contract_manager"}

# Fields every row needs regardless of type, plus the extra ones only the
# worker contract template requires (JOB_TITLE, SCHEDULE_TYPE -- the
# manager template doesn't have those tokens at all).
_BULK_COMMON_FIELDS = ["EMPLOYEE_NAME", "EMPLOYEE_ID", "STATION_NAME", "START_DATE", "PAY_TYPE", "AMOUNT"]
_BULK_WORKER_ONLY_FIELDS = ["JOB_TITLE", "SCHEDULE_TYPE"]


def _sanitize_filename_part(name: str) -> str:
    # Keep Hebrew/Latin letters, digits, spaces and a few safe punctuation
    # marks; drop anything a filesystem/zip reader could choke on.
    cleaned = re.sub(r'[\\/:*?"<>|]', "", name or "").strip()
    return cleaned or "עובד"


@app.post("/api/bulk-generate")
async def bulk_generate(request: Request):
    """Generates a merged (contract + הצהרת בטיחות) PDF for each person in
    a bulk list -- one company for the whole batch, each row independently
    typed as a worker or a station manager. Returns a ZIP of all the PDFs
    for a single download, or a 400 listing exactly which rows are invalid
    (so she can fix the Excel and re-upload, rather than silently skipping
    people)."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    company_name = (payload.get("company_name") or "").strip()
    company_id = (payload.get("company_id") or "").strip()
    people = payload.get("people")

    if not company_name or not company_id:
        return Response("company_name and company_id are required", status_code=400)
    if not isinstance(people, list) or not people:
        return Response("Body must include a non-empty 'people' list", status_code=400)

    errors = []
    built = []  # [(filename, pdf_bytes), ...]
    used_filenames = {}

    for i, person in enumerate(people):
        row_label = person.get("EMPLOYEE_NAME") or f"שורה {i + 1}"
        row_type = person.get("type")
        doc_type = BULK_DOC_TYPE_BY_ROW_TYPE.get(row_type)
        if not doc_type:
            errors.append(f'{row_label}: סוג לא תקין ("{row_type}") -- חייב להיות עובד או מנהל')
            continue

        required = list(_BULK_COMMON_FIELDS)
        if row_type == "worker":
            required += _BULK_WORKER_ONLY_FIELDS
        missing = [f for f in required if not str(person.get(f) or "").strip()]
        if missing:
            errors.append(f"{row_label}: חסרים שדות ({', '.join(missing)})")
            continue

        fields = {"COMPANY_NAME": company_name, "COMPANY_ID": company_id}
        for f in required:
            fields[f] = person[f]

        try:
            pdf_bytes = _build_final_pdf(doc_type, dict(fields))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{row_label}: שגיאה ביצירת המסמך ({e})")
            continue

        base_name = _sanitize_filename_part(str(person.get("EMPLOYEE_NAME")))
        filename = f"{base_name}.pdf"
        if filename in used_filenames:
            used_filenames[filename] += 1
            filename = f"{base_name} ({used_filenames[filename]}).pdf"
        else:
            used_filenames[filename] = 0
        built.append((filename, pdf_bytes))

    if errors:
        return Response(
            "לא נוצרו מסמכים -- יש לתקן ולהעלות מחדש:\n" + "\n".join(errors),
            status_code=400,
        )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, pdf_bytes in built:
            zf.writestr(filename, pdf_bytes)
    zip_bytes = zip_buf.getvalue()

    zip_filename = f"חוזים_{company_name}.zip"
    ascii_filename = re.sub(r"[^\x20-\x7E]", "_", zip_filename)
    utf8_filename = quote(zip_filename)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{utf8_filename}"
            )
        },
    )


@app.post("/api/send-for-signature")
async def send_for_signature(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    doc_type = payload.get("doc_type")
    fields = payload.get("fields") or {}
    phone = (payload.get("phone") or "").strip()

    if doc_type not in SMS_SIGNABLE_DOC_TYPES:
        return Response(f"SMS signing not available for: {doc_type}", status_code=400)
    if not phone:
        return Response("Missing phone number", status_code=400)
    if DOCUMENT_REGISTRY.get(doc_type) is None:
        return Response(f"Unknown doc_type: {doc_type}", status_code=400)

    name_part = fields.get("EMPLOYEE_NAME") or fields.get("BRANCH_NAME") or "document"

    try:
        pdf_bytes = _build_final_pdf(doc_type, fields)
        result = send_for_sms_signature(
            pdf_bytes,
            phone=phone,
            subject=f"מסמך לחתימה - {name_part}",
            filename=f"{doc_type}_{name_part}.pdf",
        )
    except Exception as e:  # noqa: BLE001
        return Response(f"Error sending for signature: {e}", status_code=500)

    return {"status": "sent", "task_guid": result.get("TaskGuid")}


@app.get("/api/pension/list")
def pension_list(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return pension_store.list_records()
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading pension records: {e}", status_code=500)


@app.post("/api/pension/create")
async def pension_create(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    fields = await request.json()
    if not (fields.get("employee_name") or "").strip():
        return Response("employee_name is required", status_code=400)
    try:
        new_id = pension_store.create_record(fields)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error creating pension record: {e}", status_code=500)
    return {"status": "created", "id": new_id}


@app.post("/api/pension/update/{record_id}")
async def pension_update(record_id: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    fields = await request.json()
    if not (fields.get("employee_name") or "").strip():
        return Response("employee_name is required", status_code=400)
    try:
        pension_store.update_record(record_id, fields)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error updating pension record: {e}", status_code=500)
    return {"status": "updated"}


@app.post("/api/pension/delete/{record_id}")
def pension_delete(record_id: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        pension_store.delete_record(record_id)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error deleting pension record: {e}", status_code=500)
    return {"status": "deleted"}


# --- Redesigned פנסיה: company -> fund matrix (replaces the old
# per-employee CRUD above; kept alongside it so nothing else breaks). ---

@app.get("/api/pension/companies")
def pension_companies_list(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    return pension_companies.COMPANIES


@app.get("/api/pension/funds")
def pension_funds_list(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    return pension_companies.FUNDS


@app.get("/api/pension/summary")
def pension_summary(request: Request, company: str):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return pension_store.list_summary_for_company(company)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading summary: {e}", status_code=500)


@app.get("/api/pension/cell")
def pension_cell(request: Request, company: str, fund: str):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        record = pension_store.get_record(company, fund)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading record: {e}", status_code=500)
    return record or {}


@app.post("/api/pension/cell")
async def pension_cell_save(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    company = (payload.get("company") or "").strip()
    fund = (payload.get("fund") or "").strip()
    if not company or not fund:
        return Response("company and fund are required", status_code=400)
    try:
        record_id = pension_store.upsert_record(company, fund, payload)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error saving record: {e}", status_code=500)
    return {"status": "saved", "id": record_id}


@app.get("/api/pension/attachments/{record_id}")
def pension_attachments_list(record_id: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return pension_store.list_attachments(record_id)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading attachments: {e}", status_code=500)


@app.post("/api/pension/attachments/{record_id}")
async def pension_attachments_upload(record_id: int, request: Request, files: List[UploadFile] = File(...)):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        uploaded = []
        for f in files:
            data = await f.read()
            if not data:
                continue
            att_id = pension_store.add_attachment(record_id, f.filename, f.content_type, data)
            uploaded.append(att_id)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error uploading attachment: {e}", status_code=500)
    return {"status": "uploaded", "ids": uploaded}


@app.get("/api/pension/attachment/{attachment_id}")
def pension_attachment_download(attachment_id: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    att = pension_store.get_attachment(attachment_id)
    if not att:
        return Response("Not found", status_code=404)
    ascii_filename = re.sub(r"[^\x20-\x7E]", "_", att["filename"] or "attachment.pdf")
    utf8_filename = quote(att["filename"] or "attachment.pdf")
    return Response(
        content=att["file_data"],
        media_type=att["content_type"] or "application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_filename}'
            )
        },
    )


@app.post("/api/pension/attachment/delete/{attachment_id}")
def pension_attachment_delete(attachment_id: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        pension_store.delete_attachment(attachment_id)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error deleting attachment: {e}", status_code=500)
    return {"status": "deleted"}


@app.get("/api/inventory/list")
def inventory_list(request: Request, branch: str = None):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return invoice_store.list_records(branch=branch)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading invoice records: {e}", status_code=500)


def _ready_for_entry_shape(r: dict) -> dict | None:
    """None if this invoice record isn't ready to key into Cash On Tab yet
    (branch missing/a non-inventory category, or any line item still
    unmatched) -- otherwise the {id, branch, supplier_name, invoice_number,
    items} shape both /api/inventory/ready-for-entry and
    /api/inventory/auto-enter build on. Skipped line items are left out;
    an invoice with a line item still needing confirmation returns None
    entirely rather than a partial result."""
    if r["status"] != "needs_review" or not r.get("branch"):
        return None
    if r["branch"] in branches.NON_INVENTORY_CATEGORIES:
        return None
    entries = []
    for it in r.get("line_items") or []:
        if it.get("match_source") == "skip":
            continue
        if it.get("needs_confirmation", True):
            return None
        entries.append({
            "code": it.get("matched_code"),
            "name": it.get("matched_name") or it.get("description"),
            "quantity": it.get("quantity"),
            "unit_price": it.get("unit_price"),
        })
    if not entries:
        return None

    # Merge entries that share the same Cash On Tab item code into one,
    # summing quantity -- confirmed live 2026-08-31: typing an item code
    # into the grid a second time (a supplier invoice listed the same
    # קוד פריט on two separate lines) doesn't create an independent second
    # row in Cash On Tab, it merges into the already-linked row with
    # כמות/אריזות both blown up to 999,999 (same failure shape as the
    # already-linked-row duplication bug fixed earlier for a different
    # cause). Never hand cashontab_bot.py two entries with the same code.
    merged_by_code = {}
    merged_entries = []
    for entry in entries:
        code = entry.get("code")
        existing = merged_by_code.get(code) if code else None
        if existing is not None:
            existing["quantity"] = (existing.get("quantity") or 0) + (entry.get("quantity") or 0)
            continue
        if code:
            merged_by_code[code] = entry
        merged_entries.append(entry)
    entries = merged_entries
    return {
        "id": r["id"],
        "branch": r["branch"],
        "branch_code_hint": branches.cashontab_code_hint(r["branch"]),
        "supplier_domain": r.get("supplier_domain"),
        "supplier_name": suppliers.cashontab_search_value(r.get("supplier_domain")),
        "invoice_number": r.get("invoice_number"),
        "received_at": r.get("received_at"),
        "items": entries,
    }


@app.get("/api/inventory/ready-for-entry")
def inventory_ready_for_entry(request: Request):
    """Invoices that are fully reviewed -- branch assigned, every line item
    either matched to a Cash On Tab product code or explicitly skipped --
    but not yet marked "ok". This is the exact list a browser session
    entering goods receipts into Cash On Tab's מלאי screens needs, with
    nothing left to guess: skipped items are left out, and any invoice with
    a line item still needing confirmation is left out entirely rather than
    risking a partial/wrong entry. See docs/cashontab-entry-playbook.md.

    Invoices filed under a non-inventory category (branches.NON_INVENTORY_CATEGORIES
    -- וואש פוינט / השכורות / שונה) are excluded too: those are expense
    buckets Kateryna tracks for their total cost, not real Cash On Tab
    branches, so there's nothing to key in."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        out = []
        for r in invoice_store.list_records():
            entry = _ready_for_entry_shape(r)
            if entry:
                out.append(entry)
        return out
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading ready-for-entry invoices: {e}", status_code=500)


@app.post("/api/inventory/auto-enter/{record_id}")
def inventory_auto_enter(record_id: int, request: Request):
    """The server-side path Kateryna chose over the browser-supervised one
    (see docs/cashontab-entry-playbook.md): logs into Cash On Tab itself
    (cashontab_bot.py, headless Chromium) and creates the ת.מ. רכש document
    for this one invoice, then marks it "ok" here on success. On any
    ambiguity cashontab_bot.py refuses to guess and raises instead -- the
    error (with a screenshot of whatever Cash On Tab screen it was on)
    comes back as-is rather than silently leaving a half-entered document."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    record = invoice_store.get_record(record_id)
    if not record:
        return Response("Invoice record not found", status_code=404)
    entry = _ready_for_entry_shape(record)
    if not entry:
        return Response("This invoice isn't ready for entry (branch/items not fully confirmed)", status_code=400)
    try:
        cashontab_bot.enter_invoice(entry)
    except cashontab_bot.CashOnTabError as e:
        return JSONResponse(
            {"error": str(e), "screenshot_b64": e.screenshot_b64},
            status_code=502,
        )
    except Exception as e:  # noqa: BLE001
        return Response(f"Unexpected error entering invoice: {e}", status_code=500)
    invoice_store.update_status(record_id, "ok")
    return {"status": "created"}


@app.get("/api/inventory/branches")
def inventory_branches(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return invoice_store.list_branches()
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading branches: {e}", status_code=500)


@app.get("/api/inventory/supplier-names")
def inventory_supplier_names(request: Request):
    """supplier_domain -> friendly Hebrew name, so the מלАי supplier filter
    can show real names instead of raw email domains."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    return suppliers.SUPPLIER_CASHONTAB_NAMES


@app.get("/api/inventory/branch-options")
def inventory_branch_options(request: Request):
    """Canonical branch list (matches the Cash On Tab dropdown) plus the
    non-inventory expense categories (וואש פוינט / השכורות / שונה) -- used to
    populate the editable branch picker on each invoice card. An invoice
    filed under one of the categories still shows up (and totals) in the
    branch filter, it's just excluded from /api/inventory/ready-for-entry
    since there's no real Cash On Tab branch to enter it into."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    return branches.BRANCHES + branches.NON_INVENTORY_CATEGORIES


@app.post("/api/inventory/set-branch/{record_id}")
async def inventory_set_branch(record_id: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    branch = (payload.get("branch") or "").strip()
    try:
        invoice_store.update_branch(record_id, branch)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error updating branch: {e}", status_code=500)
    return {"status": "updated"}


@app.post("/api/inventory/check-now")
def inventory_check_now(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return invoice_ingest.process_new_invoices()
    except Exception as e:  # noqa: BLE001
        return Response(f"Error checking for new invoices: {e}", status_code=500)


@app.post("/api/inventory/reparse/{record_id}")
def inventory_reparse(record_id: int, request: Request):
    """Re-runs the line-item parser against this invoice's already-stored
    raw_text (and original PDF, if we have it) -- for records ingested
    before a parser fix (or bug), so she doesn't have to wait for/trigger a
    fresh Gmail fetch to pick it up."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    record = invoice_store.get_record(record_id)
    if not record:
        return Response("Invoice record not found", status_code=404)
    try:
        pdf_bytes = invoice_store.get_pdf_data(record_id)
        line_items = invoice_ingest.parse_line_items(
            record.get("supplier_domain") or "", record.get("raw_text") or "", pdf_bytes=pdf_bytes
        )
        invoice_store.update_line_items(record_id, line_items or None)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error reparsing invoice: {e}", status_code=500)
    return {"status": "reparsed", "item_count": len(line_items or [])}


@app.get("/api/inventory/search-catalog")
def inventory_search_catalog(request: Request, q: str = ""):
    """Live catalogue search backing the fix/resolve box in the review UI --
    lets her browse real product names (e.g. distinguishing '... TITANIUM
    14\"' from '... TITANIUM 16\"') instead of typing a barcode blind."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    q = (q or "").strip()
    if not q:
        return []
    try:
        return item_matcher.search_by_name(q, limit=10)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error searching catalog: {e}", status_code=500)


@app.post("/api/inventory/resolve-item/{record_id}/{item_index}")
async def inventory_resolve_item(record_id: int, item_index: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    resolved = bool(payload.get("resolved", True))
    try:
        invoice_store.update_line_item(record_id, item_index, {"resolved": resolved})
    except Exception as e:  # noqa: BLE001
        return Response(f"Error updating item: {e}", status_code=500)
    return {"status": "updated"}


@app.post("/api/inventory/match-item/{record_id}/{item_index}")
async def inventory_match_item(record_id: int, item_index: int, request: Request):
    """Resolves a line item the automatic matcher couldn't confidently
    place: either Kateryna types/scans the Cash On Tab barcode of the
    correct product, or searches the catalogue by name and picks a result
    ("matched"), or marks it as never-to-be-entered ("skip"). When the line
    item has a supplier SKU, the decision is also saved to item_mappings so
    this exact supplier SKU is never asked about again on future invoices.
    Some suppliers' invoices (e.g. אמפייר אס / grow.security) print no
    מק"ט at all -- the parser then has nothing to key a mapping on, so for
    those the resolution is applied to this line item only and nothing is
    remembered for next time; she'll search by name again on the next
    invoice, which is still much faster than being unable to resolve it at
    all (the previous behavior: a hard 400 blocked confirmation entirely
    whenever sku was empty)."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    action = payload.get("action")
    barcode = (payload.get("barcode") or "").strip()
    # True when she's confirming a product that isn't in our catalogue
    # snapshot at all (e.g. she just added it in Cash On Tab directly --
    # our cashontab_catalog.json is a point-in-time export of Items.xlsx
    # and doesn't see that until she re-sends an updated export).
    force = bool(payload.get("force"))

    record = invoice_store.get_record(record_id)
    if not record:
        return Response("Invoice record not found", status_code=404)
    items = record.get("line_items") or []
    if item_index < 0 or item_index >= len(items):
        return Response("Item index out of range", status_code=404)
    line_item = items[item_index]
    supplier_domain = record.get("supplier_domain") or ""
    # Clean in case this line item was stored before the bidi-mark fix --
    # keeps the mapping key consistent with what future (already-clean)
    # ingests will look up. Empty when the supplier's invoice has no מק"ט
    # at all -- there's then no stable key to save a reusable mapping under,
    # but the line item itself can still be resolved below.
    sku = item_matcher.clean_key(line_item.get("sku") or line_item.get("barcode") or "")

    try:
        if action == "skip":
            if sku:
                item_mapping_store.upsert_mapping(supplier_domain, sku, "skip")
            invoice_store.update_line_item(record_id, item_index, {
                "match_source": "skip", "needs_confirmation": False,
                "matched_code": None, "matched_barcode": None, "matched_name": None,
            })
            return {"status": "updated", "match_source": "skip"}

        if action == "matched":
            if not barcode:
                return Response("barcode is required for action=matched", status_code=400)
            item = item_matcher.lookup(barcode)
            if not item and not force:
                # Doesn't exist in the catalogue snapshot -- ask her to
                # confirm it's a genuinely new product (not a typo) before
                # saving a mapping we can't verify against anything.
                return Response(
                    f'הברקוד "{barcode}" לא נמצא בקטלוג השמור (יתכן שהוספת את המוצר ישירות ב-Cash On Tab '
                    'ועדיין לא עדכנת אותי בקובץ מוצרים חדש) — אם זה נכון, לחצי "שמור בכל זאת".',
                    status_code=409,
                )
            catalog_code = item["code"] if item else barcode
            catalog_name = item["name"] if item else (line_item.get("description") or barcode)
            if sku:
                item_mapping_store.upsert_mapping(
                    supplier_domain, sku, "matched",
                    catalog_code=catalog_code, catalog_name=catalog_name,
                    notes=None if item else "force-saved: not in the cashontab_catalog.json snapshot at save time",
                )
            invoice_store.update_line_item(record_id, item_index, {
                "match_source": "matched", "needs_confirmation": False,
                "matched_code": catalog_code, "matched_barcode": item.get("barcode") if item else barcode,
                "matched_name": catalog_name,
            })
            return {"status": "updated", "match_source": "matched", "matched_name": catalog_name}

        return Response('action must be "matched" or "skip"', status_code=400)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error resolving item: {e}", status_code=500)


@app.get("/api/inventory/catalog-status")
def inventory_catalog_status(request: Request):
    """Shows when the catalogue was last synced from Cash On Tab, and how
    many products are loaded -- so the review UI can show her whether her
    latest export has actually been picked up."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        rows = catalog_store.get_catalog()
        synced_at = catalog_store.last_synced_at()
        return {
            "synced_from_db": bool(rows),
            "item_count": len(rows) if rows else item_matcher.catalog_size(),
            "last_synced_at": synced_at.isoformat() if synced_at else None,
        }
    except Exception as e:  # noqa: BLE001
        return Response(f"Error reading catalog status: {e}", status_code=500)


@app.post("/api/inventory/sync-catalog")
async def inventory_sync_catalog(request: Request):
    """Live catalogue refresh -- replaces the whole product list in one
    shot and makes it effective immediately (no redeploy). Body:
    {"items": [{"code", "barcode", "name", "active"}, ...]}. Used after
    Kateryna re-exports the product list from her own logged-in Cash On
    Tab session (via Claude in Chrome reading the export, or by her
    uploading a fresh Items.xlsx that gets converted to this shape)."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return Response("Body must include a non-empty 'items' list", status_code=400)
    try:
        count = catalog_store.replace_catalog(items)
        item_matcher.reload()
    except Exception as e:  # noqa: BLE001
        return Response(f"Error syncing catalog: {e}", status_code=500)
    return {"status": "synced", "item_count": count}


@app.get("/api/mappings/list")
def mappings_list(request: Request):
    """All confirmed supplier-sku -> Cash On Tab mappings, for the review
    page -- lets Kateryna see/fix/delete a learned match without having to
    wait for it to show up on a new invoice again."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return item_mapping_store.list_mappings()
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading mappings: {e}", status_code=500)


@app.post("/api/mappings/update/{mapping_id}")
async def mappings_update(mapping_id: int, request: Request):
    """Edits an existing mapping in place (e.g. fixing a wrong catalog_code,
    or switching a row between matched/skip/new_pending). Body:
    {"action", "catalog_code", "catalog_name", "notes"}."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    action = payload.get("action")
    if action not in ("matched", "skip", "new_pending"):
        return Response('action must be "matched", "skip" or "new_pending"', status_code=400)
    catalog_code = (payload.get("catalog_code") or "").strip() or None
    catalog_name = (payload.get("catalog_name") or "").strip() or None
    notes = (payload.get("notes") or "").strip() or None
    if action == "matched" and not catalog_code:
        return Response("catalog_code is required when action is \"matched\"", status_code=400)
    try:
        updated = item_mapping_store.update_mapping_by_id(
            mapping_id, action, catalog_code=catalog_code, catalog_name=catalog_name, notes=notes,
        )
    except Exception as e:  # noqa: BLE001
        return Response(f"Error updating mapping: {e}", status_code=500)
    if not updated:
        return Response("Mapping not found", status_code=404)
    return {"status": "updated"}


@app.post("/api/mappings/delete/{mapping_id}")
def mappings_delete(mapping_id: int, request: Request):
    """Removes a learned mapping entirely -- the next invoice with this
    supplier sku will go back to being flagged for review from scratch."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        item_mapping_store.delete_mapping(mapping_id)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error deleting mapping: {e}", status_code=500)
    return {"status": "deleted"}


@app.post("/api/inventory/mark/{record_id}")
async def inventory_mark(record_id: int, request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    status = payload.get("status", "ok")
    note = payload.get("note")
    try:
        invoice_store.update_status(record_id, status, note)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error updating record: {e}", status_code=500)
    return {"status": "updated"}


@app.post("/api/inventory/delete/{record_id}")
def inventory_delete(record_id: int, request: Request):
    """Removes an invoice card from the מלАי review list entirely -- for
    ones she doesn't need there (duplicates, irrelevant, already handled
    another way). Doesn't touch anything already learned in item_mappings
    from this invoice's line items."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        invoice_store.delete_record(record_id)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error deleting record: {e}", status_code=500)
    return {"status": "deleted"}


@app.get("/health")
def health():
    return {"status": "alive"}
