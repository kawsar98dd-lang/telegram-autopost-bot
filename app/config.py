"""Application configuration, loaded from environment variables / .env.

Uses only the standard library. All problems are collected and reported
together in one readable message so a non-programmer can fix .env in one go.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .security.crypto import CryptoError, parse_keys

_PLACEHOLDERS = {"", "change-me", "changeme", "secret", "password", "your-secret-here"}


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "Configuration problems (fix them in your .env file):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, comments, optional quotes, `export`."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        if key:
            values[key] = val
    return values


def normalize_database_url(url: str) -> str:
    """Accept postgres:// / postgresql:// (/ +asyncpg) and make it asyncpg-safe."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.split("+")[0]
    if scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must start with postgresql:// (or postgres://)")
    if not parts.hostname:
        raise ValueError("DATABASE_URL has no host")
    # Poolers such as Supabase add ?pgbouncer=true, which asyncpg rejects.
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "pgbouncer"]
    return urlunsplit(("postgresql", parts.netloc, parts.path, urlencode(query), ""))


@dataclass(frozen=True, repr=False)
class Settings:
    app_env: str
    app_url: str
    app_secret: str
    database_url: str
    session_encryption_key: str
    telegram_api_id: int | None
    telegram_api_hash: str
    log_level: str
    media_dir: Path
    max_upload_mb: int
    worker_poll_seconds: int
    post_min_interval_seconds: int
    max_posts_per_hour: int
    license_enforcement: bool
    license_server_url_override: str
    auto_migrate: bool
    session_idle_minutes: int
    session_max_hours: int
    trust_proxy_headers: bool
    setup_token: str

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def app_host(self) -> str:
        return urlsplit(self.app_url).netloc.lower()

    @property
    def cookie_secure(self) -> bool:
        return self.app_url.lower().startswith("https://")

    @property
    def telegram_configured(self) -> bool:
        return self.telegram_api_id is not None and bool(self.telegram_api_hash)

    def __repr__(self) -> str:  # never leak secrets through logs/tracebacks
        return (
            f"Settings(app_env={self.app_env!r}, app_url={self.app_url!r}, "
            f"telegram_configured={self.telegram_configured}, "
            f"license_enforcement={self.license_enforcement}, <secrets hidden>)"
        )


def _to_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _to_int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int, problems: list[str]) -> int:
    raw = env.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name} must be a whole number")
        return default
    if not lo <= value <= hi:
        problems.append(f"{name} must be between {lo} and {hi}")
    return value


def settings_from_env(env: Mapping[str, str]) -> Settings:
    problems: list[str] = []
    g = lambda k, d="": env.get(k, d).strip()  # noqa: E731

    app_env = g("APP_ENV", "production").lower()
    if app_env not in ("production", "development"):
        problems.append("APP_ENV must be 'production' or 'development'")
        app_env = "production"

    app_url = g("APP_URL", "http://localhost:8000").rstrip("/")
    if not re.match(r"^https?://[^/\s]+$", app_url):
        problems.append("APP_URL must look like https://poster.example.com (no path)")
    elif app_env == "production" and not app_url.startswith("https://"):
        problems.append("APP_URL must start with https:// in production")

    app_secret = g("APP_SECRET")
    if app_secret.lower() in _PLACEHOLDERS or app_secret.lower().startswith(("change", "replace")):
        problems.append("APP_SECRET is not set (run: python scripts/generate_keys.py)")
    elif app_env == "production" and len(app_secret) < 32:
        problems.append("APP_SECRET must be at least 32 characters")

    database_url = ""
    if not g("DATABASE_URL"):
        problems.append("DATABASE_URL is not set")
    else:
        try:
            database_url = normalize_database_url(g("DATABASE_URL"))
        except ValueError as exc:
            problems.append(str(exc))

    enc_key = g("SESSION_ENCRYPTION_KEY")
    if not enc_key:
        problems.append("SESSION_ENCRYPTION_KEY is not set (run: python scripts/generate_keys.py)")
    else:
        try:
            parse_keys(enc_key)
        except CryptoError as exc:
            problems.append(str(exc))

    api_id: int | None = None
    if g("TELEGRAM_API_ID"):
        if g("TELEGRAM_API_ID").isdigit():
            api_id = int(g("TELEGRAM_API_ID"))
        else:
            problems.append("TELEGRAM_API_ID must be a number (from https://my.telegram.org/apps)")

    log_level = g("LOG_LEVEL", "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        problems.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
        log_level = "INFO"

    enforcement = _to_bool(g("LICENSE_ENFORCEMENT"), True)
    if not enforcement and app_env == "production":
        problems.append("LICENSE_ENFORCEMENT can only be turned off when APP_ENV=development")
    override = g("LICENSE_SERVER_URL_OVERRIDE")
    if override and app_env == "production":
        problems.append("LICENSE_SERVER_URL_OVERRIDE is only allowed when APP_ENV=development")

    settings = Settings(
        app_env=app_env,
        app_url=app_url,
        app_secret=app_secret,
        database_url=database_url,
        session_encryption_key=enc_key,
        telegram_api_id=api_id,
        telegram_api_hash=g("TELEGRAM_API_HASH"),
        log_level=log_level,
        media_dir=Path(g("MEDIA_DIR", "./data/media")),
        max_upload_mb=_to_int(env, "MAX_UPLOAD_MB", 20, 1, 200, problems),
        worker_poll_seconds=_to_int(env, "WORKER_POLL_SECONDS", 5, 1, 300, problems),
        post_min_interval_seconds=_to_int(env, "POST_MIN_INTERVAL_SECONDS", 5, 1, 3600, problems),
        max_posts_per_hour=_to_int(env, "MAX_POSTS_PER_HOUR", 60, 1, 1000, problems),
        license_enforcement=enforcement,
        license_server_url_override=override,
        auto_migrate=_to_bool(g("AUTO_MIGRATE"), True),
        session_idle_minutes=_to_int(env, "SESSION_IDLE_MINUTES", 120, 5, 1440, problems),
        session_max_hours=_to_int(env, "SESSION_MAX_HOURS", 168, 1, 720, problems),
        trust_proxy_headers=_to_bool(g("TRUST_PROXY_HEADERS"), False),
        setup_token=g("SETUP_TOKEN"),
    )
    if problems:
        raise ConfigError(problems)
    return settings


def load_settings(dotenv_path: str | Path = ".env") -> Settings:
    """Real environment variables win over values from the .env file."""
    merged: dict[str, str] = {}
    path = Path(dotenv_path)
    if path.is_file():
        merged.update(parse_dotenv(path.read_text(encoding="utf-8")))
    merged.update(os.environ)
    return settings_from_env(merged)
