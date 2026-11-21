"""Zaman dilimli özet üretimi.

Akış: 2 saatlik dilimler -> her dilim için yapısal çıkarım (JSON) -> günlük
özet için bu dilimlerin üzerinden tek indirgeme çağrısı.

Kurallar:
- Sadece verilen mesajlardan çıkarım; kanıt olarak mesaj kimliği zorunlu.
- Reklam/duyuru mesajları "ad" işaretlenir ve talep sayımına katılmaz.
- Aynı içerik tekilleştirilir: grupta kopyala-yapıştır baskın.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Digest, Message
from llm import LLMError, chat_json, model_name

PROMPT_V = 2
SEGMENT_HOURS = int(os.getenv("DIGEST_SEGMENT_HOURS", "2"))
MAX_CHARS_PER_CHUNK = int(os.getenv("DIGEST_MAX_CHARS", "26000"))

SCHEMA_HINT = """{
  "sectors": [{"name": "sektor", "senders": 0, "msgs": 0, "summary": "1 cumle", "evidence": [mesaj_id]}],
  "topics": [{"label": "kisa etiket", "senders": 0, "summary": "1 cumle", "evidence": [mesaj_id]}],
  "demands": [{"what": "ne araniyor", "count": 0, "evidence": [mesaj_id]}],
  "actors": [{"user_id": 0, "name": "ad", "msgs": 0, "note": "1 cumle", "evidence": [mesaj_id]}],
  "unanswered": [{"text": "cevapsiz kalan talep", "user_id": 0, "evidence": [mesaj_id]}],
  "ad_share": 0.0,
  "notes": ["gozleme dayali 1 cumle"]
}"""

SEGMENT_SYSTEM = (
    "Bir Telegram mesaj arşivini analiz ediyorsun. Yalnızca verilen mesajlardan "
    "çıkarım yap; olmayan bir şeyi yazma. Her iddia için mesaj kimliği (id) ver. "
    "Fiyat bilgisi çoğu zaman yok, fiyat uydurma. Reklam, duyuru, bot mesajlarını "
    "talep sayımına katma; bunların oranını ad_share olarak bildir. "
    "SEKTÖR, insanların para kazanmak için uğraştığı iş alanıdır (banka hesabı alım-satımı, "
    "SMS/VoIP doğrulama, kripto takas, yazılım paneli, oyun içi ürün gibi). "
    "Bir sektör yalnızca birden fazla kişi konuştuysa listeye girer. "
    "Yanıtı SADECE geçerli JSON olarak ver, markdown yok."
)

SEGMENT_USER = """Zaman aralığı: {start} - {end} (UTC)
Sohbet: {chat_title}
Mesaj sayısı: {count} (tekilleştirilmiş)

Mesajlar, her satır "[id] gönderen: içerik" biçiminde:

{body}

Şu JSON şemasına uygun tek bir nesne döndür:
{schema}

Kurallar: sectors en fazla 4, topics en fazla 6, demands en fazla 8, actors en fazla 8,
unanswered en fazla 5. "senders" farklı kişi sayısıdır, mesaj sayısı değil."""

REDUCE_SYSTEM = (
    "Elinde bir günün dilim özetleri var. Bunları tek bir günlük brife indirgeyeceksin. "
    "Dilim özetlerinde olmayan hiçbir şeyi ekleme; sayıları topla, tekrarları birleştir. "
    "Çıktı bir pazar raporu gibi okunmalı: önce hangi sektörler canlı, sonra nerede "
    "karşılanmayan talep (iş fırsatı) var, sonra kimler öne çıkıyor. Kanıt kimliklerini koru. "
    "Yanıtı SADECE geçerli JSON olarak ver, markdown yok."
)

REDUCE_USER = """Tarih: {date} (UTC)
{segment_count} dilim özeti aşağıda. Bunları tek brife indirge.

{segments}

