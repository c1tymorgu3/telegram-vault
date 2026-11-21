"""Son 6 saate test mesaji ekler (ozet ozelligini denemek icin).

Mevcut kayitlara dokunmaz; yalnizca ekler.
"""

import random
import sqlite3
from datetime import datetime, timedelta

CHATS = [
    ("MSwap • Ticaret", -1004384814100),
    ("SPYHACKERZ CHAT & Ticaret ~ SociaL Media", -1002220378800),
]

SENDERS = [("Layn", "layn_x", 110001), ("Mehmet A.", "mehmet_a", 110002),
           ("Hasan T.", "hasant", 110003), ("Elif D.", "elifd", 110004),
           ("Kerem Yildiz", "keremy", 110005), ("Selin", "selin_k", 110006),
           ("Group Help", None, 110007)]

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


def main():
    conn = sqlite3.connect("telegramlogs.db")
    now = datetime.utcnow()
    rows = []
    for chat_title, chat_id in CHATS:
        for i in range(90):
            ts = now - timedelta(minutes=random.randint(1, 350))
            name, username, uid = random.choice(SENDERS)
            rows.append((
                random.randint(10**5, 10**7), chat_id, chat_title, uid,
                username, name, random.choice(LINES), ts,
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
        (now - timedelta(hours=6),)).fetchone()[0]
    print(f"eklendi {len(rows)} | toplam {total} | son 6 saat {recent}")
    conn.close()


if __name__ == "__main__":
    main()
