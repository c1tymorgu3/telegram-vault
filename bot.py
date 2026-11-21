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
import random
import sys
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import httpx
from sqlalchemy import select, update, func
from telethon import TelegramClient, events, utils
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
from database import SessionLocal, Message, Chat, init_db

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
CHAT_SYNC_LIMIT = 200
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


# user_id -> (username, display name). Filled lazily by resolve_senders() so a
# message never waits on a network round trip before it is stored.
SENDER_CACHE = {}
PENDING_SENDERS = set()


def _chat_title_hint(msg, chat_id):
    """Title without touching the network: cache, author, or a readable stub."""
    author = getattr(msg, "post_author", None)
    if author:
        return str(author)[:255]
    return f"Chat {chat_id}"


def sender_identity(msg, user_id):
    """Name for a sender, from cache when possible; queue it when not."""
    cached = SENDER_CACHE.get(user_id)
    if cached:
        return cached
    author = getattr(msg, "post_author", None)
    name = str(author)[:255] if author else (f"User {user_id}" if user_id else "Unknown")
    if user_id:
        PENDING_SENDERS.add(user_id)
    return None, name


async def resolve_senders(client):
    """Resolve queued sender ids a few at a time, then rewrite stored rows."""
    if not PENDING_SENDERS:
        return
    batch = [uid for uid in list(PENDING_SENDERS)[:2]]
    for user_id in batch:
        PENDING_SENDERS.discard(user_id)
        try:
            entity = await client.get_entity(user_id)
        except FloodWaitError as exc:
            logger.warning("FloodWait while resolving senders: %ss", exc.seconds)
            return
        except Exception:
            continue
        username = getattr(entity, "username", None)
        name = display_name(entity)
        SENDER_CACHE[user_id] = (username, name)
        try:
            async with SessionLocal() as session:
                await session.execute(
                    update(Message)
                    .where(Message.user_id == user_id)
                    .values(username=username, sender_name=name[:255])
                )
                await session.commit()
        except Exception as exc:
            logger.warning("Sender rename failed: %s", exc)


async def resolve_loop(client):
    while True:
        await asyncio.sleep(15)
        try:
            await resolve_senders(client)
        except Exception as exc:
            logger.warning("Sender resolve pass failed: %s", exc)


