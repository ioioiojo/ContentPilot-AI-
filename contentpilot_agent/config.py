import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    database_url: str = "sqlite:///./data/contentpilot.db"
    cookie_secure: bool = False
    session_hours: int = 12
    job_timeout: int = 180
    lease_seconds: int = 30
    max_attempts: int = 3
    provider: str = "mock"
    tools_mode: str = "local"
    allowed_hosts: tuple[str, ...] = ("*",)
    max_active_jobs: int = 10
    daily_jobs: int = 50
    max_active_media: int = 5
    daily_media: int = 20
    submissions_enabled: bool = True

    def __post_init__(self):
        if self.app_env not in {"development", "staging", "production"}:
            raise ValueError("APP_ENV must be development, staging or production")
        if any(
            n < 1
            for n in (
                self.max_active_jobs,
                self.daily_jobs,
                self.max_active_media,
                self.daily_media,
                self.max_attempts,
            )
        ):
            raise ValueError("Task limits must be positive")
        if self.app_env == "production":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL (psycopg)")
            if not self.cookie_secure:
                raise ValueError("Production requires COOKIE_SECURE=true and HTTPS")
            if not self.allowed_hosts or any(
                "*" in host or "example.com" in host for host in self.allowed_hosts
            ):
                raise ValueError("Production requires explicit real ALLOWED_HOSTS")
            if not {"localhost", "127.0.0.1"}.issubset(self.allowed_hosts):
                raise ValueError("ALLOWED_HOSTS must include localhost,127.0.0.1 for probes")
            if self.provider != "compatible":
                raise ValueError("Production requires a real model; use staging for Mock")

    @classmethod
    def from_env(cls):
        load_dotenv()
        provider = os.getenv("CONTENTPILOT_PROVIDER", "mock")
        if provider not in {"mock", "compatible"}:
            raise ValueError("CONTENTPILOT_PROVIDER must be mock or compatible")
        tools_mode = os.getenv("CONTENTPILOT_TOOLS", "mcp")
        if tools_mode not in {"local", "mcp"}:
            raise ValueError("CONTENTPILOT_TOOLS must be local or mcp")
        if os.getenv("CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER", "auto") not in {
            "auto",
            "tikhub",
            "gateway",
        }:
            raise ValueError("Search provider must be auto, tikhub or gateway")
        settings = cls(
            app_env=os.getenv("APP_ENV", "development"),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/contentpilot.db"),
            cookie_secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
            job_timeout=max(5, min(1800, int(os.getenv("JOB_TIMEOUT_SECONDS", "180")))),
            provider=provider,
            tools_mode=tools_mode,
            max_attempts=max(1, min(3, int(os.getenv("JOB_MAX_ATTEMPTS", "1")))),
            max_active_jobs=int(os.getenv("MAX_ACTIVE_JOBS", "10")),
            daily_jobs=int(os.getenv("DAILY_JOBS", "50")),
            max_active_media=int(os.getenv("MAX_ACTIVE_MEDIA", "5")),
            daily_media=int(os.getenv("DAILY_MEDIA", "20")),
            submissions_enabled=os.getenv("SUBMISSIONS_ENABLED", "true").lower() == "true",
            allowed_hosts=tuple(
                x.strip() for x in os.getenv("ALLOWED_HOSTS", "*").split(",") if x.strip()
            ),
        )
        if settings.app_env == "production":
            if (
                not os.getenv("OPENAI_API_KEY", "").strip()
                or not os.getenv("OPENAI_MODEL", "").strip()
            ):
                raise ValueError("Production requires OPENAI_API_KEY and OPENAI_MODEL")
            base = os.getenv("OPENAI_BASE_URL", "") or "https://api.openai.com/v1"
            parsed = urlsplit(base)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Production model endpoint must use HTTPS without URL credentials")
        return settings
