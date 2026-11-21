"""Aktör profili uçnoktaları: istek üzerine LLM profili, kayıtlı profil, mesaj tekili."""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Message, Profile
from digest import DigestError
from profiles import build_profile, serialize
from routes.auth import get_db, is_authenticated

router = APIRouter()


@router.get("/api/message/{message_id}")
async def get_message(
    message_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Kanıt kimliğine tıklanınca tek mesajı döner."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    row = (await db.execute(
        select(Message).where(Message.message_id == message_id)
        .order_by(Message.timestamp.desc()).limit(1)
    )).scalars().first()
    if row is None:
        return JSONResponse(status_code=404, content={"detail": "Mesaj arsivde yok"})

    return {
        "message_id": row.message_id,
        "chat_id": row.chat_id,
        "chat_title": row.chat_title,
        "user_id": row.user_id,
        "username": row.username,
        "sender_name": row.sender_name,
        "content": row.content,
        "timestamp": row.timestamp.isoformat() + "Z",
        "is_deleted": row.is_deleted,
    }


@router.get("/api/profile/{user_id}/dossier")
async def get_dossier(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    row = (await db.execute(select(Profile).where(Profile.user_id == user_id))).scalars().first()
    if row is None:
        return {"exists": False}
    return {"exists": True, **serialize(row)}


@router.post("/api/profile/{user_id}/dossier")
async def make_dossier(
    user_id: int,
    request: Request,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Bir kimliğin mesajlarından profil çıkarır (istek üzerine, önbellekli)."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    try:
        profile = await build_profile(db, user_id, force=force)
    except DigestError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    return serialize(profile)
