"""Bir Telegram kimliğinin LLM ile çıkarılmış profili.

Sadece operatör istediğinde çalışır (panelden "Profil çıkar"): o kullanıcının
kendi mesajlarına bakar, hangi sektörlerde göründüğünü, kendisi hakkında ne
açıkladığını ve davranış desenini yapısal JSON olarak üretir.

Kurallar:
- Yalnızca o kişinin kendi mesajları; başkalarının iddiaları "duyum" olarak
  ayrı işaretlenir ve kanıt kimliği ister.
- Kanıt kimlikleri veritabanında doğrulanır.
- Profil 24 saat önbelleklenir; tekrar istemek para harcamaz (force ile aşılır).
"""

import json
import os
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Message, Profile
from digest import DigestError, validate_evidence
from llm import chat_json, model_name

PROMPT_V = 1
MAX_MESSAGES = 400
CACHE_HOURS = 24

PROFILE_SYSTEM = (
    "Bir Telegram hesabının kendi mesajlarına bakıp o kişinin profilini çıkarıyorsun. "
    "Yalnızca yazdıklarından çıkarım yap; kimliği, adresi veya suç isnadı uydurma. "
    "SEKTÖR, para kazanmak için uğraşılan iş alanıdır (banka hesabı, SMS/VoIP doğrulama, "
    "kripto takas, yazılım paneli, oyun içi ürün, danışmanlık gibi). "
    "Kişinin kendisi hakkında açıkça söylediklerini (şehir, meslek, yaş, iletişim, çalışma "
    "saatleri) 'self_disclosed' altına al ve her birine mesaj kimliği ver. "
    "Duyduğunu aktardığı şeyleri 'hearsay' olarak ayır. "
    "Yanıtı SADECE geçerli JSON olarak ver, markdown yok."
)

PROFILE_USER = """Kullanıcı: {name} (id {user_id})
Toplam mesaj: {total} | Sohbet: {chats} | İlk: {first} | Son: {last}
Aşağıda bu kişinin kendi mesajlarından {sample} tanesi var, "[id] sohbet: içerik" biçiminde:

{body}

Şu JSON şemasına uygun tek bir nesne döndür:
{{
  "summary": "2-3 cümlelik profil",
  "role": "satici | alici | aracı | gozlemci | bilinmiyor",
  "sectors": [{{"name": "sektör", "first_seen": "GG.AA.YYYY", "last_seen": "GG.AA.YYYY", "note": "1 cümle", "evidence": [id]}}],
  "self_disclosed": [{{"claim": "kendi hakkında söylediği", "category": "sehir | meslek | iletisim | rutin | diger", "evidence": [id]}}],
  "hearsay": [{{"claim": "başkasından aktardığı", "evidence": [id]}}],
  "behavior": {{"active_hours": ["00-06"], "style": "kisa aciklama", "activity": "surekli | aralikli | nadir"}},
  "aliases": ["gorulen farkli isimler"],
  "notes": ["dikkat çeken 1 cümle"]
}}

Kurallar: sectors en fazla 5, self_disclosed en fazla 6, hearsay en fazla 4, aliases en fazla 5.
Kanıt kimliği olmayan iddiayı yazma."""


async def _stats(db: AsyncSession, user_id: int) -> dict:
    row = (await db.execute(
        select(
            func.count(Message.id),
            func.count(func.distinct(Message.chat_id)),
            func.min(Message.timestamp),
            func.max(Message.timestamp),
        ).where(Message.user_id == user_id)
    )).one()
    name = (await db.execute(
        select(Message.sender_name, Message.username)
        .where(Message.user_id == user_id)
        .order_by(Message.timestamp.desc()).limit(1)
    )).first()
    return {
        "total": row[0] or 0,
        "chats": row[1] or 0,
        "first": row[2],
        "last": row[3],
        "name": (name[0] if name else None) or f"user{user_id}",
        "username": name[1] if name else None,
    }


async def build_profile(db: AsyncSession, user_id: int, force: bool = False) -> Profile:
    existing = (await db.execute(
        select(Profile).where(Profile.user_id == user_id)
    )).scalars().first()

    if existing is not None and not force:
        age = datetime.utcnow() - (existing.created_at or datetime.utcnow())
        if age < timedelta(hours=CACHE_HOURS):
            return existing

    stats = await _stats(db, user_id)
    if not stats["total"]:
        raise DigestError("Bu kullanıcıya ait mesaj yok.")

    rows = (await db.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(Message.timestamp.desc())
        .limit(MAX_MESSAGES)
    )).scalars().all()

    # Zaman içine yaymak için: en yeni 200 + en eski 200 (profil geçmişi görsün).
    if len(rows) > 200:
        rows = rows[:200] + rows[-200:]

    lines, used = [], 0
    for m in sorted(rows, key=lambda x: x.timestamp):
        body = (m.content or "").replace("\n", " ").strip()[:300]
        if not body:
            continue
        line = f"[{m.message_id}] {(m.chat_title or '?')[:24]}: {body}"
        if used + len(line) > 22000:
            break
        lines.append(line)
        used += len(line) + 1

    prompt = PROFILE_USER.format(
        name=stats["name"],
        user_id=user_id,
        total=stats["total"],
        chats=stats["chats"],
        first=stats["first"].strftime("%d.%m.%Y") if stats["first"] else "-",
        last=stats["last"].strftime("%d.%m.%Y") if stats["last"] else "-",
        sample=len(lines),
        body="\n".join(lines),
    )

    payload, usage = await chat_json(PROFILE_SYSTEM, prompt, max_tokens=6000)
    ok, total = await validate_evidence(db, payload)
    payload.setdefault("_evidence", {})["ok"] = ok
    payload["_evidence"]["total"] = total

    if existing is None:
        existing = Profile(user_id=user_id, payload=payload)
        db.add(existing)

    existing.display_name = stats["name"]
    existing.msg_count = stats["total"]
    existing.chat_count = stats["chats"]
    existing.first_seen = stats["first"]
    existing.last_seen = stats["last"]
    existing.tokens_in = int(usage.get("prompt_tokens") or 0)
    existing.tokens_out = int(usage.get("completion_tokens") or 0)
    existing.cost = float((usage.get("opencode") or {}).get("list_cost") or 0.0)
    existing.model = model_name()
    existing.prompt_v = PROMPT_V
    existing.payload = payload
    existing.created_at = datetime.utcnow()

    await db.commit()
    await db.refresh(existing)
    return existing


def serialize(p: Profile) -> dict:
    return {
        "user_id": p.user_id,
        "display_name": p.display_name,
        "msg_count": p.msg_count,
        "chat_count": p.chat_count,
        "first_seen": p.first_seen.isoformat() + "Z" if p.first_seen else None,
        "last_seen": p.last_seen.isoformat() + "Z" if p.last_seen else None,
        "tokens_in": p.tokens_in,
        "tokens_out": p.tokens_out,
        "cost": p.cost,
        "model": p.model,
        "prompt_v": p.prompt_v,
        "created_at": p.created_at.isoformat() + "Z" if p.created_at else None,
        "payload": p.payload,
    }
