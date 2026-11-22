-- Migration: digests.chat_id + (chat_id, message_id) unique index
-- Hedef: PostgreSQL. Bkz. ayni klasordeki .py kosucusu (SQLite uyumlu surum).
-- Idempotent: tekrar calistirilabilir.

-- 1) Ozetler artik baslikla degil sohbet kimligiyle gruplanir.
ALTER TABLE digests ADD COLUMN IF NOT EXISTS chat_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_digests_chat_window ON digests (chat_id, window_start);

-- 2) (chat_id, message_id) dogal anahtar: once dupe temizligi, sonra unique index.
DELETE FROM messages a USING messages b
 WHERE a.id > b.id AND a.chat_id = b.chat_id AND a.message_id = b.message_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_chat_message
    ON messages (chat_id, message_id);

-- Not: mevcut digests satirlarinda chat_id NULL kalir; kod bunlari chat_title
-- ile eslemeye devam eder ve satir yeniden uretildiginde chat_id kendiliginden
-- dolar. chat_title benzersiz olmadigi icin baslikla backfill YAPMAYIN:
-- yanlis sohbete baglanan ozet, ayni basligi tasiyan diger sohbetin onbellegi
-- gibi doner. Gercek chat_id yalnizca satir yeniden uretilirken yazilir.
