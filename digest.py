"""Zaman dilimli özet üretimi.

Akış: 2 saatlik dilimler -> her dilim için yapısal çıkarım (JSON) -> günlük
özet için bu dilimlerin üzerinden tek indirgeme çağrısı.

Kurallar:
- Sadece verilen mesajlardan çıkarım; kanıt olarak mesaj kimliği zorunlu.
- Reklam/duyuru mesajları "ad" işaretlenir ve talep sayımına katılmaz.
- Aynı içerik tekilleştirilir: grupta kopyala-yapıştır baskın.
"""

import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Digest, Message
from llm import LLMError, chat_json, model_name, wrap_archive
from timeutil import utc_now

logger = logging.getLogger(__name__)

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


# Kanit listesi tasiyan tum alanlar. self_disclosed/hearsay profil (dossier)
# ciktisinda; digerleri dilim ve gunluk ozetlerinde.
EVIDENCE_FIELDS = ("sectors", "topics", "demands", "actors", "unanswered",
                   "opportunities", "self_disclosed", "hearsay")


def evidence_ids(payload: dict) -> list[int]:
    """Payload'daki butun kanit kimlikleri (profil alanlari dahil)."""
    ids: list[int] = []
    for key in EVIDENCE_FIELDS:
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            for raw in item.get("evidence") or []:
                try:
                    ids.append(int(raw))
                except (TypeError, ValueError):
                    continue
    return ids


async def validate_evidence(db: AsyncSession, payload: dict, chat_id: int | None = None,
                            window_start: datetime | None = None,
                            window_end: datetime | None = None) -> tuple[int, int]:
    """Kanit kimlikleri gercekten arsivde mi? (sohbet + pencere bazli)

    Telegram'da message_id sohbet basina sayilir; kimligi butun tabloda aramak
    baska sohbetin ayni numarali mesajini "dogrulandi" gosteriyordu. Bu yuzden
    arama (chat_id, [window_start, window_end)) icine hapsedilir.

    chat_id verilmezse sohbet siniri yoktur: sessizce yanlis dogrulama yapmak
    yerine uyari loglanir ve payload["_evidence"]["scoped"] = False isaretlenir.
    Cok sohbetli profiller (profiles.py) bu yola duser.

    Donen: (bulunan, toplam).
    """
    ids = set(evidence_ids(payload))
    scoped = chat_id is not None
    if not ids:
        payload["_evidence"] = {"ok": 0, "total": 0, "missing": 0,
                                "wrong_chat": 0, "scoped": scoped}
        return 0, 0
    if not scoped:
        logger.warning(
            "validate_evidence: chat_id verilmedi, kanit dogrulamasi sohbetle "
            "sinirlanamiyor (pencere %s - %s)", window_start, window_end,
        )

    stmt = select(Message.message_id).where(Message.message_id.in_(ids))
    if scoped:
        stmt = stmt.where(Message.chat_id == chat_id)
    if window_start is not None:
        stmt = stmt.where(Message.timestamp >= window_start)
    if window_end is not None:
        stmt = stmt.where(Message.timestamp < window_end)
    found = {r[0] for r in (await db.execute(stmt)).all()}

    # Baska sohbette (veya pencere disinda) bulunan kimlikler dogrulanmis
    # sayilmaz; wrong_chat altinda ayri raporlanir.
    wrong_chat = 0
    if scoped:
        elsewhere = await db.execute(
            select(Message.message_id).where(Message.message_id.in_(ids - found))
        )
        wrong_chat = len({r[0] for r in elsewhere.all()} - found)

    ok = len(found & ids)
    payload["_evidence"] = {
        "ok": ok,
        "total": len(ids),
        "missing": len(ids) - ok,
        "wrong_chat": wrong_chat,
        "scoped": scoped,
    }
    return ok, len(ids)


def bucket_start_for(ts: datetime, hours: int = SEGMENT_HOURS) -> datetime:
    """Bir mesajin dilim baslangici, mesajin KENDI zamanindan hesaplanir.

    Eski hesap istek penceresinin gununden turetiliyordu
    (`start.replace(hour=hour, ...)`), bu yuzden gece yarisini asan dilimler
    bir onceki gune damgalanip gunluk brifin `window_start >= day` filtresinde
    eleniyordu. Artik damga mesajin kendi timestamp'ine bagli.
    """
    base = ts.replace(minute=0, second=0, microsecond=0)
    return base - timedelta(hours=base.hour % hours)


