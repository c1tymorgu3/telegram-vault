"""Demo verisi: yerel paneli Telegram session'i olmadan gostermek icin.

Gercek arsiv artik VPS'te (Postgres). Bu betik sadece yerel SQLite dosyasina
gercekci gorunen test verisi yazar; .session dosyasi yok, bot calismaz.
"""

import random
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta

# Kurgusal demo sohbetleri: gercek grup/kanal adi ve chat_id icermez, cunku
# bu dosya public repoda duruyor. ID'ler 1000000000001 araligindan uydurma.
CHATS = [
    ("Demo Grup 1", 1000000000001, "group", 2541),
    ("Demo Grup 2", 1000000000002, "group", 5528),
    ("Demo Grup 3", 1000000000003, "group", 11787),
    ("Demo Grup 4", 1000000000004, "group", 1893),
    ("Demo Kanal 1", 1000000000005, "channel", 3354),
    ("Demo Kanal 2", 1000000000006, "channel", 3445),
    ("Demo Kanal 3", 1000000000007, "channel", 498),
]

SENDERS = [
    ("Demo Kullanici", "demo_user"), ("Mehmet A.", "mehmet_a"), ("Hasan T.", "hasant"),
    ("Elif D.", "elifd"), ("Kerem Yildiz", "keremy"), ("Group Help", None),
    ("Baran", "baran35"), ("Selin", "selin_k"), ("Emre", "emredev"),
    ("Tolga", "tolga_tt"), ("Deniz", "denizfx"), ("Zeynep", "zeynep42"),
    ("Okan", "okanh"), ("Merve", "merve_x"), ("Yusuf", "yusuf_k"),
]

LINES = [
    "200 tl iban to kripto lazım tic kabül",
    "İNG✅ DENİZ✅ YAPIKREDİ ✅ ODEABANK ✅ hesaplar aktif",
    "usdt adresi değişti, dikkat edin",
    "ödeme geldi mi kontrol edin",
    "ticaret komutunu kullanmayanlar uyarıldı",
    "küfür etme bana öyle dediler büyük harf kullanma",
    "logları sildim, yeni dosya açıyorum",
    "hesap patladı, alternatif arıyoruz",
    "fiyat 450 TL, toplu alımda indirim var",
    "moderatör bugün geç gelecek, soruları bekletin",
    "panel bakımı 20 dakika sürecek",
    "yeni üye alımı kapalı, davet linki geçersiz",
    "komisyon oranları güncellendi",
    "eski mesajları arşivden çıkardım",
    "kanıt ekran görüntüsü isteyenler özelden yazsın",
    "sahte admin uyarısı: özelden yazanlara itibar etmeyin",
    "kart bilgisi paylaşmayın, dolandırıcılık artmış",
    "deneme sürümü biten lisanslar sıfırlandı",
    "sunucu taşındı, gecikme yaşanabilir",
    "eSIM market stok güncellendi",
    "shader derlemesi 4 saniyeye indi",
    "unity 2022.3 webgl build alındı",
    "arazi sahnesi için doku atlasi hazır",
    "türkçe karakter sorunu düzeldi",
    "gece yarısı bakım penceresi",
    "/ticaret",
    "onay bekliyor, 5 dakika",
    "sistem çevrimiçi",
]


TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def main():
    conn = sqlite3.connect("telegramlogs.db")
    conn.execute("delete from messages")
    conn.execute("delete from chats")
    # Mesajlar silinince profiller ve brifler yetim kaliyor; onlari da temizle
    # (tablo yoksa eski bir DB'de betik patlamasin).
    if _has_table(conn, "profiles"):
        conn.execute("delete from profiles")
    if _has_table(conn, "digests"):
        conn.execute("delete from digests")
    conn.commit()

    # DB naive UTC tutuyor (bot.py ile ayni bicim). sqlite3'un datetime adaptoru
    # Python 3.12'de deprecated; degerleri metin olarak yaziyoruz.
    now = datetime.now(UTC).replace(tzinfo=None)
    now_s = now.strftime(TS_FMT)
    start = now - timedelta(days=60)
    rows = []
    for chat_title, chat_id, _, _ in CHATS:
        chat_start = start + timedelta(days=random.randint(0, 20))
        count = random.randint(60, 130)
        for _ in range(count):
            chat_start += timedelta(minutes=random.randint(40, 900))
            if chat_start > now:
                break
            name, username = random.choice(SENDERS)
            # Ayni kisi her mesajda ayni user_id: profil paneli anlamli olsun.
            # crc32 deterministik; hash() ise PYTHONHASHSEED yuzunden her
            # calistirmada farkli deger verir (ayni kisi her seferinde baska ID).
            user_id = 100000000 + (zlib.crc32(name.encode("utf-8")) % 900000000)
            rows.append((
                random.randint(10**5, 10**7), chat_id, chat_title,
                user_id, username, name,
                random.choice(LINES), chat_start.strftime(TS_FMT),
                1 if random.random() < 0.18 else 0,
            ))

    rows.sort(key=lambda r: r[7])
    conn.executemany(
        """insert into messages
           (message_id, chat_id, chat_title, user_id, username, sender_name,
            content, timestamp, is_deleted)
           values (?,?,?,?,?,?,?,?,?)""", rows,
    )
    conn.executemany(
        """insert into chats
           (chat_id, title, username, chat_type, member_count, last_message_at, synced_at)
           values (?,?,?,?,?,?,?)""",
        [(cid, title, None, ctype, members,
          max((r[7] for r in rows if r[1] == cid), default=None), now_s)
         for title, cid, ctype, members in CHATS],
    )
    conn.commit()
    total = conn.execute("select count(*) from messages").fetchone()[0]
    oldest = conn.execute("select min(timestamp) from messages").fetchone()[0]
    deleted = conn.execute("select count(*) from messages where is_deleted=1").fetchone()[0]
    print(f"{total} mesaj, {deleted} silinmis, en eski {oldest}, 7 sohbet")
    conn.close()


if __name__ == "__main__":
    main()
