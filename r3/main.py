"""FastAPI application and route registration."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from r3 import config, db, musicbrainz as mb
from r3.ratelimit import RateLimitMiddleware
from r3.routes import api

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

app.include_router(api.router)
