"""digest cekirdegi testleri (DB/LLM gerektirmez).

Kapsam: dilim tarihi hesabi, TRT gun sinirlari, kanit kimligi toplama,
JSON farkindalikli kirpma ve sohbet bazli gruplama.
"""

import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

# Repo kokunu path'e ekle: testler `pytest tests/` ile de calissin.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import digest
from timeutil import day_bounds_trt, to_trt, trt_date_window


def _seg(title: str, start: datetime, payload: dict):
    return SimpleNamespace(
        chat_title=title,
        window_start=start,
        window_end=start + timedelta(hours=2),
        payload=payload,
    )


# --- 1) Dilim tarihi -------------------------------------------------------

def test_bucket_21_utc_sonrasi_ayni_trt_gunu():
    """21:00 UTC'yi asan mesajlar ayni TRT gunune ve dogru dilime duser."""
    stamps = [
        datetime(2026, 11, 21, 21, 5),
        datetime(2026, 11, 21, 21, 40),
        datetime(2026, 11, 21, 22, 10),
        datetime(2026, 11, 21, 22, 55),
        datetime(2026, 11, 21, 23, 30),
    ]
    buckets = [digest.bucket_start_for(ts) for ts in stamps]

    # Dilim, mesajin KENDI saatinden gelir -> 20:00 ve 22:00 UTC dilimleri.
    assert set(buckets) == {datetime(2026, 11, 21, 20), datetime(2026, 11, 21, 22)}

    # Hicbiri bir onceki gune damgalanmaz (eski hata: 20 Kasim).
    for ts, bucket in zip(stamps, buckets, strict=True):
        assert bucket.date() == ts.date()
        assert bucket <= ts < bucket + timedelta(hours=2)

    # Hepsi ayni TRT gunune (22 Kasim) duser ve ayni TRT gun penceresine girer.
    assert len({to_trt(ts).date() for ts in stamps}) == 1
    day_start, day_end = day_bounds_trt(stamps[0])
    assert (day_start, day_end) == (datetime(2026, 11, 21, 21), datetime(2026, 11, 22, 21))
    assert all(day_start <= ts < day_end for ts in stamps)
    # Ilk dilim TRT gun sinirini keser; gunluk brif pencere kesisimiyle bulur.
    assert buckets[0] < day_start <= buckets[0] + timedelta(hours=2)


def test_bucket_gece_yarisini_asinca_gun_degismez():
    """21:59 UTC ile 22:01 UTC arasi dilim siniridir, gun siniri degil."""
    before = digest.bucket_start_for(datetime(2026, 11, 21, 21, 59))
    after = digest.bucket_start_for(datetime(2026, 11, 21, 22, 1))
    assert before == datetime(2026, 11, 21, 20)
    assert after == datetime(2026, 11, 21, 22)
    assert before.date() == after.date()  # ayni UTC gunu, ayni dilim seti


def test_trt_gun_sinirlari():
    """TRT gunu UTC'de bir onceki gunun 21:00'inde baslar."""
    start, end = day_bounds_trt(datetime(2026, 11, 21, 22, 0))
    assert start == datetime(2026, 11, 21, 21)
    assert end == datetime(2026, 11, 22, 21)

    # Takvim tarihi verildiginde de ayni sonuc.
    assert trt_date_window(datetime(2026, 11, 22)) == (datetime(2026, 11, 21, 21),
                                                      datetime(2026, 11, 22, 21))


# --- 2) Kanit kimlikleri ---------------------------------------------------

def test_evidence_ids_profil_alanlarini_da_tarar():
    payload = {
        "sectors": [{"name": "kripto", "evidence": [1, "2"]}],
        "demands": [{"what": "banka", "evidence": [3]}],
        "self_disclosed": [
            {"claim": "Istanbul", "category": "sehir", "evidence": [4, 5]},
            {"claim": "gece calisir", "category": "rutin", "evidence": [6]},
        ],
        "hearsay": [{"claim": "ortak kacmis", "evidence": [7]}],
        "notes": ["kanitsiz"],  # kanit alani degil
    }
    ids = digest.evidence_ids(payload)
    assert set(ids) == {1, 2, 3, 4, 5, 6, 7}
    assert all(isinstance(i, int) for i in ids)


def test_evidence_ids_bozuk_girdileri_atlar():
    payload = {
        "self_disclosed": [{"claim": "x", "evidence": ["abc", None, 9]}],
        "hearsay": [{"claim": "y"}],          # evidence yok
        "sectors": [None, "bozuk"],           # dict degil
    }
    assert digest.evidence_ids(payload) == [9]


def test_validate_evidence_imzasi_sohbet_alir():
    """Dogrulama sohbet + pencere ile sinirlanabilmeli (geri uyumlu imza)."""
    params = inspect.signature(digest.validate_evidence).parameters
    assert list(params)[:5] == ["db", "payload", "chat_id", "window_start", "window_end"]
    assert params["chat_id"].default is None


