from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import (BigInteger, String, Text, DateTime, Boolean, Integer,
                        Float, JSON, event)
from datetime import datetime
import config
import asyncio

engine = create_async_engine(config.DATABASE_URL, echo=False)


# The panel and the listener are two processes on one SQLite file. In the
# default rollback-journal mode a read transaction blocks every write, and
# because busy_timeout simply waits, the cost shows up as silent multi-second
# stalls (measured: update lag growing 2.7s → 22.7s) with no "database is
# locked" in the log. WAL lets readers and the writer run at the same time.
@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    if "sqlite" not in config.DATABASE_URL:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")
    finally:
        cursor.close()

SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


class Message(Base):
    __tablename__ = 'messages'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    chat_title: Mapped[str] = mapped_column(String(255), nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=True)
    username: Mapped[str] = mapped_column(String(255), nullable=True, index=True)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    is_deleted: Mapped[bool] = mapped_column(default=False)


class Chat(Base):
    """Chats the account can see, recorded from one bounded get_dialogs() read.

    The messages table only ever knows about chats that spoke while the
    listener was up. This table is what lets the panel list the rest, with the
    member count and the last activity seen on the client side.
    """
    __tablename__ = 'chats'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=True)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    chat_type: Mapped[str] = mapped_column(String(32), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=True)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class Digest(Base):
    """Zaman penceresi ozetleri: 2 saatlik dilimler ve gunluk brif.

    payload alaninda modelin urettigi yapisal JSON duruyor; kanit olarak
    mesaj id'leri tasidigi icin her iddia arsivde dogrulanabilir.
    """
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)          # segment | daily
    chat_title: Mapped[str] = mapped_column(String(255), nullable=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    msg_count: Mapped[int] = mapped_column(default=0)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    model: Mapped[str] = mapped_column(String(64), nullable=True)
    prompt_v: Mapped[int] = mapped_column(default=1)
    evidence_ok: Mapped[int] = mapped_column(default=0)
    evidence_total: Mapped[int] = mapped_column(default=0)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.utcnow())


class Profile(Base):
    """Bir Telegram kimliginin LLM ile cikarilmis profili.

    Sadece operator istedigi zaman uretilir; payload alani sektor gecmisi,
    kendisi hakkinda verdigi bilgiler ve davranis desenlerini tasir.
    """
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=True)
    msg_count: Mapped[int] = mapped_column(default=0)
    chat_count: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    model: Mapped[str] = mapped_column(String(64), nullable=True)
    prompt_v: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.utcnow())


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    asyncio.run(init_db())
    print("Veritabanı hazır.")
