"""Sağlayıcıdan bağımsız LLM istemcisi.

Tek bir yerden yönetilir; hangi sağlayıcının kullanılacağı .env'den gelir:

    LLM_PROVIDER=opencode | openrouter | openai | deepseek | groq | custom
    LLM_API_KEY=...
    LLM_MODEL=deepseek-v4.1-flash
    LLM_BASE_URL=...          # custom için zorunlu, diğerlerinde override
    LLM_SESSION_ID=...        # opencode-go oturum başlığı
    LLM_TIMEOUT=240

Eski isimler (DIGEST_API_KEY / DIGEST_MODEL / DIGEST_BASE_URL) geriye dönük
olarak desteklenir; LLM_* tanımlıysa onlar kazanır.
"""

import json
import os
import re
from typing import Any

import httpx

PROVIDERS: dict[str, dict[str, Any]] = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "headers": {"User-Agent": "TelegramVault/1.0"},
        "needs_session": True,          # x-opencode-session zorunlu
        "json_mode": True,
        "thinking_off": True,           # reasoning modeli butceyi yiyor
        "model": "deepseek-v4.1-flash",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "headers": {
            "HTTP-Referer": "https://c1tymorgu3.com",
            "X-Title": "Telegram Vault",
        },
        "json_mode": True,
        "thinking_off": False,
        "model": "deepseek/deepseek-chat",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "json_mode": True,
        "thinking_off": False,
        "model": "gpt-4o-mini",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "json_mode": True,
        "thinking_off": False,
        "model": "deepseek-chat",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "json_mode": True,
        "thinking_off": False,
        "model": "llama-3.3-70b-versatile",
    },
    "custom": {
        "base_url": "",
        "json_mode": True,
        "thinking_off": False,
        "model": "",
    },
}


class LLMError(RuntimeError):
    pass


def config() -> dict[str, Any]:
    """Aktif sağlayıcı ayarlarını .env'den okur."""
    name = (os.getenv("LLM_PROVIDER") or "opencode").strip().lower()
    if name not in PROVIDERS:
        raise LLMError(
            f"Bilinmeyen LLM_PROVIDER '{name}'. Secenekler: {', '.join(PROVIDERS)}"
        )
    preset = PROVIDERS[name]

    api_key = (os.getenv("LLM_API_KEY") or os.getenv("DIGEST_API_KEY") or "").strip()
    model = (os.getenv("LLM_MODEL") or os.getenv("DIGEST_MODEL") or preset.get("model") or "").strip()
    base_url = (os.getenv("LLM_BASE_URL") or os.getenv("DIGEST_BASE_URL")
                or preset.get("base_url") or "").strip().rstrip("/")
    thinking = os.getenv("LLM_THINKING")
    timeout = float(os.getenv("LLM_TIMEOUT") or 240)

    extra: dict[str, str] = dict(preset.get("headers") or {})
    raw_extra = os.getenv("LLM_EXTRA_HEADERS") or ""
    if raw_extra:
        try:
            extra.update({str(k): str(v) for k, v in json.loads(raw_extra).items()})
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM_EXTRA_HEADERS gecerli JSON degil: {exc}") from exc

    if preset.get("needs_session") and not os.getenv("LLM_SESSION_ID"):
        extra["x-opencode-session"] = "vault-digest"
    elif os.getenv("LLM_SESSION_ID"):
        extra["x-opencode-session"] = os.getenv("LLM_SESSION_ID", "")

    if not api_key:
        raise LLMError("LLM_API_KEY tanimli degil (.env)")
    if not base_url:
        raise LLMError(f"'{name}' icin LLM_BASE_URL zorunlu")
    if not model:
        raise LLMError(f"'{name}' icin LLM_MODEL zorunlu")

    return {
        "provider": name,
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "headers": extra,
        "json_mode": preset.get("json_mode", True),
        "thinking_off": preset.get("thinking_off", False) if thinking != "off" else True,
        "timeout": timeout,
    }


def model_name() -> str:
    """Kayıt satırlarına yazılacak model etiketi: saglayici/model."""
    cfg = config()
    return f"{cfg['provider']}/{cfg['model']}"


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        first, last = text.find("{"), text.rfind("}")
        if first != -1 and last > first:
            return json.loads(text[first:last + 1])
        raise


async def chat_json(system: str, user: str, max_tokens: int = 6000) -> tuple[dict, dict]:
    """Sistem + kullanıcı mesajından JSON nesnesi döndürür.

    Dönen ikinci değer sağlayıcının usage bloğu (token + maliyet).
    JSON yarım kalırsa bütçe büyütülüp tekrar denenir.
    """
    cfg = config()
    budget = max_tokens
    last_error = ""

    for attempt in range(3):
        payload: dict[str, Any] = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": budget,
            "temperature": 0.2,
        }
        if cfg["json_mode"]:
            payload["response_format"] = {"type": "json_object"}
        if cfg["thinking_off"]:
            # opencode-go: reasoning tokenlari butceyi yiyip icerigi bos birakiyor.
            payload["thinking"] = {"type": "disabled"}

        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
            **cfg["headers"],
        }

        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            resp = await client.post(f"{cfg['base_url']}/chat/completions",
                                     json=payload, headers=headers)

        if resp.status_code == 401:
            raise LLMError(f"{cfg['provider']}: API anahtari reddedildi (401)")
        if resp.status_code == 404:
            raise LLMError(
                f"{cfg['provider']}: /chat/completions bulunamadi — LLM_BASE_URL'i kontrol et "
                f"({cfg['base_url']})"
            )
        if resp.status_code != 200:
            raise LLMError(f"{cfg['provider']} {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        if "choices" not in data:
            raise LLMError(f"{cfg['provider']}: beklenmeyen yanit: {str(data)[:200]}")

        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()
        usage = data.get("usage") or {}

        if not text:
            reason = (message.get("reasoning_content") or "")[:100]
            raise LLMError(
                f"model bos icerik dondurdu (finish={choice.get('finish_reason')}, "
                f"reasoning={reason!r})"
            )

        if choice.get("finish_reason") == "length":
            last_error = "yanit uzunluk sinirinda kesildi"
            budget = min(budget * 2, 24000)
            continue

        try:
            return _extract_json(text), usage
        except json.JSONDecodeError as exc:
            last_error = f"gecersiz JSON ({exc})"
            budget = min(budget * 2, 24000)

    raise LLMError(f"model 3 denemede gecerli JSON dondurmedi: {last_error}")
