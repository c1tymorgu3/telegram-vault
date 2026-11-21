"""Her gun 00:00 TRT'de calisan gunluk ozet uretici.

Sunucu saati UTC; gun sinirlari Turkiye saatine gore (UTC+3) hesaplanir:
  TRT 00:00 = UTC 21:00 (bir onceki gun)
Yani bu betik 21:00 UTC'de calisir ve az once biten TRT gununu ozetler.

Kullanim:
  python daily_digest.py            # dun (TRT)
  python daily_digest.py 2026-11-20 # belirli gun
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from database import SessionLocal, init_db
from digest import summarize_day, summarize_segment

TRT = timezone(timedelta(hours=3))


def trt_day_bounds(day: datetime | None = None) -> tuple[datetime, datetime]:
    """TRT gununun UTC cinsinden [baslangic, bitis) araligi."""
    # Cron 00:00 TRT'de calisir; o anda "bugun" yeni gun oldugu icin
    # bir dakika geriye bakip az once biten gunu hedefliyoruz.
    now_trt = (day or (datetime.now(TRT) - timedelta(minutes=1))).astimezone(TRT)
    start_trt = now_trt.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_trt.astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(days=1)


async def run(day: datetime | None = None) -> None:
    await init_db()
    async with SessionLocal() as db:
        start, end = trt_day_bounds(day)
        segments = await summarize_segment(db, start, end)
        digest = await summarize_day(db, window=(start, end))

        payload = digest.payload or {}
        print(f"[{datetime.utcnow():%Y-%m-%d %H:%M}Z] gunluk ozet hazir")
        print(f"  aralik   : {start:%d.%m %H:%M} - {end:%d.%m %H:%M} UTC")
        print(f"  dilim    : {len(segments)} | mesaj: {digest.msg_count}")
        print(f"  token    : {digest.tokens_in}+{digest.tokens_out} | maliyet: ${digest.cost:.4f}")
        print(f"  baslik   : {payload.get('headline', '-')}")


if __name__ == "__main__":
    target = None
    if len(sys.argv) > 1:
        target = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=TRT)
    asyncio.run(run(target))