def _collect(rows: Iterable[Message]) -> dict[int, list[Message]]:
    """Sohbet bazinda grupla, ayni icerigi bir kez tut.

    Gruplama anahtari chat_id: basliklar benzersiz degil ve degisebiliyor.
    """
    by_chat: dict[int, list[Message]] = {}
    seen: set[str] = set()
    for m in rows:
        key = (m.content or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        by_chat.setdefault(m.chat_id, []).append(m)
    return by_chat


def _render(messages: list[Message], budget: int) -> str:
    lines, used = [], 0
    for m in messages:
        sender = m.sender_name or m.username or f"user{m.user_id}"
        body = (m.content or "").replace("\n", " ").strip()[:400]
        line = f"[{m.message_id}] {sender}: {body}"
        if used + len(line) > budget:
            if not lines:
                # Tek mesaj bile butceyi asiyorsa dilimi bos birakip atmak
                # yerine kisaltarak gonder (aksi halde sohbet sessizce duser).
                lines.append(line[:max(budget, 1)])
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _segment_block(seg: Digest) -> str:
    title = seg.chat_title or "-"
    return (f"[{title} {seg.window_start.strftime('%H:%M')}-{seg.window_end.strftime('%H:%M')}]\n"
            + json.dumps(seg.payload, ensure_ascii=False))


def pack_segments(segments: Iterable[Digest], budget: int) -> tuple[str, dict]:
    """Dilim ozetlerini JSON farkindalikli bicimde birlestirir.

    Butce asilirsa dilimler sondan BUTUN olarak dusurulur. Eski ham kesme
    (`packed[:MAX_CHARS_PER_CHUNK * 4]`) JSON'u ortadan kesiyor ve gunluk brife
    giren veri sessizce azaliyordu. Donen ikinci deger kirpma raporudur;
    kirpma olmadiysa bos sozluk.
    """
    blocks = [_segment_block(s) for s in segments]
    joined = len("\n\n".join(blocks))
    dropped_chars = dropped_segments = 0
    while len(blocks) > 1 and joined > budget:
        last = blocks.pop()
        dropped_segments += 1
        dropped_chars += len(last) + 2  # aradaki "\n\n" ayirici
        joined = len("\n\n".join(blocks))

    text = "\n\n".join(blocks)
    if len(text) > budget:
        # Tek dilim bile butceyi asiyor: govdeyi kirp ama sonunu isaretle,
        # boylece model yarim veriyle calistigini anlar.
        keep = max(budget - 32, 0)
        dropped_chars += len(text) - keep
        text = text[:keep] + "\n...(kirpildi)"

    if not dropped_chars:
        return text, {}
    logger.warning(
        "Dilim ozetleri kirpildi: %d karakter, %d dilim atildi (butce %d)",
        dropped_chars, dropped_segments, budget,
    )
    return text, {"chars_dropped": dropped_chars,
                  "segments_dropped": dropped_segments,
                  "budget": budget}


def _covered_range(digest: Digest) -> tuple[datetime, datetime]:
    """Kaydin gercekten ozetledigi [baslangic, bitis) araligi.

    Dilim, istek penceresi tarafindan kirpilmis olabilir (gun siniri 21:00 UTC'de
    dustugu icin 20:00 dilimi hep yarim kalir). O durumda kayit kendi
    penceresini kapsamis gibi gorunur ve komsu gunun brifine sizar. Uretim
    sirasinda ozetlenen gercek aralik payload["_covered"] alanina yazilir;
    bu alani tasimayan eski satirlar kendi penceresini kapsar varsayilir.
    """
    raw = (digest.payload or {}).get("_covered")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return datetime.fromisoformat(raw[0]), datetime.fromisoformat(raw[1])
        except (TypeError, ValueError):
            logger.warning("Bozuk _covered alani: id=%s %r", digest.id, raw)
    return digest.window_start, digest.window_end


def _covers(digest: Digest, start: datetime, end: datetime) -> bool:
    """Kayitli ozet [start, end) araligini tamamen kapsiyor mu?"""
    covered_start, covered_end = _covered_range(digest)
    return covered_start <= start and covered_end >= end


async def summarize_segment(db: AsyncSession, start: datetime, end: datetime,
                            force: bool = False,
                            fresh: list[Digest] | None = None) -> list[Digest]:
    """Verilen araligi 2 saatlik dilimlere bolup her dilim icin ozet uretir.

    force=False ise ayni dilim daha once ayni prompt surumuyle uretilmisse
    kayitli satir doner; ikinci tiklama para harcamaz.

    `fresh` verilirse bu istekte LLM'e giden satirlar (yeni veya guncellenmis)
    oraya eklenir; cagiran taraf maliyeti boyle ayirir.
    """
    stmt = (
        select(Message)
        .where(Message.timestamp >= start, Message.timestamp < end)
        .order_by(Message.timestamp)
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return []

    buckets: dict[tuple[int, datetime], list[Message]] = {}
    titles: dict[int, str] = {}
    seen: set[tuple[int, str]] = set()
    for m in rows:
        # Ayni sohbette birebir tekrar eden metin bir kez sayilir: prompt
        # "tekilleştirilmiş" diyor ve msg_count da o sayiyi raporlar. Sohbetler
        # arasi tekilleştirme yok (ayni metin iki sohbette iki ayri mesajdir).
        body_key = (m.chat_id, (m.content or "").strip().lower())
        if not body_key[1] or body_key in seen:
            continue
        seen.add(body_key)
        buckets.setdefault((m.chat_id, bucket_start_for(m.timestamp)), []).append(m)
        # Baslik yalnizca goruntuleme ve eski kayit eslesmesi icin tasinir.
        if m.chat_title:
            titles[m.chat_id] = m.chat_title

    made: list[Digest] = []
    for (chat_id, bucket_start), bucket in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        bucket_end = bucket_start + timedelta(hours=SEGMENT_HOURS)
        chat_title = titles.get(chat_id) or f"Chat {chat_id}"
        # Ayni dilim icin tek satir tutulur: varsa guncellenir, yoksa eklenir.
        existing_q = select(Digest).where(
            Digest.kind == "segment",
            Digest.window_start == bucket_start,
            Digest.prompt_v == PROMPT_V,
        ).order_by(Digest.id.desc()).limit(1)
        existing_q = existing_q.where(or_(
            Digest.chat_id == chat_id,
            # Geri uyum: chat_id kolonu eklenmeden once yazilmis satirlar.
            (Digest.chat_id.is_(None)) & (Digest.chat_title == chat_title),
        ))
        existing = (await db.execute(existing_q)).scalars().first()

        # Istenen aralik bu dilimin tamamini kapsamayabilir (gun siniri ya da
        # "son N saat" penceresi dilimi keser). Ozet yalnizca [need_start,
        # need_end) araligini kapsar; bu aralik payload'a yazilir.
        need_start = max(bucket_start, start)
        need_end = min(bucket_end, end)

        # Onbellek ancak kayitli ozet istenen araligi TAM kapsiyorsa ve o
        # araliktaki mesajlarin hepsini icerdiyse gecerli. Sadece msg_count'a
        # bakmak, kirpilmis eski bir dilimin tam dilim isteginde donup yanlis
        # gunun brifine girmesine yol aciyordu.
        if (existing is not None and not force
                and _covers(existing, need_start, need_end)
                and existing.msg_count >= len(bucket)):
            made.append(existing)
            continue

        body = _render(bucket, MAX_CHARS_PER_CHUNK)
        if not body:
            continue
        prompt = SEGMENT_USER.format(
            start=bucket_start.strftime("%d.%m.%Y %H:%M"),
            end=bucket_end.strftime("%H:%M"),
            chat_title=chat_title,
            count=len(bucket),
            # Mesaj metni guvenilmez veri: llm.py DATA_RULE'unu tetikleyen
            # veri blogunda gonderilir.
            body=wrap_archive(body),
            schema=SCHEMA_HINT,
        )
        payload, usage = await _call_model(SEGMENT_SYSTEM, prompt)
        # Ozetin gercekten kapsadigi aralik (dilim kirpilmis olabilir): gunluk
        # brif bu alanla hangi dilimin hangi gune ait olduguna karar verir.
        payload["_covered"] = [need_start.isoformat(), need_end.isoformat()]
        # Kanit, modelin gercekten gordugu aralikta olmali (dilim kirpilmis
        # olabilir), dilimin damgasinda degil.
        ok, total = await validate_evidence(db, payload, chat_id=chat_id,
                                            window_start=need_start, window_end=need_end)
        digest = existing or Digest(kind="segment", window_start=bucket_start)
        digest.chat_id = chat_id
        digest.chat_title = chat_title
        digest.window_end = bucket_end
        digest.msg_count = len(bucket)
        digest.tokens_in = int(usage.get("prompt_tokens") or 0)
        digest.tokens_out = int(usage.get("completion_tokens") or 0)
        digest.cost = float(usage.get("cost") or 0.0)
        digest.model = model_name()
        digest.prompt_v = PROMPT_V
        digest.evidence_ok = ok
        digest.evidence_total = total
        digest.payload = payload
        if existing is None:
            db.add(digest)
        made.append(digest)
        if fresh is not None:
            # Yeni satir da guncellenmis satir da bu istekte LLM'e gitti.
            fresh.append(digest)

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
        day = (day or utc_now()).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day + timedelta(days=1)

    if not force:
        cached = (await db.execute(
            select(Digest).where(
                Digest.kind == "daily",
                Digest.chat_id.is_(None),
                Digest.window_start == day,
                Digest.prompt_v == PROMPT_V,
            ).order_by(Digest.id.desc()).limit(1)
        )).scalars().first()
        if cached is not None and (cached.payload or {}).get("sectors"):
            return cached

    stmt = (
        select(Digest)
        .where(
            Digest.kind == "segment",
            # Dilim TRT gun sinirini kesebilir (00:00 TRT = 21:00 UTC); sadece
            # window_start'a bakmak gunun ilk/son dilimini eliyordu.
            Digest.window_start < day_end,
            Digest.window_end > day,
        )
        .order_by(Digest.window_start)
    )
    segments = (await db.execute(stmt)).scalars().all()
    # Penceresi gunle kesisen her dilim gunun verisi degildir: dilim bir istek
    # penceresiyle kirpilmis olabilir (20:00 damgali dilim yalnizca 21:00
    # sonrasini ozetler). Kaydin gercekten kapsadigi araliga bakmazsak komsu
    # gunun mesajlari iki brife birden girer.
    kept_in_day: list[Digest] = []
    for s in segments:
        covered_start, covered_end = _covered_range(s)
        if covered_start < day_end and covered_end > day:
            kept_in_day.append(s)
    segments = kept_in_day
    # Ayni sohbet+dilim icin eski/yeni satir cifti olabilir (baslik degisti ya
    # da chat_id kolonu eklenmeden once yazildi); en yeni kayit gecerlidir,
    # yoksa ayni saat iki kez sayilir. Kimlik anahtari chat_id'dir; eski
    # satirlarda chat_id NULL oldugu icin baslik kullanilir (basliklar
    # benzersiz degil, bu yuzden yalnizca son care).
    newest: dict[tuple, Digest] = {}
    for s in segments:
        chat_key = s.chat_id if s.chat_id is not None else f"title:{s.chat_title}"
        key = (chat_key, s.window_start)
        cur = newest.get(key)
        if cur is None or (s.id or 0, s.msg_count) > (cur.id or 0, cur.msg_count):
            newest[key] = s
    segments = sorted(newest.values(), key=lambda s: (s.window_start, s.chat_title or ""))
    if not segments:
        segments = await summarize_segment(db, day, day_end)
        if not segments:
            raise DigestError("Bu aralikta mesaj yok.")

    packed, truncation = pack_segments(segments, MAX_CHARS_PER_CHUNK * 4)
    # Kirpma sondan butun dilimleri dusurur; prompt ve msg_count modele
    # gercekten giden dilimleri anlatmali, dusenleri degil.
    used = segments
    if truncation:
        truncation["segments_used"] = len(segments) - truncation["segments_dropped"]
        used = segments[:truncation["segments_used"]]
    prompt = REDUCE_USER.format(
        date=day.strftime("%d.%m.%Y"),
        segment_count=len(used),
        segments=wrap_archive(packed),
    )
    payload, usage = await _call_model(REDUCE_SYSTEM, prompt)
    if truncation:
        # Sessiz kirpma yok: panel kirpildigini gorebilsin.
        payload["_truncated"] = truncation
    # Gunluk brif cok sohbetli; chat_id tek deger olamaz, pencere ile sinirla.
    ok, total = await validate_evidence(db, payload, window_start=day, window_end=day_end)

    digest = Digest(
        kind="daily",
        chat_id=None,
        chat_title=None,
        window_start=day,
        window_end=day_end,
        msg_count=sum(s.msg_count for s in used),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        cost=float(usage.get("cost") or 0.0),
        model=model_name(),
        prompt_v=PROMPT_V,
        evidence_ok=ok,
        evidence_total=total,
        payload=payload,
    )
    db.add(digest)
    await db.commit()
    return digest
