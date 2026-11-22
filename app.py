import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import config
from database import init_db
from middleware.security import security_headers
from routes import auth, dashboard, digest, messages, profiles, websocket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('security.log'),
        logging.StreamHandler()
    ]
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Varsayılan/zayıf kimlik bilgileriyle panel açılmasın. Sorun varsa
    # RuntimeError fırlar ve uygulama hiç ayağa kalkmaz (test/geliştirme için
    # ALLOW_INSECURE_DEFAULTS=1 sadece uyarı loglar).
    config.validate_security_settings()
    await init_db()
    yield


app = FastAPI(title="Telegram Vault", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=1800,
                    https_only=config.SESSION_HTTPS_ONLY, same_site='strict')

app.middleware("http")(security_headers)

if not os.path.exists("templates"):
    os.makedirs("templates")

if not os.path.exists("static"):
    os.makedirs("static")

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(messages.router)
app.include_router(dashboard.router)
app.include_router(digest.router)
app.include_router(profiles.router)
app.include_router(websocket.router)
