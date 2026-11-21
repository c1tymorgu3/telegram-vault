from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, text
from datetime import datetime, timedelta
from database import Message, Chat
from routes.auth import is_authenticated, get_db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="index.html", context={})


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="admin.html", context={})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={})


@router.get("/api/chats")
async def list_chats(request: Request, db: AsyncSession = Depends(get_db)):
    """Every chat the listener knows about, not just the ones that spoke."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    stmt = (
        select(
            Chat.chat_id,
            Chat.title,
            Chat.username,
            Chat.chat_type,
            Chat.member_count,
            Chat.last_message_at,
            func.count(Message.id).label("messages"),
        )
        .outerjoin(Message, Message.chat_id == Chat.chat_id)
        .group_by(Chat.chat_id)
        .order_by(desc(func.coalesce(Chat.last_message_at, datetime(1970, 1, 1))))
        .limit(500)
    )
    rows = (await db.execute(stmt)).all()
    return {
        "total": len(rows),
        "chats": [
            {
                "chat_id": r.chat_id,
                "title": r.title,
                "username": r.username,
                "chat_type": r.chat_type,
                "member_count": r.member_count,
                "last_message_at": r.last_message_at.isoformat() if r.last_message_at else None,
                "messages": r.messages,
            }
            for r in rows
        ],
    }


@router.get("/api/dashboard")
async def get_dashboard_stats(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    now = datetime.utcnow()
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(hours=24)

    total_stmt = select(func.count(Message.id))
    total = (await db.execute(total_stmt)).scalar()

    deleted_stmt = select(func.count(Message.id)).where(Message.is_deleted == True)
    total_deleted = (await db.execute(deleted_stmt)).scalar()

    last_hour_stmt = select(func.count(Message.id)).where(Message.timestamp >= one_hour_ago)
    last_hour = (await db.execute(last_hour_stmt)).scalar()

    active_users_stmt = select(func.count(func.distinct(Message.user_id))).where(Message.timestamp >= one_day_ago)
    active_users = (await db.execute(active_users_stmt)).scalar()

    top_group_stmt = (
        select(Message.chat_title, func.count(Message.id).label("cnt"))
        .where(Message.timestamp >= one_hour_ago)
        .group_by(Message.chat_title)
        .order_by(desc("cnt"))
        .limit(1)
    )
    top_group_row = (await db.execute(top_group_stmt)).first()
    top_group = {"name": top_group_row[0] or "Yok", "count": top_group_row[1]} if top_group_row else {"name": "Yok", "count": 0}

    top_del_stmt = (
        select(Message.chat_title, func.count(Message.id).label("cnt"))
        .where(Message.timestamp >= one_hour_ago, Message.is_deleted == True)
        .group_by(Message.chat_title)
        .order_by(desc("cnt"))
        .limit(1)
    )
    top_del_row = (await db.execute(top_del_stmt)).first()
    top_deleted_group = {"name": top_del_row[0] or "Yok", "count": top_del_row[1]} if top_del_row else {"name": "Yok", "count": 0}

    dialect = db.bind.dialect.name
    if dialect == 'sqlite':
        velocity_stmt = text("""
            SELECT strftime('%Y-%m-%d %H:00:00', timestamp) AS hr, COUNT(*) AS cnt
            FROM messages
            WHERE timestamp >= :since
            GROUP BY hr
            ORDER BY hr ASC
        """)
    else:
        velocity_stmt = text("""
            SELECT date_trunc('hour', timestamp) AS hr, COUNT(*) AS cnt
            FROM messages
            WHERE timestamp >= :since
            GROUP BY hr
            ORDER BY hr ASC
        """)
    
    velocity_rows = (await db.execute(velocity_stmt, {"since": one_day_ago})).all()

    velocity_map = {}
    for row in velocity_rows:
        hr_val = row[0]
        if isinstance(hr_val, str):
            hr_val = datetime.strptime(hr_val, "%Y-%m-%d %H:%M:%S")
        tr_hour = (hr_val + timedelta(hours=3)).strftime("%H:00")
        velocity_map[tr_hour] = row[1]

    velocity = []
    for h in range(24, 0, -1):
        slot_utc = now - timedelta(hours=h)
        tr_hour = (slot_utc + timedelta(hours=3)).strftime("%H:00")
        velocity.append({"hour": tr_hour, "count": velocity_map.get(tr_hour, 0)})

    top_groups_stmt = (
        select(Message.chat_title, func.count(Message.id).label("cnt"))
        .where(Message.timestamp >= one_day_ago)
        .group_by(Message.chat_title)
        .order_by(desc("cnt"))
        .limit(5)
    )
    top_groups_rows = (await db.execute(top_groups_stmt)).all()
    top_groups = [{"name": r[0] or "Private", "count": r[1]} for r in top_groups_rows]

    top_users_stmt = (
        select(Message.username, Message.sender_name, Message.user_id, func.count(Message.id).label("cnt"))
        .where(Message.timestamp >= one_day_ago)
        .group_by(Message.username, Message.sender_name, Message.user_id)
        .order_by(desc("cnt"))
        .limit(5)
    )
    top_users_rows = (await db.execute(top_users_stmt)).all()
    top_users = [{"username": r[0] or "?", "name": r[1] or "?", "user_id": r[2], "count": r[3]} for r in top_users_rows]

    del_groups_stmt = (
        select(Message.chat_title, func.count(Message.id).label("cnt"))
        .where(Message.timestamp >= one_day_ago, Message.is_deleted == True)
        .group_by(Message.chat_title)
        .order_by(desc("cnt"))
        .limit(5)
    )
    del_groups = [{"name": r[0] or "Private", "count": r[1]} for r in (await db.execute(del_groups_stmt)).all()]

    storage = {"sqlite": "SQLite", "postgresql": "PostgreSQL"}.get(dialect, dialect)

    return {
        "storage": storage,
        "total": total,
        "total_deleted": total_deleted,
        "last_hour": last_hour,
        "active_users_24h": active_users,
        "top_group": top_group,
        "top_deleted_group": top_deleted_group,
        "velocity": velocity,
        "deleted_groups": del_groups,
        "top_groups": top_groups,
        "top_users": top_users,
        "generated_at": (now + timedelta(hours=3)).strftime("%H:%M:%S")
    }


@router.get("/api/stream-stats")
async def stream_stats(request: Request, db: AsyncSession = Depends(get_db)):
    """Everything the live view needs, in one call.

    Per-minute resolution for the last hour, the counters that sit beside it,
    and the groups currently producing traffic. The dashboard keeps the slow
    daily view; this one is tuned for someone watching the feed.
    """
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    now = datetime.utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)
    dialect = db.bind.dialect.name

    if dialect == "sqlite":
        bucket = "strftime('%Y-%m-%d %H:%M', timestamp)"
    else:
        bucket = "to_char(date_trunc('minute', timestamp), 'YYYY-MM-DD HH24:MI')"

    rows = (await db.execute(
        text(f"""
            SELECT {bucket} AS m, COUNT(*) AS cnt
            FROM messages
            WHERE timestamp >= :since
            GROUP BY m ORDER BY m ASC
        """), {"since": hour_ago}
    )).all()

    del_rows = (await db.execute(
        text(f"""
            SELECT {bucket} AS m, COUNT(*) AS cnt
            FROM messages
            WHERE timestamp >= :since AND is_deleted = :yes
            GROUP BY m ORDER BY m ASC
        """), {"since": hour_ago, "yes": True}
    )).all()

    minute_map, del_map = {}, {}
    for raw, cnt in rows:
        key = raw if isinstance(raw, str) else raw.strftime("%Y-%m-%d %H:%M")
        minute_map[key] = cnt
    for raw, cnt in del_rows:
        key = raw if isinstance(raw, str) else raw.strftime("%Y-%m-%d %H:%M")
        del_map[key] = cnt

    per_minute, deleted_per_minute = [], []
    for i in range(60, 0, -1):
        slot = (now - timedelta(minutes=i)).replace(second=0, microsecond=0)
        key = slot.strftime("%Y-%m-%d %H:%M")
        tr = slot + timedelta(hours=3)
        label = tr.strftime("%H:%M")
        per_minute.append({"minute": label, "count": minute_map.get(key, 0)})
        deleted_per_minute.append({"minute": label, "count": del_map.get(key, 0)})

    total = (await db.execute(select(func.count(Message.id)))).scalar()
    last_hour = (await db.execute(
        select(func.count(Message.id)).where(Message.timestamp >= hour_ago)
    )).scalar()
    deleted_hour = (await db.execute(
        select(func.count(Message.id)).where(Message.timestamp >= hour_ago, Message.is_deleted == True)
    )).scalar()
    deleted_day = (await db.execute(
        select(func.count(Message.id)).where(Message.timestamp >= day_ago, Message.is_deleted == True)
    )).scalar()
    groups_hour = (await db.execute(
        select(func.count(func.distinct(Message.chat_title))).where(Message.timestamp >= hour_ago)
    )).scalar()

    group_rows = (await db.execute(
        select(Message.chat_title, func.count(Message.id).label("cnt"))
        .where(Message.timestamp >= hour_ago)
        .group_by(Message.chat_title)
        .order_by(desc("cnt"))
        .limit(4)
    )).all()

    del_group_rows = (await db.execute(
        select(Message.chat_title, func.count(Message.id).label("cnt"))
        .where(Message.timestamp >= hour_ago, Message.is_deleted == True)
        .group_by(Message.chat_title)
        .order_by(desc("cnt"))
        .limit(3)
    )).all()

    last_row = (await db.execute(
        select(Message.timestamp).order_by(desc(Message.timestamp)).limit(1)
    )).first()
    last_seen = None
    if last_row and last_row[0]:
        last_seen = (last_row[0] + timedelta(hours=3)).strftime("%H:%M:%S")

    peak = max(per_minute, key=lambda p: p["count"]) if per_minute else {"minute": "-", "count": 0}

    return {
        "per_minute": per_minute,
        "deleted_per_minute": deleted_per_minute,
        "deleted_groups_hour": [{"name": r[0] or "Private", "count": r[1]} for r in del_group_rows],
        "peak": peak,
        "last_seen": last_seen,
        "total": total or 0,
        "last_hour": last_hour or 0,
        "deleted_hour": deleted_hour or 0,
        "deleted_day": deleted_day or 0,
        "groups_hour": groups_hour or 0,
        "groups": [{"name": r[0] or "Private", "count": r[1]} for r in group_rows],
        "generated_at": (now + timedelta(hours=3)).strftime("%H:%M:%S"),
    }
