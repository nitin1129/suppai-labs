"""Private file storage in MongoDB GridFS (bucket "files": collections files.files and files.chunks).

KYC documents and report PDFs live in the same database as everything else, so one MongoDB backup covers them.
Files are never public; they are opened through short-lived signed links served by GET /files/{key}.
"""
import time
from dataclasses import dataclass
from urllib.parse import quote

from bson import ObjectId
from bson.errors import InvalidId
from gridfs.errors import NoFile

from app.config import get_settings
from app.security import sign

BUCKET = "files"


@dataclass
class StoredFile:
    data: bytes
    filename: str
    content_type: str


class GridFSStorage:
    def __init__(self, bucket):
        self.bucket = bucket

    async def save(self, folder: str, filename: str, data: bytes, content_type: str) -> str:
        file_id = await self.bucket.upload_from_stream(
            filename, data, metadata={"folder": folder, "content_type": content_type}
        )
        return str(file_id)

    async def read(self, key: str) -> StoredFile:
        try:
            stream = await self.bucket.open_download_stream(ObjectId(key))
        except (InvalidId, NoFile):
            raise FileNotFoundError(key)
        meta = stream.metadata or {}
        return StoredFile(await stream.read(), stream.filename, meta.get("content_type", "application/octet-stream"))

    async def delete(self, key: str) -> None:
        try:
            await self.bucket.delete(ObjectId(key))
        except (InvalidId, NoFile):
            pass

    def signed_url(self, key: str) -> str:
        s = get_settings()
        expires = int(time.time()) + s.signed_url_seconds
        return f"{s.public_base_url}/files/{quote(key)}?e={expires}&s={sign(key, expires)}"


_storage: GridFSStorage | None = None


def get_storage() -> GridFSStorage:
    global _storage
    if _storage is None:
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket

        from app.db import get_db

        _storage = GridFSStorage(AsyncIOMotorGridFSBucket(get_db(), bucket_name=BUCKET))
    return _storage


def set_storage(storage) -> None:
    global _storage
    _storage = storage
