"""
Fills the tokenized A.B.T. employment contract template and converts it to PDF.
Works directly on the .docx bytes (no need to pre-unzip).
"""
import io
import os
import re
import subprocess
import tempfile
import zipfile

TOKEN_PATTERN = re.compile(r"\{\{[A-Z_]+\}\}")

MONTHLY_MARKER = "שכר עובד משרה מלאה:"
HOURLY_MARKER = "שכר לעובד שעתי:"

# worker contract: schedule-type clauses (distinct from the wage markers above)
FIXED_SCHEDULE_MARKER = "<w:t>עובד משרה מלאה:</w:t>"
SHIFT_SCHEDULE_MARKER = '<w:t xml:space="preserve">עובד משמרות: </w:t>'


def _read_docx_parts(docx_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _write_docx(parts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _drop_paragraph_containing(xml: str, marker: str) -> str:
    """Remove the whole <w:p ...>...</w:p> block that contains `marker`."""
    idx = xml.find(marker)
    if idx == -1:
        return xml
    start = xml.rfind("<w:p ", 0, idx)
    end = xml.find("</w:p>", idx) + len("</w:p>")
    return xml[:start] + xml[end:]


def fill_document(template_path: str, fields: dict) -> bytes:
    """
    Generic filler: replaces every {{TOKEN}} in the template with fields[TOKEN].
    Works for any of the tokenized templates (termination letter, hearing
    invitation, employment confirmation, safety declaration, ...).
    Raises ValueError if any token in the template is left unfilled.
    """
    with open(template_path, "rb") as f:
        docx_bytes = f.read()

    parts = _read_docx_parts(docx_bytes)
    xml = parts["word/document.xml"].decode("utf-8")

    for key, value in fields.items():
        xml = xml.replace("{{%s}}" % key, str(value))

    leftover = TOKEN_PATTERN.findall(xml)
    if leftover:
        raise ValueError(f"Missing values for tokens: {set(leftover)}")

    parts["word/document.xml"] = xml.encode("utf-8")
    return _write_docx(parts)


def fill_contract(template_path: str, fields: dict, pay_type: str) -> bytes:
    """
    Employment-contract-specific filler: also drops whichever of the
    monthly-salary / hourly-wage clauses does not apply.

    fields must contain: EMPLOYEE_NAME, EMPLOYEE_ID, START_DATE, STATION_NAME,
    and either HOURLY_WAGE (pay_type='hourly') or MONTHLY_SALARY (pay_type='monthly').
    Returns filled .docx bytes.
    """
    with open(template_path, "rb") as f:
        docx_bytes = f.read()

    parts = _read_docx_parts(docx_bytes)
    xml = parts["word/document.xml"].decode("utf-8")

    if pay_type == "hourly":
        xml = _drop_paragraph_containing(xml, MONTHLY_MARKER)
    elif pay_type == "monthly":
        xml = _drop_paragraph_containing(xml, HOURLY_MARKER)
    else:
        raise ValueError("pay_type must be 'hourly' or 'monthly'")

    for key, value in fields.items():
        xml = xml.replace("{{%s}}" % key, str(value))

    leftover = TOKEN_PATTERN.findall(xml)
    if leftover:
        raise ValueError(f"Missing values for tokens: {set(leftover)}")

    parts["word/document.xml"] = xml.encode("utf-8")
    return _write_docx(parts)


def fill_worker_contract(template_path: str, fields: dict, pay_type: str, schedule_type: str) -> bytes:
    """
    Worker-contract-specific filler: drops whichever wage clause (monthly/hourly)
    and whichever schedule clause (fixed/shift) does not apply.

    fields must contain: EMPLOYEE_NAME, EMPLOYEE_ID, JOB_TITLE, STATION_NAME,
    START_DATE, and either HOURLY_WAGE (pay_type='hourly') or MONTHLY_SALARY
    (pay_type='monthly'). schedule_type is 'fixed' or 'shift'.
    Returns filled .docx bytes.
    """
    with open(template_path, "rb") as f:
        docx_bytes = f.read()

    parts = _read_docx_parts(docx_bytes)
    xml = parts["word/document.xml"].decode("utf-8")

    if pay_type == "hourly":
        xml = _drop_paragraph_containing(xml, MONTHLY_MARKER)
    elif pay_type == "monthly":
        xml = _drop_paragraph_containing(xml, HOURLY_MARKER)
    else:
        raise ValueError("pay_type must be 'hourly' or 'monthly'")

    if schedule_type == "shift":
        xml = _drop_paragraph_containing(xml, FIXED_SCHEDULE_MARKER)
    elif schedule_type == "fixed":
        xml = _drop_paragraph_containing(xml, SHIFT_SCHEDULE_MARKER)
    else:
        raise ValueError("schedule_type must be 'fixed' or 'shift'")

    for key, value in fields.items():
        xml = xml.replace("{{%s}}" % key, str(value))

    leftover = TOKEN_PATTERN.findall(xml)
    if leftover:
        raise ValueError(f"Missing values for tokens: {set(leftover)}")

    parts["word/document.xml"] = xml.encode("utf-8")
    return _write_docx(parts)


def docx_to_pdf(docx_bytes: bytes) -> bytes:
    """Convert docx bytes to pdf bytes via headless LibreOffice. Requires `soffice` on PATH."""
    with tempfile.TemporaryDirectory() as tmp:
        docx_path = os.path.join(tmp, "contract.docx")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, docx_path],
            check=True, timeout=60,
        )
        pdf_path = os.path.join(tmp, "contract.pdf")
        with open(pdf_path, "rb") as f:
            return f.read()


def merge_pdfs(pdf_bytes_list: list) -> bytes:
    """Concatenate several PDFs (bytes) into one, in the given order."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for pdf_bytes in pdf_bytes_list:
        writer.append(io.BytesIO(pdf_bytes))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
