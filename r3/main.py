"""FastAPI application and route registration."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from r3 import config, db, musicbrainz as mb
from r3.ratelimit import RateLimitMiddleware
from r3.routes import api, pages

STATIC_DIR = Path(__file__).resolve().parent / "static"

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.configure_logging()
    log.info("r3 starting (worker=%s, proxy headers=%s)", config.RUN_WORKER, config.TRUST_PROXY_HEADERS)
    yield
    # Nothing here opens on startup — the pool and the HTTP client are both
    # created on first use — but both need closing if they were.
    db.close_pool()
    mb.close()
    log.info("r3 stopped")


app = FastAPI(
    title="Royalty Readiness Report",
    description="Which of your songs are missing the identifiers royalties depend on.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(api.router)
app.include_router(pages.router)
