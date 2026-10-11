from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "suppai_labs"
    # Multi-document transactions need a replica set (Atlas has one). Turn off for a plain local mongod.
    use_transactions: bool = True

    # Shared with the main SuppAI auth so member tokens issued there are accepted here.
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 12

    invite_link_base: str = "https://suppai.in/lab/set-password"
    invite_expiry_hours: int = 24

    login_max_failures: int = 5
    login_lock_minutes: int = 15

    # "local" for development, "s3" for production.
    storage_backend: str = "local"
    local_storage_dir: str = "./storage"
    s3_bucket: str = ""
    s3_region: str = "ap-south-1"
    signed_url_seconds: int = 300
    public_base_url: str = "http://localhost:8000"

    max_upload_mb: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
