"""App settings, read from environment variables (or a local .env file)."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Known providers so the user only has to fill in their address and app password.
PROVIDERS = {
    "gmail": {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "imap_host": "imap.gmail.com"},
    "outlook": {"smtp_host": "smtp.office365.com", "smtp_port": 587, "imap_host": "outlook.office365.com"},
    "zoho": {"smtp_host": "smtp.zoho.in", "smtp_port": 587, "imap_host": "imap.zoho.in"},
}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    db_path: str
    database_url: str
    cron_secret: str
    max_send_per_run: int
    wa_token: str
    wa_phone_number_id: str
    wa_verify_token: str
    wa_app_secret: str
    wa_country_code: str
    provider: str
    email_address: str
    email_password: str
    sender_name: str
    smtp_host: str
    smtp_port: int
    imap_host: str
    imap_port: int
    imap_folder: str
    send_every_minutes: int
    reply_check_minutes: int
    send_hours: tuple[int, int]
    timezone: str
    app_password: str
    scheduler_enabled: bool

    @property
    def email_configured(self) -> bool:
        return bool(self.email_address and self.email_password and self.smtp_host)

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.wa_token and self.wa_phone_number_id)

    @property
    def imap_configured(self) -> bool:
        return bool(self.email_address and self.email_password and self.imap_host)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def get_settings() -> Settings:
    load_dotenv()
    provider = os.environ.get("EMAIL_PROVIDER", "gmail").lower()
    preset = PROVIDERS.get(provider, {})
    hours = os.environ.get("SEND_HOURS", "9-18")
    try:
        start, end = (int(x) for x in hours.split("-"))
    except ValueError:
        start, end = 0, 24
    return Settings(
        db_path=os.environ.get("LEADAPP_DB", str(ROOT / "data" / "leads.db")),
        database_url=os.environ.get("DATABASE_URL", ""),
        cron_secret=os.environ.get("CRON_SECRET", ""),
        max_send_per_run=_int("MAX_SEND_PER_RUN", 25),
        wa_token=os.environ.get("WHATSAPP_TOKEN", ""),
        wa_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        wa_verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        wa_app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
        wa_country_code=os.environ.get("WHATSAPP_COUNTRY_CODE", "91"),
        provider=provider,
        email_address=os.environ.get("EMAIL_ADDRESS", ""),
        email_password=os.environ.get("EMAIL_PASSWORD", ""),
        sender_name=os.environ.get("SENDER_NAME", ""),
        smtp_host=os.environ.get("SMTP_HOST", preset.get("smtp_host", "")),
        smtp_port=_int("SMTP_PORT", preset.get("smtp_port", 587)),
        imap_host=os.environ.get("IMAP_HOST", preset.get("imap_host", "")),
        imap_port=_int("IMAP_PORT", 993),
        imap_folder=os.environ.get("IMAP_FOLDER", "INBOX"),
        send_every_minutes=_int("SEND_EVERY_MINUTES", 30),
        reply_check_minutes=_int("REPLY_CHECK_MINUTES", 10),
        send_hours=(start, end),
        timezone=os.environ.get("TIMEZONE", "Asia/Kolkata"),
        app_password=os.environ.get("APP_PASSWORD", ""),
        scheduler_enabled=os.environ.get("SCHEDULER_ENABLED", "0" if os.environ.get("VERCEL") else "1") not in ("0", "false", "no"),
    )
