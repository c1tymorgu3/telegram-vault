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
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import and_, func, not_, select, update
from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient, events, utils
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

import config
from config import API_HASH, API_ID, PHONE_NUMBER
from database import Chat, Message, SessionLocal, init_db

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

# --- Silme / poll ayarlari -------------------------------------------------
BACKFILL_LIMIT = 200

# Sohbet listesi poll'u: her turda [MIN, MAX] araliginda rastgele bir bekleme.
# Flood gelirse bekleme backoff carpaniyla uzar; backoff'lu bekleme
# DIALOG_POLL_BACKOFF_MAX'i (300 sn) asmaz (taban aralik ise dogrudan
# [DIALOG_POLL_MIN, DIALOG_POLL_MAX]'tir, kirpilmaz).
# DIALOG_POLL_MAX >= DIALOG_POLL_DISABLED ise poll TAMAMEN kapalidir: deger
# kirpilmaz, gap_watchdog hic uyanmaz.
DIALOG_POLL_MIN = int(os.getenv("DIALOG_POLL_MIN", "50"))  # saniye
DIALOG_POLL_MAX = int(os.getenv("DIALOG_POLL_MAX", "60"))  # 9999+ = poll kapali
DIALOG_POLL_BACKOFF_MAX = 300   # backoff tavanini asan aralik = 300 sn
DIALOG_POLL_DISABLED = 9999     # "poll kapali" esigi
# Poll yalnizca en yeni N sohbete bakar: get_dialogs(limit=N) tek bir istek,
# tam sayfalama degil. Arsivdeki 7 sohbetin hepsi bu pencerede kaliyor; N'den
# eski bir sohbet sessizlesirse poll onu gormez (config'ten okunmuyor, bilincli).
DIALOG_POLL_LIMIT = 60

# Toplu silmede tek UPDATE'te islenen ID sayisi: liste parca parca islenir,
# binlerce ID ayni anda bellekte tutulmaz.
DELETE_BATCH_SIZE = 100
# Bir silme olayinda panele canli bildirilen en fazla ID. Ustunu panelin 30
# sn'lik toplam yenilemesi tamamlar; binlerce HTTP cagrisi yapilmaz.
DELETE_BROADCAST_LIMIT = 200

# Ertelenmis silmeler (~24 saat / ~7 gun) Telegram'in kendi servis mesaji
# temizligidir, insan kanit silme degildir. (alt, ust) saniye araliklari.
DELAYED_DELETE_WINDOWS = ((86280, 86520), (604680, 604920))


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
    if dialog_poll_disabled(DIALOG_POLL_MAX):
        # Belgede yazan "9999 = poll kapali" artik gercekten kapali: eskiden
        # 300 sn'ye kirpiliyor ve poll 5 dakikada bir calismaya devam ediyordu.
        logger.info("Dialog poll disabled (DIALOG_POLL_MAX=%s).", DIALOG_POLL_MAX)
        return

    backoff = 1.0
    while True:
        # Her turda yeni aralik: MIN-MAX sn. Flood gelirse backoff carpani.
        await asyncio.sleep(poll_interval(backoff=backoff))
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
# Karar yardimcilari (agdan ve DB'den bagimsiz; tests/test_bot.py bunlari
# dogrudan cagirir, bu yuzden imzalarini degistirirken testi de guncelle)
# --------------------------------------------------------------------------
def archivable_event(is_private, content):
    """Ozel sohbet ve metinsiz mesaj arsive girmez (NewMessage ile ayni kural)."""
    return (not is_private) and bool(content)


def chunked(items, size=DELETE_BATCH_SIZE):
    """ID listesini sabit boyutlu parcalara boler.

    Parcalar tek tek uretilir: 50 bin ID'lik bir ADMIN_CLEAR olayinda liste
    bastan sona bellekte kopyalanmaz.
    """
    size = max(1, int(size))
    for start in range(0, len(items), size):
        yield items[start:start + size]


def deletion_targets(chat_id, ids):
    """Islenebilir silinen ID'ler; chat_id yoksa bos liste.

    chat_id olmadan esleme yapmak, mesaj numarasini TUM sohbetlerde arayip
    ayni numaraya sahip baska bir sohbetin satirini "silindi" isaretliyordu.
    Bu yuzden chat_id yoksa hicbir ID islenmez, sadece uyari yazilir.
    """
    if not chat_id:
        return []
    return [int(mid) for mid in ids if mid]


def is_delayed_cleanup(age_seconds):
    """Ertelenmis Telegram temizligi mi (insan silmesi degil)?"""
    return any(low < age_seconds < high for low, high in DELAYED_DELETE_WINDOWS)


