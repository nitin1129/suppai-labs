from datetime import timedelta, timezone

from fastapi import APIRouter, HTTPException, status

from app.config import get_settings
from app.db import get_db
from app.schemas import LoginRequest, SetPasswordRequest, TokenResponse
from app.security import (
    create_access_token,
    hash_password,
    now,
    password_problems,
    sha256,
    verify_password,
)
from app.services.common import audit

router = APIRouter(prefix="/auth", tags=["Auth"])

BAD_LOGIN = "Wrong username or password"


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


@router.post("/lab/login", response_model=TokenResponse)
async def lab_login(body: LoginRequest):
    db, s = get_db(), get_settings()
    ident = body.username.strip().lower()
    user = await db.lab_users.find_one({"$or": [{"username": ident}, {"email": ident}]})
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_LOGIN)

    locked_until = _aware(user.get("locked_until"))
    if locked_until and locked_until > now():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again later.")
    if user.get("must_set_password"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Set your password using the link in your invite email")

    if not verify_password(body.password, user.get("password_hash")):
        failures = user.get("failed_logins", 0) + 1
        update = {"failed_logins": failures}
        if failures >= s.login_max_failures:
            update = {"failed_logins": 0, "locked_until": now() + timedelta(minutes=s.login_lock_minutes)}
        await db.lab_users.update_one({"_id": user["_id"]}, {"$set": update})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_LOGIN)

    lab = await db.labs.find_one({"_id": user["lab_id"]}, {"status": 1})
    if not lab or lab["status"] != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This lab is not active")

    await db.lab_users.update_one(
        {"_id": user["_id"]}, {"$set": {"failed_logins": 0, "locked_until": None, "last_login_at": now()}}
    )
    return TokenResponse(access_token=create_access_token(user["_id"], "lab", lab_id=user["lab_id"]))


@router.post("/lab/set-password", response_model=TokenResponse)
async def lab_set_password(body: SetPasswordRequest):
    db = get_db()
    user = await db.lab_users.find_one({"invite_token_hash": sha256(body.token)})
    expires = _aware(user.get("invite_expires_at")) if user else None
    if not user or not expires or expires < now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link is invalid or has expired. Ask SuppAI for a new one.")
    problems = password_problems(body.password)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Password needs " + ", ".join(problems))

    await db.lab_users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": hash_password(body.password),
                "must_set_password": False,
                "password_set_at": now(),
                "failed_logins": 0,
                "locked_until": None,
            },
            "$unset": {"invite_token_hash": "", "invite_expires_at": ""},
        },
    )
    await audit(db, user["_id"], "lab_user.password_set", user["lab_id"])
    return TokenResponse(access_token=create_access_token(user["_id"], "lab", lab_id=user["lab_id"]))


@router.post("/admin/login", response_model=TokenResponse)
async def admin_login(body: LoginRequest):
    admin = await get_db().admin_users.find_one({"email": body.username.strip().lower()})
    if not admin or not verify_password(body.password, admin.get("password_hash")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_LOGIN)
    return TokenResponse(access_token=create_access_token(admin["_id"], "admin"))
