"""FastAPI surface over the capture service plus a minimal live dashboard."""
from __future__ import annotations

import math
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .capture import CaptureService
from .driver import InstrumentClient, InstrumentError, TransportError
from .protocol import CHANNELS

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"

API_TOKEN_ENV = "LABLINK_API_TOKEN"
ALLOWED_ORIGINS_ENV = "LABLINK_ALLOWED_ORIGINS"


def create_app(client: InstrumentClient, db_path: str, api_token: str | None = None,
               allow_insecure_writes: bool = False) -> FastAPI:
    """Build the API app.

    Setpoint writes reach the instrument, so they are gated. With an
    ``api_token`` (or ``LABLINK_API_TOKEN``) callers must send a bearer token
    in the Authorization header. Without a token, writes are refused
    unless ``allow_insecure_writes=True``, which is only safe on a loopback
    bind. Requests carrying a foreign ``Origin`` header are always rejected.
    """
    token = api_token if api_token is not None else os.environ.get(API_TOKEN_ENV)
    allowed_origins = {
        o.strip() for o in os.environ.get(ALLOWED_ORIGINS_ENV, "").split(",") if o.strip()
    }
    svc = CaptureService(client, db_path)

    @asynccontextmanager
    async def lifespan(app):
        svc.start()
        yield
        svc.stop()

    app = FastAPI(title="lablink", description="Instrument capture gateway",
                  lifespan=lifespan)
    app.state.capture = svc

    @app.get("/")
    def dashboard():
        if DASHBOARD.exists():
            return FileResponse(DASHBOARD)
        return {"detail": "dashboard file missing", "api": "/docs"}

    @app.get("/api/instrument")
    def instrument():
        try:
            return {"idn": client.identify(), "status": client.status(),
                    "error_register": client.error_register(),
                    "connected": client.connected}
        except TransportError as e:
            return {"connected": False, "error": str(e)}

    @app.get("/api/channels")
    def channels():
        return {k: v for k, v in CHANNELS.items()}

    @app.get("/api/latest")
    def latest():
        return svc.latest()

    @app.get("/api/history/{channel}")
    def history(channel: str, limit: int = 200):
        if channel.upper() not in CHANNELS:
            raise HTTPException(404, f"unknown channel {channel}")
        if not 1 <= limit <= 2000:
            raise HTTPException(422, "limit must be 1-2000")
        return svc.history(channel.upper(), limit)

    @app.get("/api/alarms")
    def alarms(limit: int = 50):
        if not 1 <= limit <= 500:
            raise HTTPException(422, "limit must be 1-500")
        return svc.alarms(limit)

    @app.get("/api/stats")
    def stats():
        return svc.stats

    def _check_write(request: Request):
        origin = request.headers.get("origin")
        if origin is not None:
            origin_host = urlparse(origin).netloc
            if origin_host != request.headers.get("host") and origin not in allowed_origins:
                raise HTTPException(403, "cross-origin writes are not allowed")
        if token is None:
            if not allow_insecure_writes:
                raise HTTPException(
                    403, f"setpoint writes disabled. Set {API_TOKEN_ENV} or "
                    f"pass allow_insecure_writes=True for loopback-only use")
            return
        auth = request.headers.get("authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {token}"):
            raise HTTPException(401, "missing or invalid bearer token")

    @app.post("/api/setpoint/{channel}")
    def setpoint(request: Request, channel: str, value: float):
        _check_write(request)
        channel = channel.upper()
        if channel not in CHANNELS:
            raise HTTPException(404, f"unknown channel {channel}")
        if not math.isfinite(value):
            raise HTTPException(422, "setpoint must be finite")
        try:
            client.set_setpoint(channel, value)
        except InstrumentError as e:
            raise HTTPException(400, str(e)) from e
        except TransportError as e:
            raise HTTPException(502, str(e)) from e
        return {"ok": True, "channel": channel, "setpoint": value}

    return app
