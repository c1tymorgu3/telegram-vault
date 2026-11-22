import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

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

# --- Startup security gate ------------------------------------------------
# A panel that boots with the shipped defaults is a public login form with a
# known password, and an itsdangerous cookie signed with a secret that is in
# the repository. validate_security_settings() refuses to start unless the
# values look real; app.py calls it from the lifespan startup hook so that
# importing this module (tests, CLI helpers) never blows up.
DEFAULT_DASHBOARD_PASSWORD = "admin"
DEFAULT_SESSION_SECRET = "change-this-secret-key-immediately"
INSECURE_PASSWORDS = {"", "admin", "password", "changeme", "change-me", "123456", "test"}
MIN_PASSWORD_LENGTH = 12
MIN_SECRET_LENGTH = 32

# Escape hatch for tests and local development only: log a warning instead of
# refusing to start. Never set this in production.
ALLOW_INSECURE_DEFAULTS = os.getenv("ALLOW_INSECURE_DEFAULTS", "0") == "1"

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

# Session cookie over HTTPS only. Keep 1 in production; set 0 for local
# http://127.0.0.1 runs, otherwise the login cookie is never sent back and the
# dashboard loops on /login.
SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY", "1") == "1"

# Behind a reverse proxy, trust X-Forwarded-* so the rate limiter sees the real
# client IP instead of the proxy's.
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"

# Local dashboard endpoint that bot.py forwards live events to.
BROADCAST_URL = os.getenv("BROADCAST_URL", "http://127.0.0.1:8000/internal/broadcast")

# Shared secret for the bot -> panel internal endpoints, sent as X-Internal-Token.
# Empty means "not configured": the internal routes then rely on the loopback
# source check alone, so a single-host install keeps working without extra setup.
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")


def validate_security_settings(password=None, session_secret=None, allow_insecure=None):
    """Panelin varsayılan/zayıf kimlik bilgileriyle açılmasını engelle.

    Saf fonksiyon: env okumaz, import anında çalışmaz. Argüman verilmezse
    modül seviyesindeki değerler kullanılır. Sorun bulunursa RuntimeError
    fırlatır; ALLOW_INSECURE_DEFAULTS=1 ise yalnızca uyarı loglayıp sorunların
    listesini döndürür (test/geliştirme kaçış kapısı).

    Dönüş: bulunan sorunların listesi (boş liste = yapılandırma temiz).
    """
    if password is None:
        password = DASHBOARD_PASSWORD
    if session_secret is None:
        session_secret = SESSION_SECRET
    if allow_insecure is None:
        allow_insecure = ALLOW_INSECURE_DEFAULTS

    problems = []

    candidate = (password or "").strip()
    if candidate.lower() in INSECURE_PASSWORDS:
        problems.append(
            "DASHBOARD_PASSWORD tanımlı değil veya varsayılan. "
            ".env dosyasına güçlü bir şifre yazın."
        )
    elif len(candidate) < MIN_PASSWORD_LENGTH:
        problems.append(
            f"DASHBOARD_PASSWORD çok kısa ({len(candidate)} karakter). "
            f"En az {MIN_PASSWORD_LENGTH} karakter olmalı."
        )

    secret = (session_secret or "").strip()
    if not secret or secret == DEFAULT_SESSION_SECRET:
        problems.append(
            "SESSION_SECRET tanımlı değil veya varsayılan. "
            ".env dosyasına rastgele üretilmiş uzun bir secret yazın."
        )
    elif len(secret) < MIN_SECRET_LENGTH:
        problems.append(
            f"SESSION_SECRET çok kısa ({len(secret)} karakter). "
            f"En az {MIN_SECRET_LENGTH} karakter olmalı."
        )

    if not problems:
        return []

    message = "Güvenlik yapılandırması eksik, panel başlatılmadı:\n  - " + "\n  - ".join(problems)
    if allow_insecure:
        logger.warning("%s\nALLOW_INSECURE_DEFAULTS=1 olduğu için devam ediliyor.", message)
        return problems
    raise RuntimeError(
        message + "\n\nDüzeltme: .env dosyasına güçlü bir DASHBOARD_PASSWORD ve "
        f"en az {MIN_SECRET_LENGTH} karakterlik SESSION_SECRET yazın. "
        "Sadece test/geliştirme için ALLOW_INSECURE_DEFAULTS=1 kullanılabilir."
    )
