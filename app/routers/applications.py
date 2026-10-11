"""Public lab application flow (/join/lab). Six steps, saved as a draft, submitted at the end.

The applicant has no account yet, so a draft is protected by a draft token returned when it is created.
The frontend keeps it (e.g. in localStorage) and sends it as the X-Draft-Token header.
"""
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status

from app.config import get_settings
from app.db import get_db
from app.schemas import (
    DOCUMENT_KINDS,
    EDITABLE_STATUSES,
    REQUIRED_DOCUMENTS,
    REQUIRED_SECTIONS,
    ApplicationDraft,
    ApplicationStatus,
)
from app.security import new_token, now, sha256
from app.services.common import new_id, next_number
from app.services.storage import get_storage

router = APIRouter(prefix="/labs/applications", tags=["Lab application"])

ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png"}


async def _load(app_id: str, draft_token: str | None) -> dict:
    app = await get_db().lab_applications.find_one({"_id": app_id})
    if not app or not draft_token or sha256(draft_token) != app["draft_token_hash"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return app


def _public(app: dict) -> dict:
    out = {k: v for k, v in app.items() if k not in {"draft_token_hash", "_id"}}
    out["id"] = app["_id"]
    out["documents"] = [{"kind": d["kind"], "filename": d["filename"]} for d in app.get("documents", [])]
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_application(body: ApplicationDraft):
    token = new_token()
    doc = {
        "_id": new_id(),
        "status": ApplicationStatus.draft.value,
        "draft_token_hash": sha256(token),
        **body.model_dump(mode="json", exclude_none=True),
        "documents": [],
        "notes": [],
        "created_at": now(),
        "updated_at": now(),
    }
    await get_db().lab_applications.insert_one(doc)
    return {"id": doc["_id"], "draft_token": token, "status": doc["status"]}


@router.get("/{app_id}")
async def get_application(app_id: str, x_draft_token: str | None = Header(None)):
    return _public(await _load(app_id, x_draft_token))


@router.patch("/{app_id}")
async def save_steps(app_id: str, body: ApplicationDraft, x_draft_token: str | None = Header(None)):
    app = await _load(app_id, x_draft_token)
    if ApplicationStatus(app["status"]) not in EDITABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Application is {app['status']} and cannot be edited")
    changes = body.model_dump(mode="json", exclude_none=True)
    if changes:
        await get_db().lab_applications.update_one(
            {"_id": app_id}, {"$set": {**changes, "updated_at": now()}}
        )
    return _public(await get_db().lab_applications.find_one({"_id": app_id}))


@router.post("/{app_id}/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    app_id: str,
    kind: str = Form(...),
    file: UploadFile = File(...),
    x_draft_token: str | None = Header(None),
):
    app = await _load(app_id, x_draft_token)
    if ApplicationStatus(app["status"]) not in EDITABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Documents can no longer be changed")
    if kind not in DOCUMENT_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown document kind: {kind}")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload a PDF, JPEG or PNG")
    data = await file.read()
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is too large")

    key = get_storage().save(f"kyc/{app_id}", file.filename or kind, data, file.content_type)
    entry = {"kind": kind, "filename": file.filename, "key": key, "content_type": file.content_type, "uploaded_at": now()}
    # One file per kind, except "other".
    if kind != "other":
        await get_db().lab_applications.update_one({"_id": app_id}, {"$pull": {"documents": {"kind": kind}}})
    await get_db().lab_applications.update_one(
        {"_id": app_id}, {"$push": {"documents": entry}, "$set": {"updated_at": now()}}
    )
    return {"kind": kind, "filename": file.filename}


@router.post("/{app_id}/submit")
async def submit_application(app_id: str, x_draft_token: str | None = Header(None)):
    app = await _load(app_id, x_draft_token)
    if ApplicationStatus(app["status"]) not in EDITABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Application is already {app['status']}")

    missing = [s for s in REQUIRED_SECTIONS if not app.get(s)]
    have_docs = {d["kind"] for d in app.get("documents", [])}
    missing += [f"document:{k}" for k in sorted(REQUIRED_DOCUMENTS - have_docs)]
    reg = app.get("regulatory") or {}
    if reg.get("nabl_accredited") and "nabl_certificate" not in have_docs:
        missing.append("document:nabl_certificate")
    decl = app.get("declarations") or {}
    if decl and not (decl.get("accepted_terms") and decl.get("information_true")):
        missing.append("declarations: terms and declaration must be accepted")
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"message": "Application is incomplete", "missing": missing})

    reference = app.get("reference") or f"SA-LAB-{await next_number(get_db(), 'lab_application')}"
    await get_db().lab_applications.update_one(
        {"_id": app_id},
        {"$set": {"status": ApplicationStatus.submitted.value, "reference": reference, "submitted_at": now(), "updated_at": now()}},
    )
    return {"id": app_id, "reference": reference, "status": ApplicationStatus.submitted.value}
