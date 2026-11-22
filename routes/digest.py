"""Ozet ucnoktalari: dilim ozeti, gunluk brif, gecmis listesi."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Digest
from digest import DigestError, summarize_day, summarize_segment
from routes.auth import get_db, is_authenticated
from timeutil import day_bounds_trt, trt_date_window, utc_now

router = APIRouter()


def _serialize(d: Digest) -> dict:
    return {
        "id": d.id,
        "kind": d.kind,
        "chat_id": d.chat_id,
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
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    """Son N saatin dilim ozetlerini uretir (2 saatlik parcalar)."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    now = utc_now()
    start = now - timedelta(hours=hours)
    # Cache'ten donen satirlar eski maliyeti tasir; onbellekteki bir dilim
    # yeniden uretildiginde id'si degismedigi icin "bu istekte harcanan"
    # id karsilastirmasiyla bulunamaz, summarize_segment bildirir.
    fresh: list[Digest] = []
    try:
        made = await summarize_segment(db, start, now, force=force, fresh=fresh)
    except DigestError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

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
    date: str | None = None,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    """Dilim ozetlerinden gunluk brif uretir. date=GG.AA.YYYY veya YYYY-MM-DD.

    Gun, TRT takvim gunu olarak yorumlanir: 00:00-24:00 TRT, UTC'de bir onceki
    gunun 21:00'inden baslar. UTC gunu kullanmak brifi 3 saat kaydiriyordu.
    """
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    if date:
        parsed = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(date, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return JSONResponse(status_code=400,
                                content={"detail": "Tarih GG.AA.YYYY veya YYYY-MM-DD olmali"})
        window = trt_date_window(parsed)
    else:
        window = day_bounds_trt()

    try:
        digest = await summarize_day(db, window=window, force=force)
    except DigestError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    return _serialize(digest)


@router.get("/api/digests")
async def list_digests(
    request: Request,
    kind: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Yetkisiz"})

    stmt = select(Digest).order_by(desc(Digest.window_start), desc(Digest.id)).limit(limit)
    if kind:
        stmt = stmt.where(Digest.kind == kind)
    rows = (await db.execute(stmt)).scalars().all()
    return {"count": len(rows), "items": [_serialize(d) for d in rows]}