Şu JSON şemasına uygun tek bir nesne döndür:
{{
  "headline": "günün tek cümlelik özeti",
  "sectors": [{{"name": "", "senders": 0, "summary": "bu sektörde bugün ne oldu", "evidence": [id]}}],
  "opportunities": [{{"what": "karşılanmayan talep", "why": "neden fırsat", "evidence": [id]}}],
  "demands": [{{"what": "", "count": 0, "evidence": [id]}}],
  "actors": [{{"user_id": 0, "name": "", "msgs": 0, "note": "", "evidence": [id]}}],
  "unanswered": [{{"text": "", "evidence": [id]}}],
  "quiet_hours": ["00", "05"],
  "notes": [""]
}}
Kurallar: sectors en fazla 5, opportunities en fazla 5, demands en fazla 6, actors en fazla 6.
opportunities yalnızca gerçekten cevapsız kalmış taleplerden üretilir; genel tahmin yazma.
quiet_hours: mesaj sayısı belirgin düşük saatler (UTC, 2 hane)."""


# Saglayici secimi llm.py'de; geriye donuk isim korunuyor.
DigestError = LLMError


async def _call_model(system: str, user: str, max_tokens: int = 6000) -> tuple[dict, dict]:
    return await chat_json(system, user, max_tokens)


def evidence_ids(payload: dict) -> list[int]:
    ids: list[int] = []
    for key in ("sectors", "topics", "demands", "actors", "unanswered", "opportunities"):
        for item in payload.get(key) or []:
            for raw in item.get("evidence") or []:
                try:
                    ids.append(int(raw))
                except (TypeError, ValueError):
                    continue
    return ids


async def validate_evidence(db: AsyncSession, payload: dict) -> tuple[int, int]:
    """Brife giren mesaj kimlikleri gercekten arsivde mi?

    Modelin uydurdugu bir fikri degil, var olan mesaji kanit gostermesini olcer.
    Donen: (bulunan, toplam).
    """
    ids = set(evidence_ids(payload))
    if not ids:
        payload["_evidence"] = {"ok": 0, "total": 0, "missing": 0}
        return 0, 0
    rows = await db.execute(select(Message.message_id).where(Message.message_id.in_(ids)))
    found = {r[0] for r in rows.all()}
    ok = len(found & ids)
    payload["_evidence"] = {"ok": ok, "total": len(ids), "missing": len(ids - found)}
    return ok, len(ids)


def _collect(rows: Iterable[Message]) -> dict[str, list[Message]]:
    """Sohbet bazinda grupla, ayni icerigi bir kez tut."""
    by_chat: dict[str, list[Message]] = {}
    seen: set[str] = set()
    for m in rows:
        key = (m.content or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        by_chat.setdefault(m.chat_title or "?", []).append(m)
    return by_chat


def _render(messages: list[Message], budget: int) -> str:
    lines, used = [], 0
    for m in messages:
        sender = m.sender_name or m.username or f"user{m.user_id}"
        body = (m.content or "").replace("\n", " ").strip()[:400]
        line = f"[{m.message_id}] {sender}: {body}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


async def summarize_segment(db: AsyncSession, start: datetime, end: datetime,
                            force: bool = False) -> list[Digest]:
    """Verilen araligi 2 saatlik dilimlere bolup her dilim icin ozet uretir.

    force=False ise ayni dilim daha once ayni prompt surumuyle uretilmisse
    kayitli satir doner; ikinci tiklama para harcamaz.
    """
    stmt = (
        select(Message)
        .where(Message.timestamp >= start, Message.timestamp < end)
        .order_by(Message.timestamp)
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return []

    buckets: dict[tuple[str, int], list[Message]] = {}
    for m in rows:
        hour = m.timestamp.hour - (m.timestamp.hour % SEGMENT_HOURS)
        buckets.setdefault((m.chat_title or "?", hour), []).append(m)

    made: list[Digest] = []
    for (chat_title, hour), bucket in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        bucket_start = start.replace(hour=hour, minute=0, second=0, microsecond=0)
        bucket_end = bucket_start + timedelta(hours=SEGMENT_HOURS)

        if not force:
            cached = (await db.execute(
                select(Digest).where(
                    Digest.kind == "segment",
                    Digest.chat_title == chat_title,
                    Digest.window_start == bucket_start,
                    Digest.prompt_v == PROMPT_V,
                ).order_by(Digest.id.desc()).limit(1)
            )).scalars().first()
            if cached is not None:
                made.append(cached)
                continue

        body = _render(bucket, MAX_CHARS_PER_CHUNK)
        if not body:
            continue
        prompt = SEGMENT_USER.format(
            start=bucket_start.strftime("%d.%m.%Y %H:%M"),
            end=bucket_end.strftime("%H:%M"),
            chat_title=chat_title,
            count=len(bucket),
            body=body,
            schema=SCHEMA_HINT,
        )
        payload, usage = await _call_model(SEGMENT_SYSTEM, prompt)
        ok, total = await validate_evidence(db, payload)
        digest = Digest(
            kind="segment",
            chat_title=chat_title,
            window_start=bucket_start,
            window_end=bucket_end,
            msg_count=len(bucket),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cost=float((usage.get("opencode") or {}).get("list_cost") or 0.0),
            model=model_name(),
            prompt_v=PROMPT_V,
            evidence_ok=ok,
            evidence_total=total,
            payload=payload,
        )
        db.add(digest)
        made.append(digest)

    await db.commit()
    return made


async def summarize_day(db: AsyncSession, day: datetime | None = None,
                        window: tuple[datetime, datetime] | None = None,
                        force: bool = False) -> Digest:
    """Dilim ozetlerinin uzerinden gunluk brif uretir.

    window verilirse gun sinirlari yerine o aralik kullanilir (TRT gunu gibi).
    """
    if window:
        day, day_end = window
    else:
        day = (day or datetime.now(timezone.utc)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day + timedelta(days=1)

    if not force:
        cached = (await db.execute(
            select(Digest).where(
                Digest.kind == "daily",
                Digest.window_start == day,
                Digest.prompt_v == PROMPT_V,
            ).order_by(Digest.id.desc()).limit(1)
        )).scalars().first()
        if cached is not None and (cached.payload or {}).get("sectors"):
            return cached

    stmt = (
        select(Digest)
        .where(Digest.kind == "segment",
               Digest.window_start >= day, Digest.window_start < day_end)
        .order_by(Digest.window_start)
    )
    segments = (await db.execute(stmt)).scalars().all()
    if not segments:
        segments = await summarize_segment(db, day, day_end)
        if not segments:
            raise DigestError("Bu aralikta mesaj yok.")

    packed = "\n\n".join(
        f"[{s.chat_title or '-'} {s.window_start.strftime('%H:%M')}-{s.window_end.strftime('%H:%M')}]\n"
        + json.dumps(s.payload, ensure_ascii=False)
        for s in segments
    )
    prompt = REDUCE_USER.format(
        date=day.strftime("%d.%m.%Y"),
        segment_count=len(segments),
        segments=packed[:MAX_CHARS_PER_CHUNK * 4],
    )
    payload, usage = await _call_model(REDUCE_SYSTEM, prompt)
    ok, total = await validate_evidence(db, payload)

    digest = Digest(
        kind="daily",
        chat_title=None,
        window_start=day,
        window_end=day_end,
        msg_count=sum(s.msg_count for s in segments),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        cost=float((usage.get("opencode") or {}).get("list_cost") or 0.0),
        model=model_name(),
        prompt_v=PROMPT_V,
        evidence_ok=ok,
        evidence_total=total,
        payload=payload,
    )
    db.add(digest)
    await db.commit()
    return digest
