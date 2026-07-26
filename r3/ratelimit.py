"""Per-IP token buckets, held in memory.

The tool is public and unauthenticated, and a search miss can reach MusicBrainz,
so one person with a loop could spend the shared upstream allowance for
everybody. This is the cheap defence: a bucket per (client, rule), refilled
continuously.

Per process, so the effective limit is doubled across Web01 and Web02. That is
a known compromise (DECISIONS.md) — the alternative is a shared limiter in Redis,
which is a whole dependency for a bound that only needs to be roughly right.
"""

import logging
import threading
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from r3 import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    name: str
    capacity: int      # burst size, and the ceiling
    per_seconds: float # time to refill from empty to full


# BUILD.md §6: search 30/min, form posts 5/min. Anything not matched here is
# unlimited — the routes that exist so far either read Postgres or are the
# load balancer's health check.
SEARCH = Rule("search", capacity=30, per_seconds=60.0)
WRITE = Rule("write", capacity=5, per_seconds=60.0)

RULES: tuple[tuple[str, str | None, Rule], ...] = (
    # (path prefix, method or None for any, rule)
    ("/api/search", "GET", SEARCH),
    ("/api/request-artist", "POST", WRITE),
    ("/api/report", "POST", WRITE),
)

# The load balancer polls this constantly and must never be throttled.
EXEMPT_PATHS = ("/health",)

# Bound the table so a spray of forged IPs can't grow it without limit.
MAX_TRACKED = 20_000
SWEEP_EVERY_SECONDS = 60.0


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, tokens: float, updated: float) -> None:
        self.tokens = tokens
        self.updated = updated


class Limiter:
    """Token buckets keyed by (client, rule)."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, client: str, rule: Rule) -> tuple[bool, float]:
        """Spend a token. Returns (allowed, seconds until the next one)."""
        rate = rule.capacity / rule.per_seconds
        now = time.monotonic()
        key = (client, rule.name)

        with self._lock:
            self._maybe_sweep(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(rule.capacity), updated=now)
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.updated
                bucket.tokens = min(rule.capacity, bucket.tokens + elapsed * rate)
                bucket.updated = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0

            return False, (1.0 - bucket.tokens) / rate

    def _maybe_sweep(self, now: float) -> None:
        """Drop buckets that have refilled — they carry no state worth keeping.

        Called with the lock held.
        """
        if now - self._last_sweep < SWEEP_EVERY_SECONDS and len(self._buckets) < MAX_TRACKED:
            return
        self._last_sweep = now

        stale = [
            key
            for key, bucket in self._buckets.items()
            # Idle longer than a full refill means the bucket is back at
            # capacity, so forgetting it is indistinguishable from keeping it.
            if now - bucket.updated > SWEEP_EVERY_SECONDS
        ]
        for key in stale:
            del self._buckets[key]

        if len(self._buckets) >= MAX_TRACKED:
            log.warning("rate limiter tracking %d clients after sweep", len(self._buckets))

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


limiter = Limiter()


def rule_for(path: str, method: str) -> Rule | None:
    if path in EXEMPT_PATHS:
        return None
    for prefix, wanted_method, rule in RULES:
        if path.startswith(prefix) and (wanted_method is None or wanted_method == method):
            return rule
    return None


def client_id(request: Request) -> str:
    """Identify the caller.

    Behind the load balancer every request arrives from Lb01, so keying on the
    socket peer would put the whole internet in one bucket and throttle the site
    to 30 searches a minute. Keying on X-Forwarded-For unconditionally is the
    opposite failure: anyone could forge a header per request and never be
    limited at all. So the header is trusted only when we've been told we are
    actually behind a proxy.
    """
    if config.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Leftmost entry is the original client; the rest are proxies.
            return forwarded.split(",")[0].strip()

    if request.client is None:
        return "unknown"
    return request.client.host


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rule = rule_for(request.url.path, request.method)
        if rule is None:
            return await call_next(request)

        who = client_id(request)
        allowed, retry_after = limiter.check(who, rule)

        if not allowed:
            log.info("rate limited %s on %s (%s)", who, request.url.path, rule.name)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": (
                        "Too many requests. Please wait a moment and try again."
                    ),
                },
                headers={"Retry-After": str(max(1, round(retry_after)))},
            )

        return await call_next(request)