# --- 3) Gruplama -----------------------------------------------------------

def test_collect_chat_id_ile_gruplar():
    """Ayni baslikli iki sohbet tek kovaya dusmemeli."""
    rows = [
        SimpleNamespace(chat_id=1, content="Merhaba", chat_title="Pazar"),
        SimpleNamespace(chat_id=2, content="Selam", chat_title="Pazar"),
        SimpleNamespace(chat_id=1, content="merhaba ", chat_title="Pazar"),  # dupe
    ]
    grouped = digest._collect(rows)
    assert set(grouped) == {1, 2}
    assert len(grouped[1]) == 1


# --- 4) Kirpma -------------------------------------------------------------

def test_pack_segments_butce_altinda_kirpmaz():
    segs = [_seg("A", datetime(2026, 11, 21, 20), {"sectors": [{"name": "x"}]}),
            _seg("B", datetime(2026, 11, 21, 22), {"sectors": [{"name": "y"}]})]
    text, report = digest.pack_segments(segs, 10_000)
    assert report == {}
    assert "A" in text and "B" in text and text.count("\n\n") == 1


def test_pack_segments_sondan_butun_dilim_atar():
    payload = {"notes": ["n" * 400]}
    base = datetime(2026, 11, 21, 18)
    segs = [_seg(f"S{i}", base + timedelta(hours=2 * i), payload) for i in range(3)]
    full, _ = digest.pack_segments(segs, 10_000)
    budget = len(full) - 1  # tek karakter yetmiyor

    text, report = digest.pack_segments(segs, budget)
    assert len(text) <= budget
    assert report["segments_dropped"] == 1
    assert report["chars_dropped"] > 0
    assert "S2" not in text  # son dilim dustu, ortadan kesilmedi
    assert "S0" in text and "S1" in text


# --- 5) Dilim uretimi (gecici DB, LLM taklit) ------------------------------

