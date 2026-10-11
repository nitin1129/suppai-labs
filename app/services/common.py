import logging
import re
import uuid

from pymongo import ReturnDocument

from app.security import now

log = logging.getLogger("suppai.labs")


def new_id() -> str:
    return uuid.uuid4().hex


async def next_number(db, name: str, start: int = 1000) -> int:
    doc = await db.counters.find_one_and_update(
        {"_id": name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return start + doc["value"]


async def audit(db, actor: str, action: str, target: str, details: dict | None = None, session=None) -> None:
    await db.audit_log.insert_one(
        {"_id": new_id(), "actor": actor, "action": action, "target": target, "details": details or {}, "at": now()},
        session=session,
    )


async def notify(db, to: dict, template: str, data: dict) -> None:
    """Queue a notification. A worker sends it by SMS, WhatsApp or email.

    `to` is e.g. {"member_id": ...} or {"lab_id": ...} or {"email": ...}.
    Sending is deliberately out of this service so a provider outage never blocks a status change.
    """
    await db.notifications.insert_one(
        {"_id": new_id(), "to": to, "template": template, "data": data, "status": "queued", "created_at": now()}
    )
    log.info("notification queued: %s -> %s", template, to)


def username_base(legal_name: str) -> str:
    words = re.findall(r"[a-z0-9]+", legal_name.lower())
    stop = {"pvt", "private", "ltd", "limited", "llp", "the", "and", "opc"}
    words = [w for w in words if w not in stop] or words or ["lab"]
    return ".".join(words[:4])


async def unique_username(db, legal_name: str) -> str:
    base = username_base(legal_name)
    candidate, n = f"{base}.ops", 2
    while await db.lab_users.find_one({"username": candidate}, {"_id": 1}):
        candidate, n = f"{base}{n}.ops", n + 1
    return candidate