def delayed_cleanup_bounds(now):
    """Ayni pencerelerin SQL karsiligi: timestamp (now-ust, now-alt) icinde.

    `is_delayed_cleanup` satir satir cagrilamadigi icin (toplu UPDATE) ayni
    kural burada zaman damgasi siniri olarak uretilir.
    """
    return [(now - timedelta(seconds=high), now - timedelta(seconds=low))
            for low, high in DELAYED_DELETE_WINDOWS]


def dialog_poll_disabled(poll_max):
    """9999 ve uzeri deger "poll kapali" demektir (300 sn'ye kirpilmaz)."""
    return int(poll_max) >= DIALOG_POLL_DISABLED


def poll_interval(low=DIALOG_POLL_MIN, high=DIALOG_POLL_MAX, backoff=1.0):
    """Bir sonraki poll'a kadar beklenecek sure.

    Ters verilmis MIN/MAX cokmesin diye aralik normalize edilir; sonuc
    DIALOG_POLL_BACKOFF_MAX'i asmaz.
    """
    if low > high:
        low, high = high, low
    return min(random.uniform(low, high) * backoff, DIALOG_POLL_BACKOFF_MAX)


async def insert_message(session, **fields):
    """Mesaji yaz; (chat_id, message_id) zaten varsa sessizce gec.

    Backfill ile canli dinleyici ayni mesaji yazabiliyor ve DB'de unique
    kisit var. Cakisma SAVEPOINT icinde yakalaniyor: disaridaki islem
    (chat guncellemesi) ve oturum kullanilabilir kalir, Whole-commit
    patlamaz. Donus: True yazildi, False cakisti.
    """
    try:
        async with session.begin_nested():
            session.add(Message(**fields))
            await session.flush()
    except IntegrityError:
        logger.debug(
            "Duplicate archive row chat=%s message=%s ignored.",
            fields.get("chat_id"), fields.get("message_id"),
        )
        return False
    return True


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def register(client, http):
    @client.on(events.NewMessage())
    async def handler(event):
        # Private chats stay out of the archive on purpose.
        msg = event.message
        if not archivable_event(event.is_private, msg.message if msg else None):
            return

        if not remember((event.chat_id, msg.id)):
            return

        try:
            # No entity resolution here on purpose: msg.chat / msg.sender fall
            # back to a network get_entity() whenever the entity is not cached,
            # and that measured 5-9s per message. Slow handlers are also why the
            # server stopped pushing to this session. Titles come from the
            # get_dialogs cache, names from SENDER_CACHE, filled lazily.
            _t0 = time.perf_counter()
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
            # "chat" asamasi burada biter: her asama kendi perf_counter()
            # cagrisini alir; paylasilan tek deger "chat" ve "sender"
            # surelerini her zaman 0 gosteriyordu.
            _t1 = time.perf_counter()

            user_id = event.sender_id
            username, sender_name = sender_identity(msg, user_id)
            _t2 = time.perf_counter()

            async with SessionLocal() as session:
                stored = await insert_message(
                    session,
                    message_id=msg.id,
                    chat_id=chat_id,
                    chat_title=chat_title[:255] if chat_title else None,
                    user_id=user_id,
                    username=username,
                    sender_name=sender_name[:255],
                    content=content,
                    timestamp=timestamp,
                )
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

            if not stored:
                # Backfill bu mesaji zaten yazdi (unique cakisma): panele
                # ikinci kez canli bildirim gitmesin.
                return

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
            except httpx.HTTPError as exc:
                # Panel kapali olabilir; mesaj DB'de zaten duruyor.
                logger.debug("Broadcast failed: %s", exc)
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

    @client.on(events.MessageEdited())
    async def edit_handler(event):
        """Duzenlenen mesajin yeni icerigini arsive yazar.

        Satir ayni kalir: content yeni metinle degisir, edited_at naive UTC
        "simdi" olur. Eski icerik log'a YAZILMAZ (arsivde de tutulmaz).
        Metni bosaltan duzenleme arsiv satirini silmez, sadece atlanir.
        """
        msg = event.message
        if not archivable_event(event.is_private, msg.message if msg else None):
            if msg is not None and not event.is_private:
                logger.debug("Edited message %s in chat %s has no text, kept as-is.",
                             msg.id, event.chat_id)
            return

        chat_id = event.chat_id
        if not chat_id:
            logger.warning("Edited event without chat_id, id=%s skipped.", msg.id)
            return

        edited_at = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    update(Message)
                    .where(Message.chat_id == chat_id, Message.message_id == msg.id)
                    .values(content=msg.message, edited_at=edited_at)
                )
                await session.commit()

            rows = result.rowcount or 0
            if rows:
                logger.info("Edited message %s in chat %s: content replaced (%s row).",
                            msg.id, chat_id, rows)
            else:
                # Bot kapaliyken duzenlenmis olabilir; satir yoksa yeni kayit
                # acmak gecmisi carpitir, sadece debug'a dusuyoruz.
                logger.debug("Edited message %s in chat %s not in archive.", msg.id, chat_id)
        except Exception as exc:
            logger.error("Edit update error: %s", exc)

    @client.on(events.MessageDeleted())
    async def delete_handler(event):
        ids = list(event.deleted_ids or [])
        if not ids:
            return

        chat_id = event.chat_id
        targets = deletion_targets(chat_id, ids)
        if not targets:
            # Eskiden chat_id yokken ID'ler TUM tabloda araniyor ve ayni
            # numaraya sahip baska bir sohbetin satiri silinmis isaretleniyordu.
            logger.warning("Deletion event without chat_id: %s id(s) skipped.", len(ids))
            return

        # Toplu silmeler (admin purge / kanal temizligi) artik atlanmiyor:
        # ID'ler DELETE_BATCH_SIZE'lik parcalar halinde tek UPDATE ile isleniyor.
        if len(targets) > DELETE_BATCH_SIZE:
            logger.info("Bulk deletion: %s id(s) in chat %s, %s-sized batches.",
                        len(targets), chat_id, DELETE_BATCH_SIZE)

        bounds = delayed_cleanup_bounds(datetime.now(timezone.utc).replace(tzinfo=None))
        # Ertelenmis temizlik penceresine (~24h / ~7d) dusen satirlar kurali:
        # eski kod bunu satir satir uyguluyordu, toplu UPDATE'te ayni kural
        # zaman damgasi kosulu olarak sorguya giriyor.
        not_delayed = [
            not_(and_(Message.timestamp > low, Message.timestamp < high))
            for low, high in bounds
        ]
        marked_ids = []
        try:
            for chunk in chunked(targets):
                async with SessionLocal() as session:
                    hits = (await session.execute(
                        select(Message.message_id).where(
                            Message.chat_id == chat_id,
                            Message.message_id.in_(chunk),
                            Message.is_deleted.is_(False),
                            *not_delayed,
                        )
                    )).scalars().all()
                    if not hits:
                        continue
                    await session.execute(
                        update(Message)
                        .where(
                            Message.chat_id == chat_id,
                            Message.message_id.in_(hits),
                            Message.is_deleted.is_(False),
                        )
                        .values(is_deleted=True)
                    )
                    await session.commit()
                marked_ids.extend(hits)

            for msg_id in marked_ids[:DELETE_BROADCAST_LIMIT]:
                try:
                    await http.post(config.BROADCAST_URL, json={
                        "type": "DELETED",
                        "message_id": msg_id,
                        "chat_id": chat_id,
                    }, timeout=2.0)
                except httpx.HTTPError as exc:
                    # Panel kapaliysa silme kaydi DB'de zaten duruyor.
                    logger.debug("Delete broadcast failed: %s", exc)

            if marked_ids:
                # Panelin "silinenler" sayaci bu satirla dogrulanir: buyuk bir
                # temizlikte kac ID'nin gercekten isaretlendigi log'dan gorunur.
                logger.info("Marked %s message(s) deleted in chat %s%s",
                            len(marked_ids), chat_id,
                            "" if len(marked_ids) <= DELETE_BROADCAST_LIMIT
                            else " (%s not broadcast live)"
                            % (len(marked_ids) - DELETE_BROADCAST_LIMIT))
            else:
                logger.debug("Deletion event in chat %s matched no archived row.", chat_id)
        except Exception as exc:
            logger.error("Deletion log error: %s", exc)

    return handler, edit_handler, delete_handler


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
                    batch.append({
                        "message_id": msg.id,
                        "chat_id": chat_id,
                        "chat_title": (title or f"Chat {chat_id}")[:255],
                        "user_id": msg.sender_id,
                        "username": getattr(sender, "username", None),
                        "sender_name": display_name(sender)[:255],
                        "content": msg.message,
                        "timestamp": msg.date.astimezone(timezone.utc).replace(tzinfo=None),
                    })
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
            written = 0
            async with SessionLocal() as session:
                # Satir satir: canli dinleyici ayni mesaji bu arada yazmissa
                # unique kisit cakismasi tek satiri atlar, chunk'i patlatmaz.
                for row in chunk:
                    if await insert_message(session, **row):
                        written += 1
                await session.commit()
            if written < len(chunk):
                logger.debug("gap: %s/%s rows already archived (chat=%s)",
                             len(chunk) - written, len(chunk), chat_id)
            filled += written

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

