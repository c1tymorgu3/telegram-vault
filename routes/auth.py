import hashlib
import hmac
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config
from database import SessionLocal

router = APIRouter()
templates = Jinja2Templates(directory="templates")

login_attempts = defaultdict(list)
audit_log = []
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 900
AUDIT_MAX = 500
# Sozluk IP basina buyuyordu: hem zaman asimi hem de ust sinir gerekli.
LOGIN_ATTEMPTS_MAX_IPS = 5000


def is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated", False)


def prune_login_attempts(now: float | None = None):
    """Eski denemeleri dusur: pencere disi kayitlar silinir, sozluk boyutu sinirlanir."""
    if now is None:
        now = time.time()
    for ip in list(login_attempts.keys()):
        fresh = [t for t in login_attempts[ip] if now - t < RATE_LIMIT_WINDOW]
        if fresh:
            login_attempts[ip] = fresh
        else:
            login_attempts.pop(ip, None)

    # Ust sinir asilirsa en eski son denemeye sahip IP'ler atilir.
    if len(login_attempts) > LOGIN_ATTEMPTS_MAX_IPS:
        ordered = sorted(
            login_attempts.items(),
            key=lambda kv: max(kv[1]) if kv[1] else 0,
        )
        excess = len(login_attempts) - LOGIN_ATTEMPTS_MAX_IPS
        for ip, _ in ordered[:excess]:
            login_attempts.pop(ip, None)


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    prune_login_attempts(now)
    login_attempts[ip] = [t for t in login_attempts[ip] if now - t < RATE_LIMIT_WINDOW]
    return len(login_attempts[ip]) >= RATE_LIMIT_MAX


def record_attempt(ip: str):
    prune_login_attempts()
    login_attempts[ip].append(time.time())


def password_fingerprint(value: str) -> str:
    """Identify a repeated wrong password without storing the password.

    The audit log used to keep the raw string a visitor typed, which turns the
    auth log into a credential dump the moment someone fat-fingers a real
    password. A truncated digest plus its length is enough to spot the same
    attempt coming back, and is not reversible.
    """
    if not value:
        return "—"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{digest} ({len(value)} ch)"


def log_auth_event(ip: str, user_agent: str, attempt, success: bool, reason: str = ""):
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:12]
    audit_log.insert(0, {
        "time": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        "ip": ip,
        "ip_hash": ip_hash,
        "user_agent": (user_agent or "Unknown")[:200],
        "pw_fingerprint": "—" if success else password_fingerprint(attempt),
        "success": success,
        "reason": reason
    })
    if len(audit_log) > AUDIT_MAX:
        audit_log.pop()


async def verify_hcaptcha(token: str) -> bool:
    if not config.HCAPTCHA_SECRET_KEY:
        return True
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("https://api.hcaptcha.com/siteverify", data={
                "secret": config.HCAPTCHA_SECRET_KEY,
                "response": token
            })
            return resp.json().get("success", False)
    except Exception:
        return False


async def get_db():
    async with SessionLocal() as session:
        yield session


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html", context={
            "error": None,
            "hcaptcha_site_key": config.HCAPTCHA_SITE_KEY
        }
    )


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    client_ip = request.client.host
    ua = request.headers.get("user-agent", "Unknown")

    if is_rate_limited(client_ip):
        log_auth_event(client_ip, ua, "***", False, "Rate limited")
        return templates.TemplateResponse(
            request=request, name="login.html", context={
                "error": "Too many failed attempts. Please wait 15 minutes.",
                "hcaptcha_site_key": config.HCAPTCHA_SITE_KEY
            }, status_code=429
        )

    form_data = await request.form()
    raw_token = form_data.get("h-captcha-response", "")
    # Form alani dosya olarak da gonderilebilir; yalnizca metin kabul edilir.
    hcaptcha_token = raw_token if isinstance(raw_token, str) else ""

    if config.HCAPTCHA_SECRET_KEY and not await verify_hcaptcha(hcaptcha_token):
        record_attempt(client_ip)
        log_auth_event(client_ip, ua, "***", False, "Captcha failed")
        return templates.TemplateResponse(
            request=request, name="login.html", context={
                "error": "Captcha verification failed.",
                "hcaptcha_site_key": config.HCAPTCHA_SITE_KEY
            }, status_code=403
        )

    # Sabit zamanli karsilastirma: '==' cevap suresinden sifre sizdirir.
    if hmac.compare_digest(
        password.encode("utf-8"), config.DASHBOARD_PASSWORD.encode("utf-8")
    ):
        request.session["authenticated"] = True
        request.session["login_time"] = datetime.now().isoformat()
        request.session["ip"] = client_ip
        login_attempts.pop(client_ip, None)
        log_auth_event(client_ip, ua, None, True, "Success")
        return RedirectResponse(url="/", status_code=302)
    else:
        record_attempt(client_ip)
        # Sifre denemesi log'a yazilmaz (security.log'da duz metin sifre kaliyordu).
        log_auth_event(client_ip, ua, "***", False, "Invalid password")
        return templates.TemplateResponse(
            request=request, name="login.html", context={
                "error": "Login failed. Please check your credentials.",
                "hcaptcha_site_key": config.HCAPTCHA_SITE_KEY
            }, status_code=401
        )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@router.get("/api/login-logs")
async def get_login_logs(request: Request):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return {"logs": audit_log}
