"""Settings, read from environment variables (set by deploy/install.sh in /etc/home-energy.env)."""
import json
import os
import secrets
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
PEOPLE = [p.strip() for p in os.environ.get("PEOPLE", "Billy,Wife").split(",") if p.strip()]
LAT = float(os.environ.get("LAT", "51.81"))   # Tiptree
LON = float(os.environ.get("LON", "0.75"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "5"))

DB_PATH = DATA_DIR / "home.db"
SECRETS_PATH = DATA_DIR / "secrets.json"
_lock = threading.Lock()


def load_secrets() -> dict:
    """Credentials gathered through the Setup page (Hive device keys, Tuya tokens, webhook tokens)."""
    with _lock:
        if SECRETS_PATH.exists():
            return json.loads(SECRETS_PATH.read_text())
        return {}


def save_secrets(update: dict) -> dict:
    with _lock:
        data = json.loads(SECRETS_PATH.read_text()) if SECRETS_PATH.exists() else {}
        data.update(update)
        tmp = SECRETS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(SECRETS_PATH)
        return data


def webhook_token() -> str:
    """Secret token the iPhone Shortcuts use to report arrive/leave."""
    s = load_secrets()
    if "webhook_token" not in s:
        s = save_secrets({"webhook_token": secrets.token_urlsafe(24)})
    return s["webhook_token"]
