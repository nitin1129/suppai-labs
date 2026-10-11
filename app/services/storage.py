"""Private file storage on the server's disk (LOCAL_STORAGE_DIR).

Files are never public; they are opened through short-lived signed links served by /files/.
"""
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from app.config import get_settings
from app.security import sign


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root)

    def save(self, folder: str, filename: str, data: bytes, content_type: str) -> str:
        key = f"{folder}/{uuid.uuid4().hex}-{Path(filename).name}"
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def signed_url(self, key: str) -> str:
        s = get_settings()
        expires = int(time.time()) + s.signed_url_seconds
        return f"{s.public_base_url}/files/{quote(key)}?e={expires}&s={sign(key, expires)}"

    def read(self, key: str) -> bytes:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise FileNotFoundError(key)
        return path.read_bytes()


_storage = None


def get_storage() -> LocalStorage:
    global _storage
    if _storage is None:
        _storage = LocalStorage(get_settings().local_storage_dir)
    return _storage


def set_storage(storage) -> None:
    global _storage
    _storage = storage
