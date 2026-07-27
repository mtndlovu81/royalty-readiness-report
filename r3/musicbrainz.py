"""Throttled MusicBrainz Web Service v2 client.

Nothing here touches the database.

**Catalogue building is worker-side only.** The one exception on the read path
is artist *search*: when Postgres holds no match, `routes/api.py` makes a single
gated request to ask "who do you mean?". That is a bounded discovery call, not a
fetch — no profile is built and nothing is persisted. Everything that walks a
catalogue belongs to the worker.

Two obligations to MusicBrainz, both non-negotiable:

* one request per second, enforced by a module-level gate that every call
  passes through, retries included;
* a descriptive User-Agent with real contact details on every request.

The gate is a process-wide lock, which is why the worker runs on Web01 only.
A second worker would be a second gate and twice the outbound rate.
"""

import json
import logging
import threading
import time
from typing import Any

import httpx

from r3 import config

log = logging.getLogger(__name__)

# Generous: a browse of 100 recordings with includes is a real query, and we
# only get one attempt per second, so giving up early wastes the whole budget.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# 503 is routine load shedding from MusicBrainz, not punishment — the probe hit
# one on its first run and succeeded unchanged on retry.
#
# BUILD.md §4 says "retry up to 3x", so this is a count of *retries*, not of
# total attempts: 4 requests worst case, backing off 1s, 2s, 4s between them.
MAX_RETRIES_503 = 3
BACKOFF_BASE_SECONDS = 1.0

# Upstream's own number beats our guess, but two degenerate cases need handling:
# `Retry-After: 0` means "instantly", which defeats the point of backing off,
# and an hour-long value would strand a catalogue build. Outside this range we
# fall back to the exponential schedule.
RETRY_AFTER_CAP_SECONDS = 60.0

# A timeout gets one retry, then the caller decides (BUILD.md §4).
MAX_RETRIES_TIMEOUT = 1

# The gate reserves a slot before the request is built and sent, so the gap
# between reservations is exact but the gap between actual sends jitters by a
# fraction of a millisecond either way. This margin keeps us provably on the
# safe side of one request per second; it costs ~4s on an 80-request build.
GATE_MARGIN_SECONDS = 0.05

# Lucene reserves these; an artist named "AC/DC" or a query with a stray colon
# would otherwise change the query's meaning or fail outright.
_LUCENE_SPECIAL = set('+-&|!(){}[]^"~*?:\\/')


class MusicBrainzError(Exception):
    """Base for everything this module raises."""


class NotFound(MusicBrainzError):
    """404 — the entity doesn't exist upstream. Offer 'add this artist'."""


class BadRequest(MusicBrainzError):
    """400 — definitive. The body lists the valid includes; log it verbatim."""


class Unavailable(MusicBrainzError):
    """503 after every retry, or a transport failure. Retry the build later."""


class Timeout(MusicBrainzError):
    """Timed out after a retry. Mark the build failed with a reason."""


class MalformedResponse(MusicBrainzError):
    """200 with a body that isn't JSON. Skip the entity, continue the build."""


_gate_lock = threading.Lock()
_next_allowed_at = 0.0

# Every HTTP request made, retries included. The build pipeline's cost is the
# whole reason for the release-first design, so it needs to be measurable.
request_count = 0


def reset_request_count() -> int:
    """Zero the counter and return what it held."""
    global request_count
    previous = request_count
    request_count = 0
    return previous

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _client_instance() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    base_url=config.MB_BASE_URL,
                    headers={
                        "User-Agent": config.MB_USER_AGENT,
                        "Accept": "application/json",
                    },
                    timeout=TIMEOUT,
                    follow_redirects=True,
                )
    return _client


