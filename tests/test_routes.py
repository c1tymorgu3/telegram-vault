"""Güvenlik düzeltmelerinin regresyon testleri.

Kapsam: CSV formül enjeksiyonu, LIKE kaçışı, login_attempts temizliği,
sabit zamanlı token/şifre karşılaştırması, /internal/broadcast loopback kapısı.

Testler saf fonksiyonlara ve bellek içi SQLite'a dayanır; gerçek arşiv DB'sine
yazılmaz, sunucu başlatılmaz.
"""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routes import auth as auth_mod
from routes import messages as messages_mod
from routes import websocket as ws_mod

# ---------------------------------------------------------------- CSV enjeksiyonu

@pytest.mark.parametrize("raw, expected", [
    ("=1+1", "'=1+1"),
    ("+90 555", "'+90 555"),
    ("-3", "'-3"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("\tkomut", "'\tkomut"),
    ("\rkomut", "'\rkomut"),
    ("normal mesaj", "normal mesaj"),
    ("100% zam", "100% zam"),
])
def test_csv_safe_blocks_formulas(raw, expected):
    assert messages_mod.csv_safe(raw) == expected


def test_csv_safe_numeric_and_none():
    # Sayısal alanlar da aynı kontrolden geçer: int -> str, None -> boş hücre.
    assert messages_mod.csv_safe(12345) == "12345"
    assert messages_mod.csv_safe(-42) == "'-42"
    assert messages_mod.csv_safe(None) == ""


# ------------------------------------------------------------------- LIKE kaçışı

def test_escape_like_escapes_wildcards():
    assert messages_mod.escape_like("100%") == "100\\%"
    assert messages_mod.escape_like("a_b") == "a\\_b"
    # Ters bölü önce kaçırılmalı, yoksa sonradan eklenen kaçışlar bozulur.
    assert messages_mod.escape_like("c:\\x") == "c:\\\\x"
    assert messages_mod.like_pattern("100%") == "%100\\%%"


def test_like_escape_matches_nothing_for_percent_query():
    """%100%% kalıbı her satırı döndürüyordu; ESCAPE ile tam alt dize aranır."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (content TEXT)")
    con.executemany(
        "INSERT INTO t (content) VALUES (?)",
        [("indirim 100% zam",), ("tamamen alakasiz",), ("1000 lira",)],
    )
    pattern = messages_mod.like_pattern("100%")
    rows = con.execute(
        "SELECT content FROM t WHERE content LIKE ? ESCAPE '\\'", (pattern,)
    ).fetchall()
    con.close()
    assert rows == [("indirim 100% zam",)]


# --------------------------------------------------------------- login_attempts

def test_prune_login_attempts_drops_stale_entries(monkeypatch):
    monkeypatch.setattr(auth_mod, "login_attempts", auth_mod.defaultdict(list))
    now = time.time()
    auth_mod.login_attempts["1.1.1.1"] = [now - 5000]
    auth_mod.login_attempts["2.2.2.2"] = [now - 10]

    auth_mod.prune_login_attempts(now)

    assert "1.1.1.1" not in auth_mod.login_attempts
    assert auth_mod.login_attempts["2.2.2.2"] == [now - 10]


def test_prune_login_attempts_caps_dictionary_size(monkeypatch):
    monkeypatch.setattr(auth_mod, "login_attempts", auth_mod.defaultdict(list))
    monkeypatch.setattr(auth_mod, "LOGIN_ATTEMPTS_MAX_IPS", 10)
    now = time.time()
    for i in range(25):
        auth_mod.login_attempts[f"10.0.0.{i}"] = [now - i]

    auth_mod.prune_login_attempts(now)

    assert len(auth_mod.login_attempts) == 10
    # En taze kayıtlar hayatta kalır.
    assert "10.0.0.0" in auth_mod.login_attempts


def test_rate_limit_window_and_reset(monkeypatch):
    monkeypatch.setattr(auth_mod, "login_attempts", auth_mod.defaultdict(list))
    ip = "3.3.3.3"
    for _ in range(auth_mod.RATE_LIMIT_MAX):
        auth_mod.record_attempt(ip)
    assert auth_mod.is_rate_limited(ip) is True

    # Başarılı girişte o IP'nin kaydı sıfırlanır (auth.py'deki pop ile aynı etki).
    auth_mod.login_attempts.pop(ip, None)
    assert auth_mod.is_rate_limited(ip) is False


# --------------------------------------------------- sabit zamanlı karşılaştırma

def test_token_matches_constant_time_semantics():
    assert ws_mod.token_matches("abc", "abc") is True
    assert ws_mod.token_matches("abc", "abd") is False
    assert ws_mod.token_matches("abc", "abcd") is False
    # Anahtar tanımlı değilse yalnızca loopback kontrolü uygulanır.
    assert ws_mod.token_matches("", "") is True


def test_internal_token_reads_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN", "s3cret-token")
    assert ws_mod.internal_token() == "s3cret-token"


def test_is_loopback():
    assert ws_mod.is_loopback("127.0.0.1") is True
    assert ws_mod.is_loopback("::1") is True
    assert ws_mod.is_loopback("::ffff:127.0.0.1") is True
    assert ws_mod.is_loopback("10.0.0.5") is False
    assert ws_mod.is_loopback("testclient") is False
    assert ws_mod.is_loopback(None) is False


# --------------------------------------------------- /internal/broadcast uçnoktası

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ws_mod.router)
    return TestClient(app)


def test_broadcast_rejects_non_loopback_client():
    # TestClient kaynağı "testclient" olur; loopback olmadığı için 403 beklenir.
    with _client() as client:
        response = client.post("/internal/broadcast", json={"type": "fake"})
    assert response.status_code == 403


def test_broadcast_requires_token_header_from_loopback(monkeypatch):
    # Loopback kapısını geçip header kontrolünü ayrı doğrula.
    monkeypatch.setattr(ws_mod, "is_loopback", lambda host: True)
    monkeypatch.setenv("INTERNAL_TOKEN", "tok-123")

    with _client() as client:
        missing = client.post("/internal/broadcast", json={"type": "fake"})
        wrong = client.post(
            "/internal/broadcast", json={"type": "fake"},
            headers={"X-Internal-Token": "yanlis"},
        )
        ok = client.post(
            "/internal/broadcast", json={"type": "real"},
            headers={"X-Internal-Token": "tok-123"},
        )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert ok.status_code == 200
    assert ok.json() == {"status": "ok"}
