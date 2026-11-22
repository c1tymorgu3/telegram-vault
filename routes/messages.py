import csv
import io
from datetime import UTC, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Message
from routes.auth import get_db, is_authenticated

router = APIRouter()

# LIKE jokerleri: kullanici '100%' yazinca "%100%%" kalibi her satiri donduruyordu.
LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    r"""%, _ ve \ karakterlerini kacirir; sorguda escape='\\' ile kullanilmali."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def like_pattern(value: str) -> str:
    return f"%{escape_like(value)}%"


# OWASP CSV injection: formul olarak yorumlanan hucre metne cevrilir.
CSV_RISKY_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """Hucre = + - @ TAB CR ile basliyorsa basina ' eklenir; sayisal alanlar da ayni kontrolden gecer."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(CSV_RISKY_PREFIXES):
        return "'" + text
    return text


def apply_filters(stmt, query=None, username=None, chat_title=None,
                  user_id=None, is_deleted=None):
    """Liste/arama ve CSV disa aktarma ayni filtreleri kullanir (tekrar eden bloklar kaldirildi)."""
    if query:
        stmt = stmt.where(Message.content.ilike(like_pattern(query), escape=LIKE_ESCAPE))
    if username:
        stmt = stmt.where(Message.username.ilike(like_pattern(username), escape=LIKE_ESCAPE))
    if chat_title:
        stmt = stmt.where(Message.chat_title.ilike(like_pattern(chat_title), escape=LIKE_ESCAPE))
    if user_id is not None:
        # Tam eslesme: kismi arama 648997000 -> 6489970001 de dondurur.
        stmt = stmt.where(Message.user_id == user_id)
    if is_deleted is not None:
        stmt = stmt.where(Message.is_deleted == is_deleted)
    return stmt


@router.get("/api/messages")
async def search_messages(
    request: Request,
    query: str | None = None,
    username: str | None = None,
    chat_title: str | None = None,
    user_id: int | None = None,
    is_deleted: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=50),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    stmt = select(Message).order_by(desc(Message.timestamp))
    stmt = apply_filters(stmt, query, username, chat_title, user_id, is_deleted)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_count = (await db.execute(count_stmt)).scalar()

    offset = (page - 1) * page_size
    stmt = stmt.limit(page_size).offset(offset)
    messages = (await db.execute(stmt)).scalars().all()

    return {
        "total": total_count,
        "page": page,
        "page_size": page_size,
        "results": [
            {
                "id": m.id,
                "message_id": m.message_id,
                "chat_id": m.chat_id,
                "chat_title": m.chat_title,
                "user_id": m.user_id,
                "username": m.username,
                "sender_name": m.sender_name,
                "content": m.content,
                # UTC + offset: tarayici kendi saat dilimine cevirir, saat
                # kaymasi ve AM/PM karisikligi kalkar.
                "timestamp": m.timestamp.replace(tzinfo=UTC).isoformat(),
                "is_deleted": getattr(m, 'is_deleted', False)
            }
            for m in messages
        ]
    }


