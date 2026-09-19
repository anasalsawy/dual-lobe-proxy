"""FastAPI application entrypoint (``uvicorn dual_lobe.api.app:app``)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..core.engine import dispose_engines
from ..core.settings import get_settings
from ..provider.adapters import close_http_client
from ..obs.telemetry import TRACER
from . import chat, director, events, health, memory, state

LOG = logging.getLogger("dual_lobe.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    TRACER.configure(s.otel_enabled)
    # Seed bootstrap API keys into the database on startup. Idempotent: skips
    # keys that already exist. Runs after migrations (see Dockerfile/railway.json).
    from ..core.bootstrap import seed as bootstrap_seed
    try:
        created = await bootstrap_seed()
        if created:
            LOG.info("bootstrap: created %d API key(s)", len(created))
    except Exception as exc:
        LOG.error("bootstrap seed failed: %s", exc)
    LOG.info("dual-lobe gateway starting host=%s port=%d stage=%s", s.host, s.port, s.rollout_stage)
    try:
        yield
    finally:
        await close_http_client()
        await dispose_engines()


app = FastAPI(
    title="Dual-Lobe Inference Proxy",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(director.router)
app.include_router(memory.router)
app.include_router(events.router)
app.include_router(state.router)
app.include_router(health.router)


@app.get("/")
async def root():
    return {"service": "dual-lobe", "status": "ok"}
