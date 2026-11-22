"""Son 6 saate test mesaji ekler (ozet ozelligini denemek icin).

Yalnizca kendi message_id bandindaki (ID_BASE ve ustu) satirlari tazeler; bu
yuzden iki kez calistirmak kopya uretmez, sadece zaman damgalarini yeniler.
Diger kayitlara dokunmaz.
"""

import random
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta

# Kurgusal demo sohbetleri: gercek grup adi/chat_id yok (public repo).
CHATS = [
    ("Demo Grup 1", 1000000000001),
    ("Demo Grup 2", 1000000000002),
]

SENDERS = [
    ("Demo Kullanici", "demo_user"),
    ("Mehmet A.", "mehmet_a"),
    ("Hasan T.", "hasant"),
    ("Elif D.", "elifd"),
    ("Kerem Yildiz", "keremy"),
    ("Selin", "selin_k"),
    ("Group Help", None),
]

LINES = [
    "200 tl iban to kripto lazım tic kabül",
    "İNG banka hesabı olan var mı aktif",
    "usdt adresi değişti dikkat edin",
    "ödeme geldi mi kontrol edin",
    "komisyon oranı ne kadar şu an",
    "kart bilgisi paylaşmayın dolandırıcılık artmış",
    "yeni üye alımı kapalı davet linki geçersiz",
    "moderatör bugün geç gelecek soruları bekletin",
    "panel bakımı 20 dakika sürecek",
    "eSIM market stok güncellendi",
    "deneme sürümü biten lisanslar sıfırlandı",
    "sunucu taşındı gecikme yaşanabilir",
    "kimde çalışan sms servisi var",
    "toplu alımda indirim var mı",
    "sahte admin uyarısı özelden yazanlara itibar etmeyin",
    "arayan var mı hesap açan",
    "hesap patladı alternatif arıyoruz",
    "logları sildim yeni dosya açıyorum",
    "unity webgl build alındı shader 4 saniye",
    "türkçe karakter sorunu düzeldi",
]


# seed_demo.py ile ayni crc32 formulu: ayni kisi iki betikte de ayni user_id.
# hash() kullanilmaz: PYTHONHASHSEED yuzunden her calistirmada degisir.
ID_BASE = 1000000000
BAND_SIZE = 100000
MESSAGES_PER_CHAT = 90


TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


def demo_user_id(name: str) -> int:
    return 100000000 + (zlib.crc32(name.encode("utf-8")) % 900000000)


def main():
    conn = sqlite3.connect("telegramlogs.db")
    # Bu betigin yazdigi bandi temizle: tekrar calistirmak kopya biriktirmez.
    conn.execute(
        "delete from messages where message_id >= ? and message_id < ?",
        (ID_BASE, ID_BASE + BAND_SIZE),
    )

    # DB naive UTC tutuyor (bot.py ile ayni bicim). sqlite3'un datetime adaptoru
    # Python 3.12'de deprecated; degerleri metin olarak yaziyoruz.
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = []
    for chat_index, (chat_title, chat_id) in enumerate(CHATS):
        for i in range(MESSAGES_PER_CHAT):
            ts = now - timedelta(minutes=random.randint(1, 350))
            name, username = random.choice(SENDERS)
            rows.append((
                ID_BASE + chat_index * 1000 + i, chat_id, chat_title,
                demo_user_id(name), username, name, random.choice(LINES),
                ts.strftime(TS_FMT),
                1 if random.random() < 0.15 else 0,
            ))
    rows.sort(key=lambda r: r[7])
    conn.executemany(
        "insert into messages (message_id, chat_id, chat_title, user_id, username,"
        " sender_name, content, timestamp, is_deleted) values (?,?,?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    total = conn.execute("select count(*) from messages").fetchone()[0]
    recent = conn.execute(
        "select count(*) from messages where timestamp > ?",
        ((now - timedelta(hours=6)).strftime(TS_FMT),)).fetchone()[0]
    print(f"eklendi {len(rows)} | toplam {total} | son 6 saat {recent}")
    conn.close()


if __name__ == "__main__":
    main()
