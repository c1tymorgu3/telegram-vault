-- Migration: messages.edited_at
-- Hedef: PostgreSQL. Ayni adim .py kosucusunda da var (SQLite uyumlu).
-- Idempotent: tekrar calistirilabilir.

ALTER TABLE messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP;

-- Mevcut satirlar NULL kalir: duzenleme izi bundan sonraki olaylar icin tutulur.
