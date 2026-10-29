import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    redis_broker_url: str = os.getenv("REDIS_BROKER_URL", "redis://localhost:6379/0")
    redis_backend_url: str = os.getenv("REDIS_BACKEND_URL", "redis://localhost:6379/1")
    web_dir: str = os.getenv("WEB_DIR", "web")
    max_images: int = int(os.getenv("MAX_IMAGES", "10"))
    max_image_bytes: int = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
    result_ttl_seconds: int = int(os.getenv("RESULT_TTL_SECONDS", "3600"))


settings = Settings()
