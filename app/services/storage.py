"""Private file storage. Files are never public; they are opened through short-lived signed links."""
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


class S3Storage:
    def __init__(self, bucket: str, region: str):
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region)

    def save(self, folder: str, filename: str, data: bytes, content_type: str) -> str:
        key = f"{folder}/{uuid.uuid4().hex}-{Path(filename).name}"
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256"
        )
        return key

    def signed_url(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=get_settings().signed_url_seconds,
        )


_storage = None


def get_storage():
    global _storage
    if _storage is None:
        s = get_settings()
        _storage = S3Storage(s.s3_bucket, s.s3_region) if s.storage_backend == "s3" else LocalStorage(s.local_storage_dir)
    return _storage


def set_storage(storage) -> None:
    global _storage
    _storage = storage
