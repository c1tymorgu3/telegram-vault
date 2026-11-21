"""Ozet ucnoktalari: dilim ozeti, gunluk brif, gecmis listesi."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Digest
from digest import DigestError, summarize_day, summarize_segment
from routes.auth import get_db, is_authenticated

router = APIRouter()


def _serialize(d: Digest) -> dict:
    return {
        "id": d.id,
        "kind": d.kind,
        "chat_title": d.chat_title,
        "window_start": d.window_start.isoformat() + "Z",
        "window_end": d.window_end.isoformat() + "Z",
        "msg_count": d.msg_count,
        "tokens_in": d.tokens_in,
        "tokens_out": d.tokens_out,
        "cost": d.cost,
        "model": d.model,
        "prompt_v": d.prompt_v,
        "evidence_ok": d.evidence_ok,
        "evidence_total": d.evidence_total,
        "created_at": d.created_at.isoformat() + "Z" if d.created_at else None,
        "payload": d.payload,
    }


@router.post("/api/digest/segment")
async def digest_segment(
    request: Request,
    hours: int = Query(6, ge=1, le=24),
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Son N saatin dilim ozetlerini uretir (2 saatlik parcalar)."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(hours=hours)
    before = (await db.execute(select(func.max(Digest.id)))).scalar() or 0
    try:
        made = await summarize_segment(db, start, now, force=force)
    except DigestError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    # Cache'ten donen satirlar eski maliyeti tasir; "bu istekte harcanan" ayri.
    fresh = [d for d in made if d.id > before]
    return {
        "segments": len(made),
        "fresh": len(fresh),
        "cached": len(made) - len(fresh),
        "messages": sum(d.msg_count for d in made),
        "tokens_in": sum(d.tokens_in for d in fresh),
        "tokens_out": sum(d.tokens_out for d in fresh),
        "cost": round(sum(d.cost for d in fresh), 6),
        "items": [_serialize(d) for d in made],
    }


@router.post("/api/digest/daily")
async def digest_daily(
    request: Request,
    date: Optional[str] = None,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Dilim ozetlerinden gunluk brif uretir. date=GG.AA.YYYY veya YYYY-MM-DD."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    day = datetime.now(timezone.utc).replace(tzinfo=None)
    if date:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                day = datetime.strptime(date, fmt)
                break
            except ValueError:
                continue
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        digest = await summarize_day(db, day, force=force)
    except DigestError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    return _serialize(digest)


@router.get("/api/digests")
async def list_digests(
    request: Request,
    kind: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    stmt = select(Digest).order_by(desc(Digest.window_start), desc(Digest.id)).limit(limit)
    if kind:
        stmt = stmt.where(Digest.kind == kind)
    rows = (await db.execute(stmt)).scalars().all()
    return {"count": len(rows), "items": [_serialize(d) for d in rows]}
