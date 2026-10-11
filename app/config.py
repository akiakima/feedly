"""環境変数から設定を読み込む。.env があれば起動時に読み込む。"""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str
    timezone: str
    fetch_interval_minutes: int
    retention_days: int
    seed_presets: bool
    enable_scheduler: bool
    app_base_url: str
    app_username: str
    app_password: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_use_ssl: bool
    mail_from: str
    mail_to: str
    digest_times: str


def load_settings() -> Settings:
    default_db = f"sqlite:///{BASE_DIR / 'data' / 'feedly.db'}"
    return Settings(
        database_url=os.environ.get("DATABASE_URL", default_db),
        timezone=os.environ.get("TIMEZONE", "Asia/Tokyo"),
        fetch_interval_minutes=int(os.environ.get("FETCH_INTERVAL_MINUTES", "30")),
        retention_days=int(os.environ.get("RETENTION_DAYS", "30")),
        seed_presets=_bool("SEED_PRESETS", True),
        enable_scheduler=_bool("ENABLE_SCHEDULER", True),
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        app_username=os.environ.get("APP_USERNAME", ""),
        app_password=os.environ.get("APP_PASSWORD", ""),
        smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_use_ssl=_bool("SMTP_USE_SSL", False),
        mail_from=os.environ.get("MAIL_FROM", ""),
        mail_to=os.environ.get("MAIL_TO", ""),
        digest_times=os.environ.get("DIGEST_TIMES", "07:00,18:00"),
    )


settings = load_settings()
