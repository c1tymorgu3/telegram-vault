"""Testler .env dosyasi olmadan da calisir.

Panel ve LLM ayarlari import aninda okundugu icin sahte degerler burada,
test modulleri yuklenmeden once tanimlanir. Gercek anahtar gerekmez; testler
LLM cagrisini zaten taklit ediyor.
"""

import os

_DEFAULTS = {
    "DASHBOARD_PASSWORD": "test-password-1234",
    "SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "LLM_PROVIDER": "openai",
    "LLM_API_KEY": "test-key",
    "LLM_MODEL": "test-model",
    "LLM_BASE_URL": "http://127.0.0.1:9/v1",
}

for _key, _value in _DEFAULTS.items():
    os.environ.setdefault(_key, _value)