def close() -> None:
    """Close the HTTP client. Safe if never opened."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _wait_turn() -> None:
    """Block until this process is allowed to make another request.

    The lock is held across the sleep deliberately: callers queue up and leave
    one at a time, so concurrent callers can't all wake at once and burst.
    """
    global _next_allowed_at
    with _gate_lock:
        now = time.monotonic()
        delay = _next_allowed_at - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        _next_allowed_at = now + config.MB_RATE_LIMIT_SECONDS + GATE_MARGIN_SECONDS


def _encode(params: dict[str, Any]) -> dict[str, str]:
    """Drop empty params and join list values the way MusicBrainz expects."""
    out: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            if not value:
                continue
            # `inc` and friends are '+'-separated. httpx percent-encodes the
            # '+', which MusicBrainz decodes back to a separator — confirmed by
            # probe_q1.py, which got 12/12 recordings this way.
            value = "+".join(str(v) for v in value)
        out[key] = str(value)
    return out


def get(path: str, **params: Any) -> dict[str, Any]:
    """Make one throttled GET against the web service and return parsed JSON.

    Raises a MusicBrainzError subclass for every failure mode so callers can
    tell "this entity is missing" from "upstream is having a bad day".
    """
    params["fmt"] = "json"
    query = _encode(params)
    url = path.lstrip("/")

    global request_count

    attempt = 0
    timeouts = 0

    while True:
        attempt += 1
        _wait_turn()
        request_count += 1

        try:
            response = _client_instance().get(url, params=query)
        except httpx.TimeoutException as exc:
            timeouts += 1
            if timeouts <= MAX_RETRIES_TIMEOUT:
                log.warning("timeout on %s, retrying once", url)
                continue
            raise Timeout(f"{url} timed out after {timeouts} attempts") from exc
        except httpx.TransportError as exc:
            raise Unavailable(f"could not reach MusicBrainz for {url}: {exc}") from exc

        _log_rate_limit(response, url)

        status = response.status_code

        if status == 200:
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("malformed JSON from %s: %s", url, response.text[:200])
                raise MalformedResponse(f"{url} returned unparseable JSON") from exc

        if status == 404:
            raise NotFound(f"{url} not found upstream")

        if status == 400:
            # The body names the valid includes — the single most useful
            # debugging artefact this API produces. Keep it.
            raise BadRequest(f"{url} rejected: {response.text[:500]}")

        if status in (503, 429):
            # `attempt` is 1-based, so on the first failure this is retry 1.
            if attempt <= MAX_RETRIES_503:
                delay = _retry_delay(response, attempt)
                log.warning(
                    "%s from %s, retry %d/%d in %.1fs",
                    status,
                    url,
                    attempt,
                    MAX_RETRIES_503,
                    delay,
                )
                time.sleep(delay)
                continue
            raise Unavailable(
                f"{url} returned {status} after {attempt} attempts "
                f"({MAX_RETRIES_503} retries)"
            )

        raise MusicBrainzError(f"{url} returned unexpected {status}: {response.text[:200]}")


def _log_rate_limit(response: httpx.Response, url: str) -> None:
    """Record what's left of the allowance.

    MusicBrainz reports roughly 1200 requests per rolling window. A long
    catalogue build is the only thing that gets near it, and this is the only
    way to see it coming.
    """
    if not log.isEnabledFor(logging.DEBUG):
        return
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is None:
        return

    # X-RateLimit-Reset is a Unix timestamp; seconds-from-now is the readable
    # form. Confirmed against the live API: limit 1200, reset an epoch second.
    resets_in = "?"
    reset = response.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            resets_in = f"{max(0.0, float(reset) - time.time()):.0f}"
        except ValueError:
            pass

    log.debug(
        "rate limit: %s of %s remaining, resets in %ss (%s)",
        remaining,
        response.headers.get("X-RateLimit-Limit", "?"),
        resets_in,
        url,
    )


def _retry_delay(response: httpx.Response, retry: int) -> float:
    """Seconds to wait before retry number `retry` (1-based).

    Exponential — 1s, 2s, 4s — unless upstream sent a usable `Retry-After`,
    which wins because it reflects what the server actually knows. A `0` or a
    negative is not usable: taken literally it retries instantly and burns the
    retry budget during the exact busy period the backoff exists to wait out.
    """
    computed = BACKOFF_BASE_SECONDS * (2 ** (retry - 1))

    header = response.headers.get("Retry-After")
    if not header:
        return computed

    try:
        # Numeric seconds. The HTTP-date form is legal but MusicBrainz doesn't
        # use it; falling back to the exponential schedule is fine if it ever does.
        advised = float(header)
    except ValueError:
        log.debug("unparseable Retry-After %r, backing off %.1fs", header, computed)
        return computed

    if advised <= 0:
        log.debug("Retry-After %r is not a delay, backing off %.1fs", header, computed)
        return computed

    if advised > RETRY_AFTER_CAP_SECONDS:
        log.warning(
            "Retry-After %.0fs exceeds the %.0fs cap; waiting %.0fs",
            advised,
            RETRY_AFTER_CAP_SECONDS,
            RETRY_AFTER_CAP_SECONDS,
        )
        return RETRY_AFTER_CAP_SECONDS

    return advised


def escape_query(text: str) -> str:
    """Neutralise Lucene syntax in a user-supplied search term."""
    out = []
    for char in text:
        if char in _LUCENE_SPECIAL:
            out.append("\\")
        out.append(char)
    return "".join(out)


def search_artists(name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search artists by name. One request.

    Returns the raw MusicBrainz artist objects — score, name, disambiguation,
    country, type and life-span, which DESIGN.md §5 found sufficient to
    disambiguate. Returns [] for a blank query without calling upstream.
    """
    name = (name or "").strip()
    if not name:
        return []

    limit = max(1, min(int(limit), 100))
    payload = get("artist", query=escape_query(name), limit=limit)
    artists = payload.get("artists")
    if not isinstance(artists, list):
        raise MalformedResponse("artist search returned no 'artists' list")
    return artists


def get_artist(mbid: str, inc: list[str] | str | None = None) -> dict[str, Any]:
    """Look up one artist by MBID. Used for `ipis` and aliases."""
    return get(f"artist/{mbid}", inc=inc)
