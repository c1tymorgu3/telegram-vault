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
from datetime import datetime, timedelta

from database import SessionLocal, init_db
from digest import summarize_day, summarize_segment
from timeutil import TRT, day_bounds_trt, trt_date_window, utc_now


def trt_day_bounds(day: datetime | None = None) -> tuple[datetime, datetime]:
    """TRT gununun UTC cinsinden [baslangic, bitis) araligi.

    day verilirse o TRT takvim gunu kullanilir. Verilmezse cron 00:00 TRT'de
    calistigi icin bir dakika geriye bakilir ve az once biten gun hedeflenir.
    """
    if day is not None:
        return trt_date_window(day)
    return day_bounds_trt(utc_now() - timedelta(minutes=1))


async def run(day: datetime | None = None) -> None:
    await init_db()
    async with SessionLocal() as db:
        start, end = trt_day_bounds(day)
        segments = await summarize_segment(db, start, end)
        digest = await summarize_day(db, window=(start, end))

        payload = digest.payload or {}
        print(f"[{utc_now():%Y-%m-%d %H:%M}Z] gunluk ozet hazir")
        print(f"  aralik   : {start:%d.%m %H:%M} - {end:%d.%m %H:%M} UTC")
        print(f"  dilim    : {len(segments)} | mesaj: {digest.msg_count}")
        print(f"  token    : {digest.tokens_in}+{digest.tokens_out} | maliyet: ${digest.cost:.4f}")
        print(f"  baslik   : {payload.get('headline', '-')}")


if __name__ == "__main__":
    target = None
    if len(sys.argv) > 1:
        target = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=TRT)
    asyncio.run(run(target))
