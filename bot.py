"""Telethon userbot listener for Telegram Vault.

Read this before changing anything here — every rule below exists because
breaking it costs accounts, not just uptime:

1. Read-only. This process never sends a message, joins a chat, or invites
   anyone. Keep it that way; a listener gets ignored, a sender gets banned.
2. One process per session file. Two processes on the same .session make
   Telegram revoke the auth key (AuthKeyDuplicatedError), and the re-login that
   usually follows is what actually gets numbers flagged.
3. Login is opt-in (ALLOW_NEW_LOGIN). There is no automatic code-request retry
   loop any more: repeatedly asking for a login code on one number is the
   fastest way to get it limited.
4. Real client identity. Announcing as "Telethon" is the easiest unofficial
   client fingerprint to spot, so device_model/system_version are filled in.
5. No per-message API calls. Entities come from the update cache
   (msg.sender / msg.chat) instead of get_sender()/get_chat(), and startup
   does not page through every dialog.
"""

import asyncio
import logging
import os
import sys
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import httpx
from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyError,
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    FloodWaitError,
)

import config
from config import API_ID, API_HASH, PHONE_NUMBER
from database import SessionLocal, Message, init_db

# Auth errors that mean "this session will never work again". Fatal, but the
# answer is never "delete the session and log in again" — see handle_fatal().
AUTH_FATAL_ERRORS = (
    AuthKeyError,
    AuthKeyUnregisteredError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

SESSION_FILE = f"{config.SESSION_NAME}.session"
LOCK_FILE = f"{config.SESSION_NAME}.lock"

logging.basicConfig(
    format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Dialogs are only read for the local dashboard, so a capped cache is enough
# and one request instead of a full pagination.
CHAT_TITLE_CACHE = {}
RECENT_KEYS = deque(maxlen=5000)
RECENT_SET = set()


# --------------------------------------------------------------------------
# Single-instance guard
# --------------------------------------------------------------------------
def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_session_lock(path):
    """Take an exclusive, crash-tolerant lock on the session.

    Atomic create + PID liveness instead of a byte-range lock: on Windows the
    byte-range variant happily succeeds twice inside one process, which is
    exactly the case that must be blocked (start.bat launching two children).
    Returns the lock path, or None if a live process already owns it.
    """
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                with open(path, "r", encoding="utf-8") as existing:
                    pid = int(existing.read().strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if _pid_alive(pid):
                return None
            # Stale lock left behind by a crash: reclaim it.
            try:
                os.remove(path)
            except OSError:
                return None
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return path
    return None


def release_session_lock(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def fatal_exit(code):
    """Exit after releasing the lock so the next start is not blocked."""
    release_session_lock(_STATE["lock"])
    sys.exit(code)


# --------------------------------------------------------------------------
# Proxy
# --------------------------------------------------------------------------
def build_proxy(raw):
    """Turn a proxy URL into what Telethon 1.36 expects.

    socks5://user:pass@host:port -> python-socks dict
    mtproxy://secret@host:port   -> (host, port, secret) tuple
    """
    if not raw:
        return None

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()

    if scheme in ("mtproxy", "tg"):
        host = parsed.hostname
        port = parsed.port or 443
        secret = unquote(parsed.username or "")
        return (host, port, secret)

    proxy_type = {"socks5": "socks5", "socks4": "socks4", "http": "http"}.get(scheme)
    if not proxy_type:
        logger.warning("Unsupported PROXY scheme %r, ignoring proxy.", scheme)
        return None

    return {
        "proxy_type": proxy_type,
        "addr": parsed.hostname,
        "port": parsed.port or (1080 if scheme.startswith("socks") else 8080),
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "rdns": True,
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def remember(key):
    """True the first time this (chat, message) pair is seen."""
    if key in RECENT_SET:
        return False
    if len(RECENT_KEYS) == RECENT_KEYS.maxlen:
        RECENT_SET.discard(RECENT_KEYS[0])
    RECENT_KEYS.append(key)
    RECENT_SET.add(key)
    return True


def display_name(entity):
    if entity is None:
        return "Unknown"
    name = " ".join(
        part for part in (
            getattr(entity, "first_name", "") or "",
            getattr(entity, "last_name", "") or "",
        ) if part
    ).strip()
    return name or getattr(entity, "username", None) or "Unknown"


_STATE = {"lock": None}


def handle_fatal(exc, reason):
    """Stop cleanly and leave an explanation a human can act on.

    The previous version told the operator to delete the session file, which
    turns one bad start into a login-code storm and a number ban. Never advise
    a re-login here unless a human explicitly decides to.
    """
    logger.critical("[FATAL] %s: %s: %s", reason, type(exc).__name__, exc)
    logger.critical(
        "Do NOT delete %s and log in again. A fresh login from the same host "
        "and number is what gets accounts limited. Resolve the cause first "
        "(duplicate process, revoked key, or banned account).", SESSION_FILE
    )
    try:
        with open("AUTH_ERROR.flag", "w", encoding="utf-8") as handle:
            handle.write(f"{type(exc).__name__}: {exc}\nreason={reason}\n")
    except OSError:
        pass
    release_session_lock(_STATE["lock"])
    sys.exit(2)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def register(client, http):
    @client.on(events.NewMessage())
    async def handler(event):
        # Private chats stay out of the archive on purpose.
        if event.is_private:
            return

        msg = event.message
        if not msg or not msg.message:
            return

        if not remember((event.chat_id, msg.id)):
            return

        try:
            # Cached entities: no API call, no flood risk.
            chat = msg.chat
            sender = msg.sender

            chat_id = event.chat_id
            if chat is not None and getattr(chat, "title", None):
                CHAT_TITLE_CACHE[chat_id] = chat.title
            chat_title = CHAT_TITLE_CACHE.get(chat_id, f"Chat {chat_id}")

            content = msg.message
            timestamp = msg.date.astimezone(timezone.utc).replace(tzinfo=None)
            user_id = event.sender_id
            username = getattr(sender, "username", None) if sender else None
            sender_name = display_name(sender)

            async with SessionLocal() as session:
                session.add(Message(
                    message_id=msg.id,
                    chat_id=chat_id,
                    chat_title=chat_title[:255] if chat_title else None,
                    user_id=user_id,
                    username=username,
                    sender_name=sender_name[:255],
                    content=content,
                    timestamp=timestamp,
                ))
                await session.commit()

            try:
                await http.post(config.BROADCAST_URL, json={
                    "chat_title": chat_title,
                    "chat_id": chat_id,
                    "sender_name": sender_name,
                    "username": username,
                    "user_id": user_id,
                    "content": content,
                    "timestamp": timestamp.isoformat(),
                }, timeout=2.0)
            except Exception:
                pass

        except Exception as exc:
            logger.error("Error: %s", exc)

    @client.on(events.MessageDeleted())
    async def delete_handler(event):
        if not event.deleted_ids:
            return

        # Bulk deletions are usually admin purges or channel wipes, not intel.
        if len(event.deleted_ids) > 5:
            return

        try:
            async with SessionLocal() as session:
                for msg_id in event.deleted_ids:
                    query = select(Message).where(Message.message_id == msg_id)
                    if event.chat_id:
                        query = query.where(Message.chat_id == event.chat_id)

                    result = await session.execute(query)
                    msg = result.scalars().first()
                    if not msg:
                        continue

                    # Delayed deletions (~24h / ~7d) are Telegram-side cleanup
                    # of service messages, not a human removing evidence.
                    age = (
                        datetime.now(timezone.utc).replace(tzinfo=None) - msg.timestamp
                    ).total_seconds()
                    if (86280 < age < 86520) or (604680 < age < 604920):
                        continue

                    msg.is_deleted = True
                    await session.commit()

                    try:
                        await http.post(config.BROADCAST_URL, json={
                            "type": "DELETED",
                            "message_id": msg_id,
                            "chat_id": msg.chat_id,
                        }, timeout=2.0)
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("Deletion log error: %s", exc)

    return handler, delete_handler


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
async def warm_entity_cache(client):
    """Small, bounded dialog read so titles are known without paging everything.

    A full get_dialogs() on a large account is a burst of API traffic right
    after login — exactly the pattern that produces FloodStrike.
    """
    try:
        await client.get_dialogs(limit=100)
    except FloodWaitError as exc:
        logger.warning("FloodWait while warming dialogs: sleeping %ss", exc.seconds + 10)
        await asyncio.sleep(exc.seconds + 10)
    except Exception as exc:
        logger.warning("Dialog warm-up skipped: %s", exc)


async def main():
    await init_db()

    if not API_ID or not API_HASH:
        logger.critical(
            "[FATAL] API_ID / API_HASH are missing. Fill them in .env (see "
            ".env.example) — one api_id per account, never shared between "
            "accounts or deployments."
        )
        sys.exit(6)

    # One process per session. Two is an instant auth-key revocation.
    lock = acquire_session_lock(LOCK_FILE)
    if lock is None:
        logger.critical(
            "[FATAL] Another instance already holds %s. A second process on the "
            "same session makes Telegram revoke the key (AuthKeyDuplicatedError). "
            "Close the other window (including a leftover start.bat child) and "
            "retry. Do not delete the session.", LOCK_FILE
        )
        sys.exit(3)

    _STATE["lock"] = lock

    client = TelegramClient(
        config.SESSION_NAME,
        API_ID,
        API_HASH,
        device_model=config.DEVICE_MODEL,
        system_version=config.SYSTEM_VERSION,
        app_version=config.APP_VERSION,
        lang_code=config.LANG_CODE,
        system_lang_code=config.LANG_PACK,
        proxy=build_proxy(config.PROXY),
        connection_retries=5,
        retry_delay=2,
        request_retries=3,
        timeout=30,
        auto_reconnect=True,
        # Let Telethon absorb small floods instead of raising into our loop,
        # where retrying would amplify them.
        flood_sleep_threshold=60,
        sequential_updates=True,
        # Process only live updates. Catching up replays a backlog of hundreds
        # of messages in parallel and looks like scraping.
        catch_up=False,
    )

    http = httpx.AsyncClient()
    register(client, http)

    try:
        await client.connect()
    except AuthKeyDuplicatedError as exc:
        handle_fatal(exc, "session used by two processes at once")
    except Exception as exc:
        handle_fatal(exc, "cannot connect to Telegram")

    if not await client.is_user_authorized():
        if not config.ALLOW_NEW_LOGIN:
            logger.critical(
                "[FATAL] %s is not authorized. Refusing to request a login code "
                "automatically — that loop is what gets numbers flagged. Log in "
                "once from a normal client, or set ALLOW_NEW_LOGIN=1 for a single "
                "deliberate first run.", SESSION_FILE
            )
            fatal_exit(4)
        if not PHONE_NUMBER:
            logger.critical(
                "[FATAL] ALLOW_NEW_LOGIN=1 but PHONE_NUMBER is empty. Telethon "
                "would block on an interactive prompt."
            )
            fatal_exit(6)
        logger.warning("ALLOW_NEW_LOGIN=1 — requesting a login code once.")
        try:
            await client.start(phone=PHONE_NUMBER)
        except AuthKeyDuplicatedError as exc:
            handle_fatal(exc, "session used by two processes at once")
        except AUTH_FATAL_ERRORS as exc:
            handle_fatal(exc, "login rejected")
        except FloodWaitError as exc:
            logger.critical("FloodWait on login: wait %ss, then retry manually.", exc.seconds)
            fatal_exit(5)

    await warm_entity_cache(client)
    logger.info("System active. Listening to live updates only.")

    attempt = 0
    while True:
        try:
            await client.run_until_disconnected()
            if not client.is_connected():
                raise ConnectionError("client disconnected")
        except AuthKeyDuplicatedError as exc:
            handle_fatal(exc, "session used by two processes at once")
        except AUTH_FATAL_ERRORS as exc:
            handle_fatal(exc, "auth rejected")
        except FloodWaitError as exc:
            wait = exc.seconds + 10
            logger.warning("FloodWait: sleeping %ss", wait)
            await asyncio.sleep(wait)
        except Exception as exc:
            attempt += 1
            if attempt > 5:
                logger.critical("Reconnect failed 5 times in a row: %s", exc)
                break
            wait = min(5 * attempt, 60)
            logger.error("Connection lost (%d/5): %s — retry in %ss", attempt, exc, wait)
            await asyncio.sleep(wait)
            try:
                await client.connect()
            except Exception as reconnect_exc:
                logger.error("Reconnect error: %s", reconnect_exc)

    await http.aclose()
    try:
        await client.disconnect()
    finally:
        release_session_lock(lock)
    sys.exit(1)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by operator.")

