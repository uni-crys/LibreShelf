from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    GOOGLE_BOOKS_API_KEY: str = ""
    READMOO_SYNC_TOKEN: str = ""
    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    LIBROVIA_DATABASE_PATH: Path = BACKEND_DIR / "data" / "ebooks.db"
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.CORS_ORIGINS.split(",")
            if origin.strip()
        ]

    @model_validator(mode="after")
    def validate_production_settings(self):
        if self.ENVIRONMENT != "production":
            return self
        if len(self.READMOO_SYNC_TOKEN) < 32:
            raise ValueError(
                "production requires READMOO_SYNC_TOKEN with at least 32 characters"
            )
        if not self.cors_origins or "*" in self.cors_origins:
            raise ValueError(
                "production requires explicit CORS_ORIGINS; wildcard is forbidden"
            )
        if any("localhost" in origin or "127.0.0.1" in origin for origin in self.cors_origins):
            raise ValueError(
                "production CORS_ORIGINS must not contain localhost"
            )
        if not self.LIBROVIA_DATABASE_PATH.is_absolute():
            raise ValueError(
                "production requires an absolute LIBROVIA_DATABASE_PATH"
            )
        return self

settings = Settings()
