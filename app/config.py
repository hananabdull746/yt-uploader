from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()


def oauth_base_url(base_url: str) -> str:
    """Use localhost for IP-based OAuth callback hosts (e.g. SSH-tunnel setups)."""
    value = base_url.rstrip("/")
    parsed = urlsplit(value)
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        return value
    port = parsed.port
    netloc = f"localhost:{port}" if port else "localhost"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class Settings:
    app_name: str = "YT Uploader"
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    database_path: Path = Path(os.getenv("DATABASE_PATH", "./data/yt_uploader.sqlite3"))
    downloads_path: Path = Path(os.getenv("DOWNLOADS_PATH", "./downloads"))
    log_path: Path = Path(os.getenv("LOG_PATH", "./logs/app.log"))
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    session_secret: str = os.getenv("SESSION_SECRET", "")
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "")
    base_url: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    poll_interval: int = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "300")))
    max_download_bytes: int = max(1, int(os.getenv("MAX_DOWNLOAD_BYTES", str(4 * 1024**3))))
    request_timeout: int = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")))
    worker_enabled_on_start: bool = os.getenv("WORKER_ENABLED_ON_START", "false").lower() == "true"

    @property
    def oauth_redirect_uri(self) -> str:
        return oauth_base_url(self.base_url) + "/oauth/callback"

    @property
    def data_dir(self) -> Path:
        return self.database_path.parent


settings = Settings()