@router.get("/api/export")
async def export_messages(
    request: Request,
    query: str | None = None,
    username: str | None = None,
    chat_title: str | None = None,
    user_id: int | None = None,
    is_deleted: bool | None = None,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    stmt = select(Message).order_by(desc(Message.timestamp))
    stmt = apply_filters(stmt, query, username, chat_title, user_id, is_deleted)

    stmt = stmt.limit(10000)
    messages = (await db.execute(stmt)).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=',', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["Mesaj_ID", "Tarih_Saat", "Grup_Kanal", "Gonderen_Ad", "Kullanici_Adi", "User_ID", "Icerik", "Silinmis_Mi"])

    for m in messages:
        tr_time = (m.timestamp + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        writer.writerow([
            csv_safe(m.message_id),
            csv_safe(tr_time),
            csv_safe(m.chat_title or "Ozel"),
            csv_safe(m.sender_name),
            csv_safe(m.username or ""),
            csv_safe(m.user_id),
            csv_safe(m.content.replace('\n', ' ')),
            csv_safe("EVET" if getattr(m, 'is_deleted', False) else "HAYIR")
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vault_OSINT_Export.csv"}
    )


@router.get("/api/profile/{user_id}")
async def get_target_profile(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    latest_msg_stmt = select(Message).where(Message.user_id == user_id).order_by(desc(Message.timestamp)).limit(1)
    first_msg_stmt = select(Message).where(Message.user_id == user_id).order_by(Message.timestamp).limit(1)

    latest_msg = (await db.execute(latest_msg_stmt)).scalar_one_or_none()
    first_msg = (await db.execute(first_msg_stmt)).scalar_one_or_none()

    if not latest_msg:
        return {"error": "User not found"}

    total_stmt = select(func.count(Message.id)).where(Message.user_id == user_id)
    total_count = (await db.execute(total_stmt)).scalar()

    group_stmt = select(Message.chat_title, func.count(Message.id)).where(Message.user_id == user_id).group_by(Message.chat_title).order_by(desc(func.count(Message.id))).limit(10)
    groups = (await db.execute(group_stmt)).all()

    ts_stmt = select(Message.timestamp).where(Message.user_id == user_id)
    timestamps = (await db.execute(ts_stmt)).scalars().all()

    hours = {str(i): 0 for i in range(24)}
    for ts in timestamps:
        tr_time = ts + timedelta(hours=3)
        hours[str(tr_time.hour)] += 1

    target_groups = {g[0] for g in groups if g[0]}
    target_hours_total = sum(hours.values())
    target_hours_pct = {h: (c / target_hours_total if target_hours_total else 0) for h, c in hours.items()}

    clones = []
    if target_hours_total >= 5 and target_groups:
        clone_stmt = select(Message.user_id, Message.username, Message.sender_name, Message.chat_title, Message.timestamp).where(
            Message.chat_title.in_(list(target_groups)),
            Message.user_id != user_id
        )
        clone_rows = (await db.execute(clone_stmt)).all()

        user_data = {}
        for r in clone_rows:
            uid = r[0]
            if uid not in user_data:
                user_data[uid] = {'username': r[1], 'name': r[2], 'groups': set(), 'hours': {str(i): 0 for i in range(24)}, 'total': 0}
            if r[3]:
                user_data[uid]['groups'].add(r[3])

            tr_h = str((r[4] + timedelta(hours=3)).hour)
            user_data[uid]['hours'][tr_h] += 1
            user_data[uid]['total'] += 1

        for uid, data in user_data.items():
            if data['total'] < 3:
                continue

            intersection = len(target_groups.intersection(data['groups']))
            union = len(target_groups.union(data['groups']))
            group_score = (intersection / union) * 100 if union > 0 else 0

            overlap = 0
            for h in range(24):
                h_str = str(h)
                pct_a = target_hours_pct[h_str]
                pct_b = data['hours'][h_str] / data['total']
                overlap += min(pct_a, pct_b)
            time_score = overlap * 100

            total_score = (group_score * 0.4) + (time_score * 0.6)

            if total_score >= 35:
                clones.append({
                    "user_id": uid,
                    "username": data['username'],
                    "name": data['name'],
                    "score": round(total_score, 1),
                    "shared_groups": intersection
                })

        clones = sorted(clones, key=lambda x: x['score'], reverse=True)[:3]

    return {
        "user_id": user_id,
        "username": latest_msg.username or "Yok",
        "sender_name": latest_msg.sender_name or "Bilinmiyor",
        "total": total_count,
        "first_seen": first_msg.timestamp.replace(tzinfo=UTC).isoformat(),
        "last_seen": latest_msg.timestamp.replace(tzinfo=UTC).isoformat(),
        "groups": [{"name": g[0] or "Private", "count": g[1]} for g in groups],
        "hours": hours,
        "clones": clones
    }
