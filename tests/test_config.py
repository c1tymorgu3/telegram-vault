"""config.validate_security_settings() icin birim testleri.

app.py import EDILMEZ: testler sadece config modulunu yukler, boylece
FastAPI/DB/Telegram tarafi (ve .env'deki gercek degerler) devreye girmez.
Tum cagrilar argumanlari acikca verir; ortam degiskenine bagimlilik yok.
"""

import sys
from pathlib import Path

import pytest

# pytest, test dosyasinin kokunu (tests/) sys.path'e ekler; repo kokunu ekle.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # sys.path ayarindan sonra import edilmeli

STRONG_PASSWORD = "correct-horse-batt"  # 17 karakter, uydurma -> gecmeli
STRONG_SECRET = "b7f1c0d2a94e4f8d9a1c3e5f7b9d1a3c5e7f9b1d3a5c7e9f"


def test_varsayilan_sifre_reddedilir():
    with pytest.raises(RuntimeError) as excinfo:
        config.validate_security_settings("admin", STRONG_SECRET, allow_insecure=False)
    assert "DASHBOARD_PASSWORD" in str(excinfo.value)


@pytest.mark.parametrize("weak", ["", "   ", "admin", "ADMIN", "password", "changeme", "test"])
def test_bos_ve_zayif_sifre_reddedilir(weak):
    with pytest.raises(RuntimeError):
        config.validate_security_settings(weak, STRONG_SECRET, allow_insecure=False)


def test_kisa_sifre_reddedilir():
    with pytest.raises(RuntimeError) as excinfo:
        config.validate_security_settings("kisa-sifre1", STRONG_SECRET, allow_insecure=False)
    assert "kısa" in str(excinfo.value).lower() or "kisa" in str(excinfo.value).lower()


def test_varsayilan_secret_reddedilir():
    with pytest.raises(RuntimeError) as excinfo:
        config.validate_security_settings(STRONG_PASSWORD, config.DEFAULT_SESSION_SECRET,
                                          allow_insecure=False)
    assert "SESSION_SECRET" in str(excinfo.value)


@pytest.mark.parametrize(
    "secret", ["", "   ", "kisa-secret", "x" * 31, config.DEFAULT_SESSION_SECRET]
)
def test_zayif_secret_reddedilir(secret):
    with pytest.raises(RuntimeError):
        config.validate_security_settings(STRONG_PASSWORD, secret, allow_insecure=False)


def test_guclu_degerlerle_gecer():
    assert config.validate_security_settings(STRONG_PASSWORD, STRONG_SECRET,
                                             allow_insecure=False) == []


def test_16_karakterlik_sifre_gecer():
    # Uretimdeki sifre 16 karakter: minimum sinir bunu engellememeli.
    assert config.validate_security_settings("Solar-Panel-2026", STRONG_SECRET,
                                             allow_insecure=False) == []


def test_minimum_sinirlar_korunuyor():
    # 12 karakter sifre ve 32 karakter secret sinirda gecer.
    assert config.validate_security_settings("a" * config.MIN_PASSWORD_LENGTH,
                                             "c" * config.MIN_SECRET_LENGTH,
                                             allow_insecure=False) == []


def test_allow_insecure_defaults_sadece_uyarir(caplog):
    with caplog.at_level("WARNING"):
        problems = config.validate_security_settings(
            "admin", config.DEFAULT_SESSION_SECRET, allow_insecure=True
        )
    assert problems, "varlayilan degerlerle sorun listesi bos olmamali"
    assert any("ALLOW_INSECURE_DEFAULTS" in rec.message for rec in caplog.records)


def test_allow_insecure_temiz_yapilandirmada_sorun_dondurmez():
    assert config.validate_security_settings(STRONG_PASSWORD, STRONG_SECRET,
                                             allow_insecure=True) == []


def test_internal_token_modul_degiskeni_var():
    # routes/websocket.py bu degeri getattr ile okur; tanimsizsa loopback
    # kontrolune dusulur. Degisken her zaman str olmali (bos = tanimsiz).
    assert isinstance(config.INTERNAL_TOKEN, str)


def test_modul_varsayilanlari_env_ile_tutarli():
    # Modul seviyesindeki degerler .env'den gelir; ama dogrulama argumanlari
    # verilmeden cagrildiginda da tip olarak str olmali (auth.py karsilastirmasi).
    assert isinstance(config.DASHBOARD_PASSWORD, str)
    assert isinstance(config.SESSION_SECRET, str)