async def gap_watchdog(client):
    """One cheap request per interval: which chats have something new?

    Symptom being fixed: updates only appeared after the same account opened
    the chat in another client. The session is simply not being pushed to, so
    waiting for events is not enough.

    `client.catch_up()` is NOT the answer here — it walks every dialog and
    issues one channel-difference request each, which at ~200 dialogs is a
    flood per pass. `get_dialogs(limit=...)` is a single request that returns
    the newest message id per chat; only the chats that moved are backfilled.
    """
    backoff = 1.0
    while True:
        # Her turda yeni aralik: 50-60 sn. Flood gelirse backoff carpani.
        await asyncio.sleep(min(random.uniform(DIALOG_POLL_MIN, DIALOG_POLL_MAX) * backoff,
                                DIALOG_POLL_BACKOFF_MAX))
        try:
            dialogs = await client.get_dialogs(limit=DIALOG_POLL_LIMIT)
        except FloodWaitError as exc:
            # Telegram bu hizi sevmedi: araligi iki katina cikar, 5 dk'da dur.
            backoff = min(backoff * 2, 5)
            logger.warning("FloodWait on dialog poll: %ss — backoff x%.0f",
                           exc.seconds, backoff)
            await asyncio.sleep(exc.seconds + 5)
            continue
        except Exception as exc:
            logger.warning("Dialog poll failed: %s", exc)
            continue

        latest = {}
        for dialog in dialogs:
            if dialog.is_user or dialog.message is None:
                continue
            latest[utils.get_peer_id(dialog.entity)] = dialog.message.id
        if not latest:
            continue

        try:
            async with SessionLocal() as session:
                rows = (await session.execute(
                    select(Message.chat_id, func.max(Message.message_id))
                    .where(Message.chat_id.in_(list(latest)))
                    .group_by(Message.chat_id)
                )).all()
        except Exception as exc:
            logger.warning("Dialog poll compare failed: %s", exc)
            continue

        stored = {chat_id: last_id for chat_id, last_id in rows}
        behind = [cid for cid, last_id in latest.items()
                  if last_id and last_id > stored.get(cid, 0)]
        if behind:
            logger.info("Dialog poll: %s chat(s) behind, fetching.", len(behind))
            await backfill_gap(client, only=set(behind))


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
            # No entity resolution here on purpose: msg.chat / msg.sender fall
            # back to a network get_entity() whenever the entity is not cached,
            # and that measured 5-9s per message. Slow handlers are also why the
            # server stopped pushing to this session. Titles come from the
            # get_dialogs cache, names from SENDER_CACHE, filled lazily.
            _t0 = _t1 = _t2 = time.perf_counter()
            chat_id = event.chat_id
            chat_title = CHAT_TITLE_CACHE.get(chat_id) or _chat_title_hint(msg, chat_id)

            content = msg.message
            timestamp = msg.date.astimezone(timezone.utc).replace(tzinfo=None)
            # Pipeline latency: Telegram's own send time vs the moment this
            # process handles it. Anything above ~2s means the update stream is
            # being drained late, not that the panel is slow.
            _lag = (datetime.now(timezone.utc).replace(tzinfo=None) - timestamp).total_seconds()
            if _lag > 2:
                logger.warning("Update lag %.1fs (%s)", _lag, chat_title[:32])
            user_id = event.sender_id
            username, sender_name = sender_identity(msg, user_id)

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
                # The chat row may exist from the startup sync; if it does not,
                # this update is a no-op and the next sync creates it.
                await session.execute(
                    update(Chat)
                    .where(Chat.chat_id == chat_id)
                    .values(
                        last_message_at=timestamp,
                        title=chat_title[:255] if chat_title else None,
                    )
                )
                await session.commit()
            _t3 = time.perf_counter()

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
            _t4 = time.perf_counter()

            _parts = {
                "chat": _t1 - _t0,
                "sender": _t2 - _t1,
                "db": _t3 - _t2,
                "broadcast": _t4 - _t3,
            }
            if sum(_parts.values()) > 0.8:
                logger.warning(
                    "Slow path %.1fs (chat %.2f, sender %.2f, db %.2f, post %.2f)",
                    sum(_parts.values()), _parts["chat"], _parts["sender"],
                    _parts["db"], _parts["broadcast"],
                )

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
    """One bounded dialog read that warms titles AND records the chat list.

    A full get_dialogs() on a large account is a burst of API traffic right
    after login — exactly the pattern that produces FloodStrike — so the read
    stays capped. The rows it returns are persisted to `chats`, which is the
    only way the panel can list a group that has not spoken yet.
    """
    try:
        dialogs = await client.get_dialogs(limit=CHAT_SYNC_LIMIT)
    except FloodWaitError as exc:
        logger.warning("FloodWait while warming dialogs: sleeping %ss", exc.seconds + 10)
        await asyncio.sleep(exc.seconds + 10)
        return
    except Exception as exc:
        logger.warning("Dialog warm-up skipped: %s", exc)
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    saved = 0
    try:
        async with SessionLocal() as session:
            existing = {
                row.chat_id: row
                for row in (await session.execute(select(Chat))).scalars()
            }
            for dialog in dialogs:
                entity = dialog.entity
                if dialog.is_user:
                    # Private chats stay out of the archive, same rule as the
                    # message handler.
                    continue
                # Marked id (-100… for channels/megagroups) so it matches
                # event.chat_id from the message handler exactly.
                chat_id = utils.get_peer_id(entity) if entity else dialog.id
                title = dialog.title or dialog.name or f"Chat {chat_id}"
                if dialog.is_channel and not dialog.is_group:
                    chat_type = "channel"
                elif dialog.is_group:
                    chat_type = "group"
                else:
                    chat_type = "chat"

                row = existing.get(chat_id)
                if row is None:
                    row = Chat(chat_id=chat_id)
                    session.add(row)
                    existing[chat_id] = row
                row.title = title[:255]
                row.username = getattr(entity, "username", None)
                row.chat_type = chat_type
                row.member_count = getattr(entity, "participants_count", None)
                last = getattr(dialog, "date", None)
                row.last_message_at = (
                    last.astimezone(timezone.utc).replace(tzinfo=None) if last else None
                )
                row.synced_at = now
                CHAT_TITLE_CACHE[chat_id] = title
                saved += 1
            await session.commit()
    except Exception as exc:
        logger.warning("Chat sync failed: %s", exc)
        return
    logger.info("Chat list synced: %s dialogs recorded.", saved)


