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
from urllib.parse import quote

from fastapi import FastAPI, Request
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


@app.get("/health")
def health():
    return {"status": "alive"}
