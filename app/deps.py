from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.security import decode_access_token

bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    id: str
    role: str
    lab_id: str | None = None


def _principal(creds: HTTPAuthorizationCredentials | None) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    try:
        claims = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return Principal(id=claims["sub"], role=claims.get("role", ""), lab_id=claims.get("lab_id"))


def require_role(role: str):
    def dep(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        p = _principal(creds)
        if p.role != role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
        if role == "lab" and not p.lab_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Token has no lab")
        return p

    return dep


current_lab = require_role("lab")
current_admin = require_role("admin")
current_member = require_role("member")
