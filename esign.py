"""
Minimal client for the 2Sign ("חתימה ירוקה") digital-signature API.
Used to send a generated PDF to a phone number for SMS-based signing.

API docs: https://2sign-co-il.gitbook.io/2sign.co.il-docs

Auth reads TWOSIGN_EMAIL / TWOSIGN_PASSWORD from environment variables --
set these in the Render dashboard (Settings -> Environment), never in code
or in git.

How the signature placement works: every template that supports SMS signing
has an invisible marker character (§, white text) placed exactly where the
signature should go. 2Sign's "SearchWordForMarkingSignature" finds that
character in the uploaded PDF and puts the signature field there automatically
-- no manual coordinate placement needed.
"""
import os

import requests

BASE_URL = "https://app.2sign.co.il"
SIGNATURE_MARKER = "§"


def _unwrap(data: dict) -> dict:
    """2Sign wraps every response as {Status, Message, ResponseObject: {...actual data...}}."""
    if isinstance(data, dict) and isinstance(data.get("ResponseObject"), dict):
        return data["ResponseObject"]
    return data


def _login() -> str:
    email = os.environ.get("TWOSIGN_EMAIL")
    password = os.environ.get("TWOSIGN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "TWOSIGN_EMAIL / TWOSIGN_PASSWORD environment variables are not set"
        )

    resp = requests.post(
        f"{BASE_URL}/api/Account/LoginApi",
        json={"Email": email, "Password": password, "RememberMe": False},
        timeout=30,
    )
    resp.raise_for_status()
    data = _unwrap(resp.json())
    token = data.get("access_token") or data.get("Access_Token") or data.get("AccessToken")
    if not token:
        raise RuntimeError("2Sign login did not return an access_token (check credentials / API access)")
    return token


def _upload_file(token: str, pdf_bytes: bytes, filename: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/api/V2/BaseTasks/UploadFileForTask",
        headers={"Authorization": f"Bearer {token}"},
        files={"FileName": (filename, pdf_bytes, "application/pdf")},
        data={"Name": "fileUpload"},
        timeout=60,
    )
    resp.raise_for_status()
    return _unwrap(resp.json())


def send_for_sms_signature(pdf_bytes: bytes, phone: str, subject: str,
                            filename: str = "document.pdf") -> dict:
    """
    Uploads `pdf_bytes` to 2Sign and creates a signing task sent to `phone`
    via SMS. The signature field is placed wherever SIGNATURE_MARKER appears
    in the document.

    Returns the 2Sign response (includes the TaskGuid needed to later check
    status / download the signed document via get_task_status()).
    """
    token = _login()
    upload = _upload_file(token, pdf_bytes, filename)
    task_guid = upload.get("TaskGuid") or upload.get("taskGuid")
    pdf_guid = upload.get("PdfGuid") or upload.get("pdfGuid")
    if not task_guid or not pdf_guid:
        raise RuntimeError(f"2Sign upload did not return TaskGuid/PdfGuid: {upload}")

    resp = requests.post(
        f"{BASE_URL}/api/V2/TasksNew/CreateTask",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "ClientPhones": phone,
            "TaskGuid": task_guid,
            "PdfGuid": pdf_guid,
            "TaskSubject": subject,
            "Language": 1,
            "IsSendOnCreation": True,
            "IsSendSmsOnCreation": True,
            "SearchWordForMarkingSignature": SIGNATURE_MARKER,
            "ViewOnlyTask": False,
            "DigitalSign": True,
        },
        timeout=60,
    )
    resp.raise_for_status()
    result = _unwrap(resp.json())
    if isinstance(result, dict):
        result.setdefault("TaskGuid", task_guid)
    return result


def get_task_status(task_guid: str) -> dict:
    """Returns current status (includes IsSigned) and, once signed, links to the signed file."""
    token = _login()
    resp = requests.get(
        f"{BASE_URL}/api/Tasks/GetTaskByGuid",
        headers={"Authorization": f"Bearer {token}"},
        params={"guid": task_guid},
        timeout=30,
    )
    resp.raise_for_status()
    return _unwrap(resp.json())
