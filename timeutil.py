"""TRT (UTC+3) zaman yardimcilari.

Veritabani naive UTC tutuyor (bot.py: `msg.date.astimezone(utc).replace(tzinfo=None)`).
Panel ve gunluk brifler ise Turkiye takvim gunune gore konusuyor. +3 kaydirmasi
koda dagilmisti (her yerde `timedelta(hours=3)`); tek yer burasi:
naive UTC <-> naive TRT cevrimi ve TRT gun sinirlari.

Kural: fonksiyonlar naive datetime alip naive datetime dondurur; aware verilirse
once UTC'ye (veya TRT'ye) cekilir.
"""

from datetime import UTC, datetime, timedelta, timezone

TRT_OFFSET = timedelta(hours=3)
TRT = timezone(TRT_OFFSET)


def utc_now() -> datetime:
    """Simdiki an, naive UTC (DB ile ayni bicim)."""
    return datetime.now(UTC).replace(tzinfo=None)


def trt_now() -> datetime:
    """Simdiki an, naive TRT."""
    return to_trt(utc_now())


def to_trt(dt: datetime) -> datetime:
    """naive UTC -> naive TRT."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt + TRT_OFFSET


def from_trt(dt: datetime) -> datetime:
    """naive TRT -> naive UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(TRT).replace(tzinfo=None)
    return dt - TRT_OFFSET


def day_bounds_trt(dt: datetime | None = None) -> tuple[datetime, datetime]:
    """Verilen anin TRT takvim gununun UTC [baslangic, bitis) sinirlari.

    dt naive UTC kabul edilir; verilmezse simdi. 21:00 UTC bir sonraki TRT
    gununun 00:00'idir, yani TRT gunu UTC'de bir onceki gunun 21:00'inde baslar.
    """
    start_trt = to_trt(dt or utc_now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return from_trt(start_trt), from_trt(start_trt + timedelta(days=1))


def trt_date_window(day: datetime) -> tuple[datetime, datetime]:
    """Bir TAKVIM tarihini (orn. 2026-11-21) TRT gunu kabul edip UTC araligi verir.

    Naif deger TRT takvim tarihi sayilir. Aware deger once TRT'ye cevrilir:
    aksi halde `datetime(..., tzinfo=utc)` gibi bir an, UTC takvim gunu olarak
    yorumlanip pencereyi 3 saat kaydiriyordu.
    """
    if day.tzinfo is not None:
        day = day.astimezone(TRT).replace(tzinfo=None)
    start_trt = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return from_trt(start_trt), from_trt(start_trt + timedelta(days=1))
