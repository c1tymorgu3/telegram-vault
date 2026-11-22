import hmac
import ipaddress
import os

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

import config

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Kopan istemciler listede kalirsa her yayinda bosuna denenir ve
        # liste sinirsiz buyur; gonderim hatasi verenleri tur sonunda dusur.
        dead: list[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()


def is_loopback(host: str | None) -> bool:
    """Yalnizca yerel kaynaklar (127.0.0.1, ::1, ::ffff:127.0.0.1) kabul edilir."""
    if not host:
        return False
    host = str(host).strip().lower()
    host = host.removeprefix("::ffff:")
    host = host.split("%", 1)[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def internal_token() -> str:
    """Paylasilan anahtar: once config.INTERNAL_TOKEN, yoksa INTERNAL_TOKEN ortam degiskeni.

    Anahtar hic tanimli degilse bos doner; bu durumda yalnizca loopback kontrolu uygulanir.
    """
    token = getattr(config, "INTERNAL_TOKEN", None) if config else None
    if not token:
        token = os.getenv("INTERNAL_TOKEN", "")
    return token or ""


def token_matches(provided: str, expected: str) -> bool:
    """Sabit zamanli karsilastirma: timing sizintisini kapatir."""
    if not expected:
        return True
    return hmac.compare_digest((provided or "").encode("utf-8"), expected.encode("utf-8"))


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not websocket.session.get("authenticated", False):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@router.post("/internal/broadcast")
async def internal_broadcast(request: Request):
    # Panel disari acilirsa sahte olay basilmamali: kaynak loopback olmali.
    client_host = request.client.host if request.client else None
    if not is_loopback(client_host):
        return JSONResponse(status_code=403, content={"detail": "Yetkisiz kaynak"})

    # Ortak anahtar tanimliysa header'daki deger sabit zamanli karsilastirilir.
    if not token_matches(request.headers.get("X-Internal-Token", ""), internal_token()):
        return JSONResponse(status_code=403, content={"detail": "Gecersiz token"})

    try:
        data = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Gecersiz JSON"})
    if not isinstance(data, dict):
        return JSONResponse(status_code=400, content={"detail": "Gecersiz JSON"})

    await manager.broadcast(data)
    return {"status": "ok"}
