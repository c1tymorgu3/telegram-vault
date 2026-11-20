import os
from dotenv import load_dotenv

load_dotenv()

# Account label. When set (e.g. ACCOUNT=alt1), API_ID_ALT1 / API_HASH_ALT1
# override the generic values, so every account can carry its own api_id
# instead of sharing one cluster identity (shared api_id + shared IP is what
# links accounts together once one of them gets flagged).
ACCOUNT = os.getenv("ACCOUNT", "").strip().lower()


def _account_env(name, default=None):
    if ACCOUNT:
        value = os.getenv(f"{name}_{ACCOUNT.upper()}")
        if value not in (None, ""):
            return value
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value


API_ID = int(_account_env("API_ID", "0"))
API_HASH = _account_env("API_HASH", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./telegramlogs.db")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", None)
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-secret-key-immediately")
HCAPTCHA_SITE_KEY = os.getenv("HCAPTCHA_SITE_KEY", "")
HCAPTCHA_SECRET_KEY = os.getenv("HCAPTCHA_SECRET_KEY", "")

# --- Client identity -------------------------------------------------------
# A userbot that announces itself as "Telethon" is the easiest unofficial
# client fingerprint to spot. Keep these consistent with the device you would
# normally sign in from.
SESSION_NAME = os.getenv("SESSION_NAME", "userbot_session")
DEVICE_MODEL = os.getenv("DEVICE_MODEL", "Samsung SM-A546B")
SYSTEM_VERSION = os.getenv("SYSTEM_VERSION", "SDK 33")
APP_VERSION = os.getenv("APP_VERSION", "10.9.1 (4665)")
LANG_CODE = os.getenv("LANG_CODE", "en")
LANG_PACK = os.getenv("LANG_PACK", "en")

# One account, one egress address. Supports socks5://, socks4://, http:// or
# an MTProxy secret URL.
PROXY = os.getenv("PROXY", "")

# Creating a session from this process means asking Telegram for a login code.
# That request loop is what gets numbers flagged, so it is opt-in: log in once
# from a normal client, or set ALLOW_NEW_LOGIN=1 for a single first run only.
ALLOW_NEW_LOGIN = os.getenv("ALLOW_NEW_LOGIN", "0") == "1"

# Local dashboard endpoint that bot.py forwards live events to.
BROADCAST_URL = os.getenv("BROADCAST_URL", "http://127.0.0.1:8000/internal/broadcast")
