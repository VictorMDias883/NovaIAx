import json
import os
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DownstreamService(BaseModel):
    name: str
    base_url: str
    timeout_seconds: int = 5
    allowed_paths: list[str] | None = None


class Settings(BaseSettings):
    app_name: str = "novaiax-gateway"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = Field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me-in-production"))
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    redis_url: str = Field(default_factory=lambda: os.getenv("REDIS_URL", "redis://redis:6379/0"))
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"])
    api_key_header: str = "X-API-Key"
    default_admin_username: str = "admin"
    default_admin_password: str = Field(default_factory=lambda: os.getenv("DEFAULT_ADMIN_PASSWORD", "changeme123"))
    master_api_key: str | None = Field(default_factory=lambda: os.getenv("MASTER_API_KEY"))
    rate_limit_default: int = 60
    rate_limit_ai: int = 10
    cache_ttl_default: int = 60
    cache_ttl_ai: int = 300
    max_payload_bytes: int = 1024 * 1024
    downstream_services_json: str = Field(
        default_factory=lambda: os.getenv(
            "DOWNSTREAM_SERVICES_JSON",
            '[{"name":"ai","base_url":"http://mock-ai-service:8001","timeout_seconds":5}]',
        )
    )

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    @property
    def downstream_services(self) -> dict[str, DownstreamService]:
        data = json.loads(self.downstream_services_json)
        services = {}
        for item in data:
            service = DownstreamService(**item)
            services[service.name] = service
        return services


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
