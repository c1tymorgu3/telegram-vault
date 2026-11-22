"""bot.py icindeki saf karar yardimcilarinin testleri.

Amac: Telegram'a ve Postgres'e baglanmadan silme/duzenleme mantigini
dogrulamak. Handler'lar aga ve DB'ye dokundugu icin burada yalnizca
onlarin kullandigi saf fonksiyonlar test edilir:

    archivable_event, chunked, deletion_targets,
    is_delayed_cleanup, delayed_cleanup_bounds,
    dialog_poll_disabled, poll_interval, insert_message

insert_message ve edited_at yolu gecici bir SQLite dosyasinda calistirilir;
canli DB'ye ve Telegram'a dokunulmaz.
"""

import os
import sys
from datetime import datetime, timedelta

# tests/ bir paket degil; bot.py'yi repo kokunden import edebilmek icin.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # yol eklemesinden sonra geldigi icin sira onemli


# --- chunked: toplu silme listesi parcalama -------------------------------
def test_chunked_splits_large_deletion_into_batches():
    ids = list(range(1, 251))
    chunks = list(bot.chunked(ids))
    assert [len(c) for c in chunks] == [100, 100, 50]
    # Parcalar sirali ve kayipsiz: hicbir ID dusmuyor, tekrarlamiyor.
    assert [i for c in chunks for i in c] == ids


def test_chunked_empty_and_custom_size():
    assert list(bot.chunked([])) == []
    assert [len(c) for c in bot.chunked(list(range(7)), size=3)] == [3, 3, 1]


def test_chunked_never_yields_empty_chunks():
    # size<=0 bile sonsuz donguye girmemeli (max(1, size) korumasi).
    assert [len(c) for c in bot.chunked([1, 2], size=0)] == [1, 1]


# --- deletion_targets: chat_id'siz olay atlanir ---------------------------
def test_deletion_without_chat_id_is_skipped():
    # chat_id yoksa hicbir ID islenmez: tum tabloda arama yapilmaz, yanlis
    # sohbetin ayni numarali satiri silinmis isaretlenmez.
    assert bot.deletion_targets(None, [1, 2, 3]) == []
    assert bot.deletion_targets(0, [1, 2, 3]) == []


def test_deletion_targets_keeps_ids_of_known_chat():
    assert bot.deletion_targets(-1001234567890, [5, 6]) == [5, 6]
    # Bos/None ID'ler (Telethon bazen 0 dondurur) elenir.
    assert bot.deletion_targets(-1001, [0, 7, None]) == [7]


# --- ertelenmis silme pencereleri -----------------------------------------
def test_is_delayed_cleanup_windows():
    assert bot.is_delayed_cleanup(86399) is True        # ~24 saat
    assert bot.is_delayed_cleanup(604800) is True       # ~7 gun
    assert bot.is_delayed_cleanup(30) is False          # taze silme
    assert bot.is_delayed_cleanup(86400 + 5000) is False


def test_delayed_cleanup_bounds_match_row_by_row_rule():
    """SQL kosulu, satir satir uygulanan eski kuralla ayni sonucu vermeli."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    bounds = bot.delayed_cleanup_bounds(now)
    assert len(bounds) == len(bot.DELAYED_DELETE_WINDOWS)

    for low, high in bounds:
        mid = low + (high - low) / 2
        assert bot.is_delayed_cleanup((now - mid).total_seconds()) is True
        assert low < mid < high

    # Pencere disindaki zaman damgalari hicbir araliga dusmez.
    outside = now - timedelta(seconds=3600)
    assert not any(low < outside < high for low, high in bounds)


# --- DIALOG_POLL_MAX: 9999 gercekten "kapali" mi --------------------------
def test_dialog_poll_disabled_threshold():
    assert bot.dialog_poll_disabled(bot.DIALOG_POLL_DISABLED) is True
    assert bot.dialog_poll_disabled(9999) is True
    assert bot.dialog_poll_disabled(60) is False


def test_poll_interval_is_bounded_by_backoff_max():
    for _ in range(50):
        assert bot.poll_interval(50, 60) <= bot.DIALOG_POLL_BACKOFF_MAX
    # Yuksek backoff carpani tavani asmaz.
    assert bot.poll_interval(50, 60, backoff=100.0) == bot.DIALOG_POLL_BACKOFF_MAX


def test_poll_interval_survives_reversed_range():
    # MIN > MAX yanlis ayarlanirsa uniform() cokmesin.
    value = bot.poll_interval(60, 50)
    assert 50 <= value <= 60


# --- ortak arsiv filtresi (NewMessage + MessageEdited) --------------------
def test_archivable_event_filter():
    assert bot.archivable_event(False, "merhaba") is True
    assert bot.archivable_event(True, "ozel sohbet") is False   # DM arsivlenmez
    assert bot.archivable_event(False, "") is False             # metinsiz
    assert bot.archivable_event(False, None) is False


# --- insert_message: (chat_id, message_id) cakismasi ----------------------
def test_insert_message_skips_duplicate_and_keeps_session_usable(tmp_path):
    """Backfill ayni mesaji once yazmissa canli insert patlamamali.

    Unique kisit ihlali SAVEPOINT icinde yutulur: satir sayisi artmaz,
    ayni oturumdaki diger islemler (chat guncellemesi) commit edilebilir.
    """
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from database import Base, Message

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}"

    async def scenario():
        engine = create_async_engine(db_url)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        stamp = datetime(2026, 1, 1, 12, 0, 0)
        row = {
            "message_id": 7, "chat_id": -100123, "chat_title": "Test",
            "user_id": 5, "username": "u", "sender_name": "U",
            "content": "ilk", "timestamp": stamp,
        }
        async with session_factory() as session:
            first = await bot.insert_message(session, **row)
            second = await bot.insert_message(session, **{**row, "content": "ikinci"})
            third = await bot.insert_message(session, **{**row, "message_id": 8})
            await session.commit()
            count = (await session.execute(select(Message))).scalars().all()
        await engine.dispose()
        return first, second, third, count

    first, second, third, count = asyncio.run(scenario())
    assert first is True
    assert second is False
    assert third is True                # cakismadan sonra yazmaya devam eder
    assert len(count) == 2
    assert count[0].content == "ilk"     # cakisan yazim mevcut satiri ezmez


def test_edit_update_sets_edited_at(tmp_path):
    """MessageEdited'in yazdigi alanlar: content degisir, edited_at dolar."""
    import asyncio

    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from database import Base, Message

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edit.db'}"

    async def scenario():
        engine = create_async_engine(db_url)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        stamp = datetime(2026, 1, 1, 12, 0, 0)
        async with session_factory() as session:
            session.add(Message(message_id=1, chat_id=9, content="eski",
                                timestamp=stamp))
            await session.commit()
            edited_at = datetime(2026, 1, 2, 3, 4, 5)
            result = await session.execute(
                update(Message)
                .where(Message.chat_id == 9, Message.message_id == 1)
                .values(content="yeni", edited_at=edited_at)
            )
            await session.commit()
            rows = result.rowcount
            stored = (await session.execute(select(Message))).scalars().one()
        await engine.dispose()
        return rows, stored

    rows, stored = asyncio.run(scenario())
    assert rows == 1
    assert stored.content == "yeni"
    assert stored.edited_at == datetime(2026, 1, 2, 3, 4, 5)

