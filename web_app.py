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
from fastapi.responses import HTMLResponse, Response

from contract_filler import (
    fill_contract,
    fill_worker_contract,
    fill_document,
    fill_incident_notice,
    fill_hearing_invitation,
    docx_to_pdf,
    merge_pdfs,
)

app = FastAPI()

HTML_PATH = os.path.join(os.path.dirname(__file__), "chat_ui.html")

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


@app.get("/", response_class=HTMLResponse)
def index():
    with open(HTML_PATH, encoding="utf-8") as f:
        return f.read()


@app.post("/api/generate")
async def generate(request: Request):
    payload = await request.json()
    doc_type = payload.get("doc_type")
    fields = payload.get("fields") or {}

    entry = DOCUMENT_REGISTRY.get(doc_type)
    if entry is None:
        return Response(f"Unknown doc_type: {doc_type}", status_code=400)

    try:
        # main_fields is consumed (mutated) by _build_pdf_for for "contract" kind,
        # so derive bundle fields from a copy first.
        bundle_source_fields = dict(fields)
        pdf_bytes = _build_pdf_for(doc_type, fields)

        for bundle_doc_type in BUNDLES.get(doc_type, []):
            bundle_fields = _fields_for_bundle_doc(bundle_doc_type, bundle_source_fields)
            bundle_pdf = _build_pdf_for(bundle_doc_type, bundle_fields)
            pdf_bytes = merge_pdfs([pdf_bytes, bundle_pdf])
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


@app.get("/health")
def health():
    return {"status": "alive"}
