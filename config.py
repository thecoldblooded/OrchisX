import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Core paths & storage
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./x_scraper.db",
        description="Async SQLite database connection URL"
    )
    PROXY_FILE_PATH: str = Field(
        default="./proxies.txt",
        description="Path to proxy list text file (format: ip:port:user:pass or protocol://user:pass@ip:port)"
    )
    EXPORTS_DIR: str = Field(
        default="./exports",
        description="Directory for saved bulk extraction files (CSV/JSON)"
    )

    # Security & API
    API_KEY_SECRET: str = Field(
        default="local-dev-key",
        description="Master API key for securing REST endpoints"
    )
    API_HOST: str = Field(default="0.0.0.0", description="API host")
    API_PORT: int = Field(default=8000, description="API port")

    # Anti-bot & Camofox
    CAMOFOX_URL: str = Field(
        default="http://localhost:9377",
        description="Camofox anti-detection browser service URL"
    )
    DEFAULT_REQUEST_TIMEOUT: float = Field(
        default=20.0,
        description="Default request timeout in seconds"
    )
    MAX_RETRIES_PER_QUERY: int = Field(
        default=3,
        description="Maximum retry attempts per query across proxies/accounts"
    )
    RATE_LIMIT_COOLDOWN_SECONDS: int = Field(
        default=900,
        description="Rate limit cooldown period in seconds (15 mins default)"
    )

    # Twitter Protocol Defaults
    DEFAULT_BEARER_TOKEN: str = Field(
        default="Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        description="Standard Twitter Web public guest bearer token"
    )
    DEFAULT_USER_AGENT: str = Field(
        default="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        description="Default browser User-Agent for requests"
    )

    # Webhook defaults
    WEBHOOK_RETRY_ATTEMPTS: int = Field(default=3, description="Number of webhook delivery attempts")
    WEBHOOK_TIMEOUT: float = Field(default=10.0, description="Webhook delivery request timeout in seconds")


settings = Settings()

# Ensure required directories exist
os.makedirs(settings.EXPORTS_DIR, exist_ok=True)
