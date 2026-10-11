"""Admin → Onboarding → Labs."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config import get_settings
from app.db import get_db, transaction
from app.deps import Principal, current_admin
from app.schemas import REVIEWABLE_STATUSES, ApplicationStatus, HoldRequest, ReviewAction
from app.security import new_token, now, sha256
from app.services.common import audit, new_id, notify, unique_username
from app.services.storage import get_storage

router = APIRouter(prefix="/admin/labs/applications", tags=["Admin: lab onboarding"])


async def _get(app_id: str) -> dict:
    app = await get_db().lab_applications.find_one({"_id": app_id})
    if not app or app["status"] == ApplicationStatus.draft.value:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return app


@router.get("")
async def list_applications(
    status_: ApplicationStatus | None = Query(None, alias="status"),
    _: Principal = Depends(current_admin),
):
    query = {"status": status_.value} if status_ else {"status": {"$ne": ApplicationStatus.draft.value}}
    cursor = get_db().lab_applications.find(query).sort("submitted_at", -1)
    return [
        {
            "id": a["_id"],
            "reference": a.get("reference"),
            "legal_name": (a.get("centre") or {}).get("legal_name"),
            "city": ((a.get("centre") or {}).get("centre_address") or {}).get("city"),
            "status": a["status"],
            "submitted_at": a.get("submitted_at"),
        }
        async for a in cursor
    ]


@router.get("/{app_id}")
async def get_application(app_id: str, admin: Principal = Depends(current_admin)):
    app = await _get(app_id)
    storage = get_storage()
    out = {k: v for k, v in app.items() if k not in {"draft_token_hash", "_id"}}
    out["id"] = app["_id"]
    out["documents"] = [
        {"kind": d["kind"], "filename": d["filename"], "url": storage.signed_url(d["key"])} for d in app.get("documents", [])
    ]
    await audit(get_db(), admin.id, "application.viewed", app_id)
    return out


@router.post("/{app_id}/review")
async def mark_reviewing(app_id: str, admin: Principal = Depends(current_admin)):
    app = await _get(app_id)
    if app["status"] != ApplicationStatus.submitted.value:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Application is {app['status']}")
    await get_db().lab_applications.update_one(
        {"_id": app_id}, {"$set": {"status": ApplicationStatus.under_review.value, "updated_at": now()}}
    )
    await audit(get_db(), admin.id, "application.reviewing", app_id)
    return {"id": app_id, "status": ApplicationStatus.under_review.value}


@router.post("/{app_id}/hold")
async def hold_or_reject(app_id: str, body: HoldRequest, admin: Principal = Depends(current_admin)):
    app = await _get(app_id)
    if ApplicationStatus(app["status"]) not in REVIEWABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Application is {app['status']}")
    new_status = ApplicationStatus.on_hold if body.action == ReviewAction.hold else ApplicationStatus.rejected
    note = {"by": admin.id, "action": body.action.value, "note": body.note, "at": now()}
    await get_db().lab_applications.update_one(
        {"_id": app_id},
        {"$set": {"status": new_status.value, "updated_at": now()}, "$push": {"notes": note}},
    )
    await audit(get_db(), admin.id, f"application.{body.action.value}", app_id, {"note": body.note})
    await notify(
        get_db(),
        {"email": app["centre"]["official_email"]},
        f"lab_application_{body.action.value}",
        {"reference": app.get("reference"), "note": body.note},
    )
    return {"id": app_id, "status": new_status.value}


async def _issue_invite(db, user_id: str, email: str, username: str, session=None) -> None:
    s = get_settings()
    token = new_token()
    await db.lab_users.update_one(
        {"_id": user_id},
        {"$set": {
            "invite_token_hash": sha256(token),
            "invite_expires_at": now() + timedelta(hours=s.invite_expiry_hours),
        }},
        session=session,
    )
    # The link (with the raw token) only ever goes into the outgoing email, never into an API response.
    await notify(db, {"email": email}, "lab_invite", {"username": username, "link": f"{s.invite_link_base}?token={token}"})


@router.post("/{app_id}/accept")
async def accept(app_id: str, admin: Principal = Depends(current_admin)):
    db = get_db()
    app = await _get(app_id)
    if ApplicationStatus(app["status"]) not in REVIEWABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Application is {app['status']}")

    centre, service = app["centre"], app["service"]
    lab_id, user_id = new_id(), new_id()
    username = await unique_username(db, centre["legal_name"])
    email = centre["official_email"]

    lab = {
        "_id": lab_id,
        "application_id": app_id,
        "reference": app["reference"],
        "name": centre["legal_name"],
        "entity_type": centre["entity_type"],
        "address": centre["centre_address"],
        "contact_phone": centre["contact_phone"],
        "email": email,
        "nabl_accredited": app["regulatory"].get("nabl_accredited", False),
        "home_collection": service["home_collection"],
        "opening_hours": service["opening_hours"],
        "bank": service["bank"],
        "status": "active",
        "created_at": now(),
    }
    user = {
        "_id": user_id,
        "lab_id": lab_id,
        "username": username,
        "email": email,
        "password_hash": None,
        "must_set_password": True,
        "failed_logins": 0,
        "locked_until": None,
        "created_at": now(),
    }

    async with transaction() as session:
        await db.labs.insert_one(lab, session=session)
        await db.lab_users.insert_one(user, session=session)
        if service["pincodes_covered"]:
            await db.lab_service_areas.insert_many(
                [{"_id": new_id(), "lab_id": lab_id, "pincode": p} for p in service["pincodes_covered"]], session=session
            )
        await db.lab_applications.update_one(
            {"_id": app_id},
            {"$set": {"status": ApplicationStatus.active.value, "lab_id": lab_id, "accepted_at": now(), "updated_at": now()}},
            session=session,
        )
        await audit(db, admin.id, "application.accepted", app_id, {"lab_id": lab_id, "username": username}, session=session)

    await _issue_invite(db, user_id, email, username)
    return {"lab_id": lab_id, "username": username, "email": email, "invite_sent": True}


@router.post("/{app_id}/resend-invite")
async def resend_invite(app_id: str, admin: Principal = Depends(current_admin)):
    """Replaces 'New password': issues a fresh set-password link and invalidates the old one."""
    db = get_db()
    app = await _get(app_id)
    if app["status"] != ApplicationStatus.active.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Lab is not active")
    user = await db.lab_users.find_one({"lab_id": app["lab_id"]}, sort=[("created_at", 1)])
    await _issue_invite(db, user["_id"], user["email"], user["username"])
    await db.lab_users.update_one({"_id": user["_id"]}, {"$set": {"failed_logins": 0, "locked_until": None}})
    await audit(db, admin.id, "lab.invite_resent", app_id, {"user_id": user["_id"]})
    return {"username": user["username"], "email": user["email"], "invite_sent": True}