BACKFILL_LIMIT = 200
DIALOG_POLL_MIN = int(os.getenv("DIALOG_POLL_MIN", "50"))  # saniye; rastgele
DIALOG_POLL_MAX = int(os.getenv("DIALOG_POLL_MAX", "60"))  # 9999 = poll kapali
DIALOG_POLL_BACKOFF_MAX = 300
DIALOG_POLL_LIMIT = 60   # en yeni 60 sohbet; arsivdeki 7 sohbetin hepsi burada


async def backfill_gap(client, only=None):
    """Fetch what arrived while the process was down.

    `catch_up=False` means Telegram will not replay a backlog into the event
    loop, and replaying one in parallel is exactly what looks like scraping.
    Instead each known chat is asked, in bounded batches, for messages newer
    than the last id already stored. The batch cap keeps a restart from turning
    into a scraping burst while still closing a long outage.
    """
    filled = 0
    try:
        async with SessionLocal() as session:
            chats = [
                (row.chat_id, row.title)
                for row in (await session.execute(select(Chat))).scalars()
            ]
    except Exception as exc:
        logger.warning("Backfill skipped (chat list unavailable): %s", exc)
        return

    for chat_id, title in chats:
        if only is not None and chat_id not in only:
            continue
        async with SessionLocal() as session:
            last = (await session.execute(
                select(func.max(Message.message_id)).where(Message.chat_id == chat_id)
            )).scalar()
        # Only chats we already archive: a chat with no history would otherwise
        # get a free 50-message pull on every restart.
        if not last:
            continue
        try:
            entity = await client.get_entity(chat_id)
        except FloodWaitError as exc:
            logger.warning("FloodWait before backfill: %ss", exc.seconds)
            break
        except Exception:
            continue

        chunk = []
        cursor = last
        try:
            while True:
                batch = []
                async for msg in client.iter_messages(
                    entity, min_id=cursor, limit=BACKFILL_LIMIT, reverse=True
                ):
                    cursor = max(cursor, msg.id)
                    if not msg.message:
                        continue
                    sender = msg.sender
                    batch.append(Message(
                        message_id=msg.id,
                        chat_id=chat_id,
                        chat_title=(title or f"Chat {chat_id}")[:255],
                        user_id=msg.sender_id,
                        username=getattr(sender, "username", None),
                        sender_name=display_name(sender)[:255],
                        content=msg.message,
                        timestamp=msg.date.astimezone(timezone.utc).replace(tzinfo=None),
                    ))
                chunk.extend(batch)
                # Unlimited depth on purpose: the archive must not have holes,
                # so keep paging until the chat is caught up. The short pause is
                # the only throttle, so a long outage does not look like a burst.
                if len(batch) < BACKFILL_LIMIT:
                    break
                await asyncio.sleep(0.5)
        except FloodWaitError as exc:
            logger.warning("FloodWait during backfill: %ss", exc.seconds)
            break
        except Exception as exc:
            logger.warning("Backfill failed for %s: %s", chat_id, exc)
            continue

        if chunk:
            async with SessionLocal() as session:
                session.add_all(chunk)
                await session.commit()
            filled += len(chunk)

    if filled:
        logger.info("Backfilled %s message(s) missed while offline.", filled)
    else:
        logger.info("Backfill: no gap to close.")


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
    await backfill_gap(client)

    # Background loops: names resolve off the hot path, and a five-minute
    # per-chat check recovers anything Telegram stopped pushing.
    asyncio.create_task(resolve_loop(client))
    asyncio.create_task(gap_watchdog(client))

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

