import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings


def now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    return bcrypt.checkpw(password.encode(), hashed.encode())


def new_token() -> str:
    return secrets.token_urlsafe(32)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_access_token(sub: str, role: str, **claims) -> str:
    s = get_settings()
    payload = {
        "sub": sub,
        "role": role,
        "iat": now(),
        "exp": now() + timedelta(minutes=s.access_token_minutes),
        **claims,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])


def sign(value: str, expires_at: int) -> str:
    key = get_settings().jwt_secret.encode()
    return hmac.new(key, f"{value}:{expires_at}".encode(), hashlib.sha256).hexdigest()


def verify_signature(value: str, expires_at: int, signature: str) -> bool:
    if expires_at < int(now().timestamp()):
        return False
    return hmac.compare_digest(sign(value, expires_at), signature)


PASSWORD_MIN_LENGTH = 10


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    if not any(c.isdigit() for c in password):
        problems.append("a number")
    if not any(c.isalpha() for c in password):
        problems.append("a letter")
    return problems


# Slots, "today" and paused dates are in India time regardless of the server's timezone.
from zoneinfo import ZoneInfo  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def local_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def local_today():
    return local_now().date()
