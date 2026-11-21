"""Demo verisi: yerel paneli Telegram session'i olmadan gostermek icin.

Gercek arsiv artik VPS'te (Postgres). Bu betik sadece yerel SQLite dosyasina
gercekci gorunen test verisi yazar; .session dosyasi yok, bot calismaz.
"""

import random
import sqlite3
from datetime import datetime, timedelta

CHATS = [
    ("MSwap • Ticaret", -1004384814100, "group", 2541),
    ("SPYHACKERZ CHAT & Ticaret ~ SociaL Media", -1002220378800, "group", 5528),
    ("SPYHACKERZ OLD GROUP 7/24", -1003229781784, "group", 11787),
    ("Kripto Sinyal VIP", -1003111222333, "group", 1893),
    ("MSwap Teminat Duyuru", -1003438484590, "channel", 3354),
    ("MSwap • Login", -1003553659114, "channel", 3445),
    ("Free Unity Assets", -1002062286339, "channel", 498),
]

SENDERS = [
    ("Layn", "layn_x"), ("Mehmet A.", "mehmet_a"), ("Hasan T.", "hasant"),
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


def main():
    conn = sqlite3.connect("telegramlogs.db")
    conn.execute("delete from messages")
    conn.execute("delete from chats")
    conn.commit()

    now = datetime.utcnow()
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
            user_id = 100000000 + (abs(hash(name)) % 900000000)
            rows.append((
                random.randint(10**5, 10**7), chat_id, chat_title,
                user_id, username, name,
                random.choice(LINES), chat_start,
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
          max((r[7] for r in rows if r[1] == cid), default=None), now)
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
