from contextlib import asynccontextmanager

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_settings

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def set_database(client, db) -> None:
    """Used by app startup and by tests (to inject an in-memory database)."""
    global _client, _db
    _client, _db = client, db


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        s = get_settings()
        set_database(c := AsyncIOMotorClient(s.mongo_uri), c[s.mongo_db])
    return _db


@asynccontextmanager
async def transaction():
    """Yields a session inside a transaction, or None when transactions are disabled."""
    if not get_settings().use_transactions or _client is None:
        yield None
        return
    async with await _client.start_session() as session:
        async with session.start_transaction():
            yield session


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.lab_applications.create_index("reference", unique=True, sparse=True)
    await db.lab_applications.create_index("status")
    await db.labs.create_index("application_id", unique=True)
    await db.lab_users.create_index("username", unique=True)
    await db.lab_users.create_index("email")
    await db.lab_users.create_index("invite_token_hash", sparse=True)
    await db.lab_service_areas.create_index([("pincode", 1), ("lab_id", 1)], unique=True)
    await db.lab_leave.create_index([("lab_id", 1), ("date", 1)], unique=True)
    await db.orders.create_index([("lab_id", 1), ("status", 1), ("slot_date", 1)])
    await db.orders.create_index("member_id")
    await db.orders.create_index("reference", unique=True)
    await db.order_events.create_index([("order_id", 1), ("at", 1)])
    await db.admin_users.create_index("email", unique=True)
    await db["files.files"].create_index("metadata.folder")
