"""Migration: digests.chat_id + (chat_id, message_id) unique index.

Faz 3.13 / ozet duzeltmeleri:
- digests.chat_id: ozetler baslikla degil sohbet kimligiyle gruplanacak.
  Mevcut satirlar NULL kalabilir; kod bunlari baslikla eslemeye devam eder.
- messages (chat_id, message_id) unique index: backfill ile canli dinleyici
  yarisinca cift kayit olusuyordu. Once dupe temizligi, sonra index.

Postgres hedef; SQLite'ta da bozulmaz (SQLite ADD COLUMN IF NOT EXISTS
desteklemez, kolon PRAGMA ile kontrol edilir).

Kullanim:  ./.venv/Scripts/python.exe migrations/2026_11_22_digest_chat_id.py [--dry-run]
"""

import asyncio
import pathlib
import sys

from sqlalchemy import text

# Script migrations/ altindan calisiyor; repo kokunu sys.path'e ekle.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from database import engine

# SQLite "ADD COLUMN IF NOT EXISTS" desteklemez; orada kolon varligi PRAGMA ile bakilir.
DIGEST_COLUMN_PG = "ALTER TABLE digests ADD COLUMN IF NOT EXISTS chat_id BIGINT"
DIGEST_COLUMN = "ALTER TABLE digests ADD COLUMN chat_id BIGINT"
DIGEST_INDEX = ("CREATE INDEX IF NOT EXISTS ix_digests_chat_window "
                "ON digests (chat_id, window_start)")
# Postgres: her (chat_id, message_id) icin en kucuk id kalir.
DEDUPE_PG = ("DELETE FROM messages a USING messages b "
             "WHERE a.id > b.id AND a.chat_id = b.chat_id AND a.message_id = b.message_id")
# SQLite'ta DELETE ... USING yok; ayni is MIN(id) ile yapilir.
DEDUPE_SQLITE = ("DELETE FROM messages WHERE id NOT IN "
                 "(SELECT MIN(id) FROM messages GROUP BY chat_id, message_id)")
UNIQUE_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_chat_message "
                "ON messages (chat_id, message_id)")


async def _has_column(conn, table: str, column: str, dialect: str) -> bool:
    if dialect == "sqlite":
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
        return any(r[1] == column for r in rows)
    # table_schema filtresi sart: ayni adli tablo baska bir semada da olabilir,
    # yanlis pozitif ALTER'i atlar ve ardindan index olusturma patlar.
    row = (await conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})).first()
    return row is not None


async def _count_dupes(conn) -> int:
    row = (await conn.execute(text(
        "SELECT COALESCE(SUM(n - 1), 0) FROM ("
        "  SELECT COUNT(*) AS n FROM messages GROUP BY chat_id, message_id HAVING COUNT(*) > 1"
        ") d"
    ))).scalar()
    return int(row or 0)


async def main(dry_run: bool = False) -> None:
    dialect = engine.dialect.name
    async with engine.begin() as conn:
        steps: list[tuple[str, str]] = []

        if not await _has_column(conn, "digests", "chat_id", dialect):
            steps.append(("digests.chat_id",
                          DIGEST_COLUMN if dialect == "sqlite" else DIGEST_COLUMN_PG))
        steps.append(("ix_digests_chat_window", DIGEST_INDEX))

        dupes = await _count_dupes(conn)
        print(f"Cift mesaj satiri (silinecek): {dupes}")
        steps.append(("messages dupe temizligi",
                      DEDUPE_SQLITE if dialect == "sqlite" else DEDUPE_PG))
        steps.append(("uq_messages_chat_message", UNIQUE_INDEX))

        for label, sql in steps:
            if dry_run:
                print(f"[dry-run] {label}: {sql}")
                continue
            await conn.execute(text(sql))
            print(f"uygulandi: {label}")

    await engine.dispose()
    if not dry_run:
        print("Migration tamam.")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
