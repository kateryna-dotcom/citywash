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
import os
import re
import threading
import time
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile, File
from typing import List
from fastapi.responses import HTMLResponse, Response, RedirectResponse
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
import item_matcher
import item_mapping_store

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


@app.get("/api/inventory/branches")
def inventory_branches(request: Request):
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    try:
        return invoice_store.list_branches()
    except Exception as e:  # noqa: BLE001
        return Response(f"Error loading branches: {e}", status_code=500)


@app.get("/api/inventory/branch-options")
def inventory_branch_options(request: Request):
    """Canonical branch list (matches the Cash On Tab dropdown), used to
    populate the editable branch picker on each invoice card."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    return branches.BRANCHES


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
    correct product ("matched"), or marks it as never-to-be-entered
    ("skip"). Either way the decision is saved to item_mappings so this
    exact supplier SKU is never asked about again on future invoices."""
    unauthorized = _require_api_auth(request)
    if unauthorized:
        return unauthorized
    payload = await request.json()
    action = payload.get("action")
    barcode = (payload.get("barcode") or "").strip()

    record = invoice_store.get_record(record_id)
    if not record:
        return Response("Invoice record not found", status_code=404)
    items = record.get("line_items") or []
    if item_index < 0 or item_index >= len(items):
        return Response("Item index out of range", status_code=404)
    line_item = items[item_index]
    supplier_domain = record.get("supplier_domain") or ""
    sku = line_item.get("sku") or line_item.get("barcode") or ""
    if not sku:
        return Response("This item has no SKU/barcode to key the mapping on", status_code=400)

    try:
        if action == "skip":
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
            if not item:
                return Response(f'הברקוד "{barcode}" לא נמצא בקטלוג Cash On Tab', status_code=400)
            item_mapping_store.upsert_mapping(
                supplier_domain, sku, "matched",
                catalog_code=item["code"], catalog_name=item["name"],
            )
            invoice_store.update_line_item(record_id, item_index, {
                "match_source": "matched", "needs_confirmation": False,
                "matched_code": item["code"], "matched_barcode": item.get("barcode"),
                "matched_name": item["name"],
            })
            return {"status": "updated", "match_source": "matched", "matched_name": item["name"]}

        return Response('action must be "matched" or "skip"', status_code=400)
    except Exception as e:  # noqa: BLE001
        return Response(f"Error resolving item: {e}", status_code=500)


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


@app.get("/health")
def health():
    return {"status": "alive"}
