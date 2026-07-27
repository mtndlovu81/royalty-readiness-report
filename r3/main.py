"""FastAPI application and route registration."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from r3 import config, db, musicbrainz as mb
from r3.errors import error_context, wants_json
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

@app.exception_handler(StarletteHTTPException)
async def http_exception(request: Request, exc: StarletteHTTPException):
    """404s, 405s and anything a route raised deliberately.

    The detail is passed through because those messages are written for people
    ("No profile for that name"). Crashes take the other handler.
    """
    if wants_json(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return pages.templates.TemplateResponse(
        request,
        "error.html",
        error_context(exc.status_code, exc.detail if exc.status_code < 500 else None),
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """A parameter or form field FastAPI wouldn't accept.

    Its default answer is a JSON array of pydantic error objects, which is
    right for the API and unreadable in a browser — an over-long search term
    would have shown someone `{"type":"string_too_long"}`.
    """
    if wants_json(request):
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    log.info("validation error on %s: %s", request.url.path, exc.errors())
    return pages.templates.TemplateResponse(
        request, "error.html", error_context(422), status_code=422
    )


async def database_unavailable(request: Request, exc: Exception):
    """Postgres is unreachable. Say so as a 503, not a 500.

    A 500 tells a load balancer this instance is broken; a 503 says the thing
    behind it is, which is both true and the difference between one node being
    pulled and all of them.
    """
    log.error("database unavailable on %s: %s", request.url.path, exc)
    if wants_json(request):
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    return pages.templates.TemplateResponse(
        request, "error.html", error_context(503), status_code=503
    )


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Last resort. A stack trace is for the log, never for a visitor."""
    log.exception("unhandled error on %s", request.url.path)
    if wants_json(request):
        return JSONResponse({"error": "internal error"}, status_code=500)
    return pages.templates.TemplateResponse(
        request, "error.html", error_context(500), status_code=500
    )


# Registered for every "database isn't answering" class, not just
# OperationalError: an unreachable Postgres surfaces as PoolTimeout, which is a
# psycopg_pool type and would otherwise fall through to the 500 handler.
for _exc in db.UNAVAILABLE:
    app.add_exception_handler(_exc, database_unavailable)

app.add_middleware(RateLimitMiddleware)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(api.router)
app.include_router(pages.router)