async def _mk_session(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from database import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _echo_model(calls: list[str]):
    """Prompt'u payload'a geri yazan sahte model: hangi mesaj gitti gorulur."""
    async def fake_model(system, user, max_tokens=6000):
        calls.append(user)
        return ({"notes": [user[:1200]]},
                {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.25})

    return fake_model


@pytest.mark.asyncio
async def test_summarize_segment_ayni_sohbette_dupe_tekillestirir(tmp_path, monkeypatch):
    """Birebir ayni metin bir kez gonderilir; prompt ve msg_count ayni sayiyi soyler."""
    from database import Message

    engine, session_factory = await _mk_session(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(digest, "_call_model", _echo_model(calls))

    async with session_factory() as db:
        for mid, content, minute in [(700, "ayni metin", 5), (701, "  Ayni Metin ", 6),
                                     (702, "farkli", 7)]:
            db.add(Message(message_id=mid, chat_id=1, user_id=1, chat_title="Pazar",
                           content=content, timestamp=datetime(2026, 11, 21, 21, minute),
                           is_deleted=False))
        await db.commit()

        made = await digest.summarize_segment(db, datetime(2026, 11, 21, 20),
                                              datetime(2026, 11, 21, 22))
        assert len(calls) == 1
        assert made[0].msg_count == 2
        assert "Mesaj sayısı: 2" in calls[0]
        assert calls[0].count("[70") == 2  # dupe satiri prompt'a iki kez girmedi

    await engine.dispose()


def test_digest_modulunde_calisma_notu_yok():
    """Kaynak dosyada yarım kalmış iş izi (TODO/FIXME) bırakılmaz."""
    import re

    source = Path(digest.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\bTODO\b|\bFIXME\b", source)


@pytest.mark.asyncio
async def test_summarize_segment_kirpilmis_dilim_tam_dilimde_yenilenir(tmp_path, monkeypatch):
    """Kirpilmis dilim, komsu saat isteginde onbellekten DONMEZ.

    Eski hata: 20:00 dilimi 21:30'a kadar ozetlenmisti; 21:00-22:00 istegi
    msg_count karsilastirmasi yuzunden o eski (20:00-21:30) ozeti donduruyor,
    yani bir sonraki TRT gununun brifi onceki gunun mesajlarini aliyordu.
    """
    from sqlalchemy import select

    from database import Digest, Message

    engine, session_factory = await _mk_session(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(digest, "_call_model", _echo_model(calls))

    async with session_factory() as db:
        for mid, ts in [(700, datetime(2026, 11, 21, 20, 30)),
                        (701, datetime(2026, 11, 21, 20, 40)),
                        (702, datetime(2026, 11, 21, 21, 29))]:
            db.add(Message(message_id=mid, chat_id=1, user_id=1, chat_title="Pazar",
                           content=f"m{mid}", timestamp=ts, is_deleted=False))
        await db.commit()

        first = await digest.summarize_segment(db, datetime(2026, 11, 21, 20),
                                               datetime(2026, 11, 21, 21, 30))
        assert first[0].msg_count == 3
        assert first[0].payload["_covered"] == ["2026-11-21T20:00:00",
                                                "2026-11-21T21:30:00"]

        # Komsu saat: eski ozet bu araligi kapsamiyor -> yeniden uretilmeli.
        second = await digest.summarize_segment(db, datetime(2026, 11, 21, 21),
                                                datetime(2026, 11, 21, 22))
        assert len(calls) == 2
        assert second[0].msg_count == 1
        assert second[0].payload["_covered"] == ["2026-11-21T21:00:00",
                                                 "2026-11-21T22:00:00"]

        # Ayni istek tekrar: artik onbellekten doner.
        again = await digest.summarize_segment(db, datetime(2026, 11, 21, 21),
                                              datetime(2026, 11, 21, 22))
        assert len(calls) == 2
        assert again[0].id == second[0].id
        assert len((await db.execute(select(Digest))).scalars().all()) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_summarize_day_kirpilmis_dilim_komsu_gune_sizmaz(tmp_path, monkeypatch):
    """Yarim dilim yalnizca kendi gununun brifine girer, onceki gune sizmaz."""
    from database import Digest, Message

    engine, session_factory = await _mk_session(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(digest, "_call_model", _echo_model(calls))

    async with session_factory() as db:
        # Sohbet 1: TRT 22 Kasim'in ilk saati (21:05 UTC). Sohbet 2: onceki gun.
        db.add(Message(message_id=700, chat_id=1, user_id=1, chat_title="Bir",
                       content="yeni gun", timestamp=datetime(2026, 11, 21, 21, 5),
                       is_deleted=False))
        db.add(Message(message_id=800, chat_id=2, user_id=2, chat_title="Iki",
                       content="eski gun", timestamp=datetime(2026, 11, 21, 20, 30),
                       is_deleted=False))
        # Panel "son 2 saat" istegiyle uretilmis yarim dilim: 20:00 damgali ama
        # yalnizca 21:00-21:30 arasini ozetliyor.
        db.add(Digest(kind="segment", chat_id=1, chat_title="Bir",
                      window_start=datetime(2026, 11, 21, 20),
                      window_end=datetime(2026, 11, 21, 22), msg_count=1,
                      prompt_v=digest.PROMPT_V,
                      payload={"sectors": [{"name": "YARIM"}],
                               "_covered": ["2026-11-21T21:00:00", "2026-11-21T21:30:00"]}))
        await db.commit()

        # 21 Kasim TRT gunu: yarim dilim bu gune ait DEGIL.
        prev = await digest.summarize_day(db, window=(datetime(2026, 11, 20, 21),
                                                     datetime(2026, 11, 21, 21)))
        assert "YARIM" not in calls[-1]
        assert "eski gun" in calls[-1]
        assert prev.msg_count == 1

        # 22 Kasim TRT gunu: yarim dilim bu gune ait.
        today = await digest.summarize_day(db, window=(datetime(2026, 11, 21, 21),
                                                      datetime(2026, 11, 22, 21)))
        assert "YARIM" in calls[-1]
        assert today.msg_count == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_summarize_day_kirpma_sayilari_dogru_raporlar(tmp_path, monkeypatch):
    """Kirpma sonrasi prompt ve msg_count modele gercekten giden dilimleri sayar."""
    from database import Message

    engine, session_factory = await _mk_session(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(digest, "_call_model", _echo_model(calls))
    monkeypatch.setattr(digest, "MAX_CHARS_PER_CHUNK", 1000)

    async with session_factory() as db:
        stamps = [datetime(2026, 11, 21, 21, 5), datetime(2026, 11, 21, 23, 5),
                  datetime(2026, 11, 22, 1, 5), datetime(2026, 11, 22, 3, 5),
                  datetime(2026, 11, 22, 5, 5)]
        for i, ts in enumerate(stamps):
            db.add(Message(message_id=700 + i, chat_id=1, user_id=1, chat_title="Pazar",
                           content=f"mesaj {i} " + "x" * 400,
                           timestamp=ts, is_deleted=False))
        await db.commit()
        await digest.summarize_segment(db, datetime(2026, 11, 21, 21),
                                       datetime(2026, 11, 22, 7))

        day = await digest.summarize_day(db, window=(datetime(2026, 11, 21, 21),
                                                    datetime(2026, 11, 22, 21)))
        trunc = day.payload["_truncated"]
        assert trunc["segments_dropped"] >= 1
        assert trunc["segments_used"] == 5 - trunc["segments_dropped"]
        # Prompt ve msg_count yalnizca modele giden dilimleri anlatir.
        assert f"{trunc['segments_used']} dilim" in calls[-1]
        assert day.msg_count == trunc["segments_used"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_summarize_segment_fresh_listesi_maliyeti_ayirir(tmp_path, monkeypatch):
    """Onbellekten donen satirlar fresh'e yazilmaz; yenilenen satir yazilir."""
    from database import Message

    engine, session_factory = await _mk_session(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(digest, "_call_model", _echo_model(calls))

    async with session_factory() as db:
        db.add(Message(message_id=700, chat_id=1, user_id=1, chat_title="Pazar",
                       content="m1", timestamp=datetime(2026, 11, 21, 21, 5),
                       is_deleted=False))
        await db.commit()

        fresh: list = []
        await digest.summarize_segment(db, datetime(2026, 11, 21, 20),
                                       datetime(2026, 11, 21, 22), fresh=fresh)
        assert len(fresh) == 1

        fresh2: list = []
        await digest.summarize_segment(db, datetime(2026, 11, 21, 20),
                                       datetime(2026, 11, 21, 22), fresh=fresh2)
        assert fresh2 == []  # onbellek: para harcanmadi

        fresh3: list = []
        await digest.summarize_segment(db, datetime(2026, 11, 21, 20),
                                       datetime(2026, 11, 21, 22), force=True, fresh=fresh3)
        assert len(fresh3) == 1  # yerinde guncellendi ama LLM'e gitti

    await engine.dispose()


def test_trt_date_window_aware_girdiyi_trt_takvimine_cevirir():
    """Aware UTC an, UTC gunu degil TRT takvim gunu olarak yorumlanmali."""
    from datetime import UTC

    assert trt_date_window(datetime(2026, 11, 22, tzinfo=UTC)) == (
        datetime(2026, 11, 21, 21), datetime(2026, 11, 22, 21))
    assert trt_date_window(datetime(2026, 11, 22)) == (
        datetime(2026, 11, 21, 21), datetime(2026, 11, 22, 21))


def test_daily_digest_gun_siniri_timeutil_ile_ayni():
    """Gunluk brif ile panel ayni TRT gun penceresini kullanmali (onbellek anahtari)."""
    import daily_digest

    assert daily_digest.trt_day_bounds(datetime(2026, 11, 22)) == trt_date_window(
        datetime(2026, 11, 22))
    assert daily_digest.trt_day_bounds(datetime(2026, 11, 22)) == (
        datetime(2026, 11, 21, 21), datetime(2026, 11, 22, 21))

@pytest.mark.asyncio
async def test_summarize_segment_sohbet_bazli(tmp_path, monkeypatch):
    """Ayni baslikli iki sohbet ayri dilim uretir; tekrar istek onbellekten doner."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from database import Base, Digest, Message

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    calls = []

    async def fake_model(system, user, max_tokens=6000):
        calls.append(system[:10])
        return ({"sectors": [{"name": "x", "evidence": [700]}]},
                {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.25})

    monkeypatch.setattr(digest, "_call_model", fake_model)

    async with session_factory() as db:
        rows = [(1, 700, datetime(2026, 11, 21, 21, 5)),
                (2, 700, datetime(2026, 11, 21, 21, 25)),  # ayni message_id, baska sohbet
                (2, 701, datetime(2026, 11, 21, 22, 15))]
        for chat_id, mid, ts in rows:
            db.add(Message(message_id=mid, chat_id=chat_id, user_id=chat_id, chat_title="Pazar",
                           content=f"mesaj {chat_id}-{mid}", timestamp=ts, is_deleted=False))
        await db.commit()

        made = await digest.summarize_segment(db, datetime(2026, 11, 21, 20),
                                              datetime(2026, 11, 22, 0))
        assert {(d.chat_id, d.window_start) for d in made} == {
            (1, datetime(2026, 11, 21, 20)),
            (2, datetime(2026, 11, 21, 20)),
            (2, datetime(2026, 11, 21, 22)),
        }
        # Kanit sohbetle sinirli: 700 her iki sohbette var ama kendi diliminde dogrulanir.
        for d in made:
            assert d.payload["_evidence"]["scoped"] is True
            if d.window_start == datetime(2026, 11, 21, 20):
                assert d.payload["_evidence"]["ok"] == 1
            else:  # 22:00 diliminde 700 yok; baska sohbette bulundugu ayri sayilir
                assert d.payload["_evidence"]["ok"] == 0
                assert d.payload["_evidence"]["wrong_chat"] == 1

        before = len(calls)
        await digest.summarize_segment(db, datetime(2026, 11, 21, 20), datetime(2026, 11, 22, 0))
        assert len(calls) == before, "onbellekten donmeli, yeni cagri olmamali"
        assert len((await db.execute(select(Digest))).scalars().all()) == 3

    await engine.dispose()
