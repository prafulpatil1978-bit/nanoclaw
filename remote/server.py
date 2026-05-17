import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth
from . import input as inp
from . import screen as scr

app = FastAPI(title="Nanoclaw Remote")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

FRAME_INTERVAL = 0.08  # ~12 FPS


class LoginRequest(BaseModel):
    password: str
    totp_code: str


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text())


@app.post("/api/login")
async def login(body: LoginRequest, request: Request):
    ip = request.client.host
    allowed, msg = auth.check_rate_limit(ip)
    if not allowed:
        raise HTTPException(429, msg)

    ok, msg = auth.verify_credentials(body.password, body.totp_code)
    if not ok:
        auth.record_failed(ip)
        raise HTTPException(401, msg)

    auth.clear_rate_limit(ip)
    return {"token": auth.create_token()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = "") -> None:
    if not auth.verify_token(token):
        await ws.close(code=4401)
        return

    await ws.accept()

    # Send screen dimensions so client can size the canvas
    _, w, h = await asyncio.to_thread(scr.capture_frame)
    await ws.send_text(json.dumps({"type": "info", "width": w, "height": h}))

    async def send_frames() -> None:
        while True:
            frame, _, _ = await asyncio.to_thread(scr.capture_frame)
            try:
                await ws.send_bytes(frame)
            except Exception:
                break
            await asyncio.sleep(FRAME_INTERVAL)

    frame_task = asyncio.create_task(send_frames())
    try:
        while True:
            raw = await ws.receive_text()
            await asyncio.to_thread(inp.handle_event, json.loads(raw))
    except WebSocketDisconnect:
        pass
    finally:
        frame_task.cancel()
        try:
            await frame_task
        except asyncio.CancelledError:
            pass
