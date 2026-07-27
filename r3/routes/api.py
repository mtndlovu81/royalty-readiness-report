"""JSON routes.

Search resolves against Postgres first and only falls back to MusicBrainz when
we hold nothing locally — the miss is a discovery question ("who do you mean?"),
answered by one rate-gated request. Catalogue building never happens here; that
is the worker's job, and it is what keeps the throttle invariant intact.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from psycopg.types.json import Jsonb

from r3 import db, musicbrainz as mb

log = logging.getLogger(__name__)

router = APIRouter()

# A search box entry longer than this is not a real artist name.
MAX_QUERY_LENGTH = 120

LOCAL_LIMIT = 10
UPSTREAM_LIMIT = 10

# How long a cached upstream result stays servable. Retention is longer (7
# days, pruned by the worker) so stale rows can be overwritten in place rather
# than churning the primary key.
CACHE_FRESH_INTERVAL = "24 hours"

# 'failed' artists are broken builds — findable in the database, but not
# something to offer a visitor.
VISIBLE_STATUSES = ("published", "building", "pending")


def _like_pattern(term: str) -> str:
    """Escape LIKE wildcards so a query of '%' doesn't match everything."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _normalize(term: str) -> str:
    """Lowercased, unaccented, trimmed, whitespace collapsed.

    Delegated to Postgres so the cache key and the local lookup below are
    produced by one definition. Doing it in Python would drift — unicodedata
    folds 'ö' but not 'ø', 'ß' or 'æ', all of which unaccent folds.
    """
    return db.query_value("SELECT r3_normalize(%s)", (term,)) or ""


def _search_local(normalized: str) -> list[dict[str, Any]]:
    # r3_normalize() on the column side too, so "bjork" finds "Björk".
    rows = db.query(
        """
        SELECT slug, name, disambiguation, country, type, status
          FROM artists
         WHERE r3_normalize(name) LIKE %s ESCAPE '\\'
           AND status = ANY(%s)
         ORDER BY (r3_normalize(name) = %s) DESC,
                  length(name),
                  name
         LIMIT %s
        """,
        (_like_pattern(normalized), list(VISIBLE_STATUSES), normalized, LOCAL_LIMIT),
    )
    return [
        {
            "name": row["name"],
            "slug": row["slug"],
            "disambiguation": row["disambiguation"],
            "country": row["country"],
            # Same key as the upstream candidates below, so both shapes render
            # through one template. NULL means unclassified, not "not a person".
            "type": row["type"],
            "status": row["status"],
            "in_catalogue": True,
        }
        for row in rows
    ]


def _cache_get(normalized: str, *, fresh_only: bool = True) -> list[dict[str, Any]] | None:
    """Cached candidates, or None for a miss.

    `fresh_only=False` accepts a stale row, which is what an upstream outage
    wants: BUILD.md §4 says serve cached data rather than nothing, and slightly
    old candidates beat an empty page.
    """
    if fresh_only:
        row = db.query_one(
            """
            SELECT results
              FROM search_cache
             WHERE query = %s
               AND fetched_at > now() - %s::interval
            """,
            (normalized, CACHE_FRESH_INTERVAL),
        )
    else:
        row = db.query_one(
            "SELECT results FROM search_cache WHERE query = %s",
            (normalized,),
        )
    return row["results"] if row else None


def _cache_put(normalized: str, results: list[dict[str, Any]]) -> None:
    """Overwrite in place, keeping the key stable across refreshes."""
    db.execute(
        """
        INSERT INTO search_cache (query, results)
        VALUES (%s, %s)
        ON CONFLICT (query)
        DO UPDATE SET results = EXCLUDED.results, fetched_at = now()
        """,
        (normalized, Jsonb(results)),
    )


def _search_upstream(term: str) -> list[dict[str, Any]]:
    """Offer build candidates. One gated request."""
    candidates = []
    for artist in mb.search_artists(term, limit=UPSTREAM_LIMIT):
        life_span = artist.get("life-span") or {}
        candidates.append(
            {
                "name": artist.get("name"),
                "mbid": artist.get("id"),
                "disambiguation": artist.get("disambiguation"),
                "country": artist.get("country"),
                "type": artist.get("type"),
                "began": life_span.get("begin"),
                "ended": life_span.get("end"),
                "score": artist.get("score"),
                "in_catalogue": False,
            }
        )
    return candidates


def run_search(q: str) -> dict[str, Any]:
    """Find an artist. Shared by the JSON and HTML routes.

    `source` tells the caller what it is looking at:
      local        — profiles we hold, ready to open
      musicbrainz  — candidates we could build, not yet in the catalogue
      unavailable  — upstream is down; the caller should offer the request form
    """
    term = (q or "").strip()
    if not term:
        return {
            "query": "",
            "source": "local",
            "results": [],
            "cached": False,
            "upstream_available": True,
        }

    normalized = _normalize(term)

    results = _search_local(normalized)
    if results:
        # A local hit never reads or writes the cache — the cache exists to
        # spare us the upstream call, and there wasn't going to be one.
        return {
            "query": term,
            "source": "local",
            "results": results,
            "cached": False,
            "upstream_available": True,
        }

    cached = _cache_get(normalized)
    if cached is not None:
        return {
            "query": term,
            "source": "musicbrainz",
            "results": cached,
            "cached": True,
            "upstream_available": True,
        }

    try:
        candidates = _search_upstream(term)
        _cache_put(normalized, candidates)
        return {
            "query": term,
            "source": "musicbrainz",
            "results": candidates,
            "cached": False,
            "upstream_available": True,
        }
    except mb.MusicBrainzError as exc:
        # An upstream outage degrades discovery to the request form rather than
        # failing the request (BUILD.md §4). We hold nothing locally either way.
        log.warning("upstream search failed for %r: %s", term, exc)

        stale = _cache_get(normalized, fresh_only=False)
        if stale is not None:
            return {
                "query": term,
                "source": "musicbrainz",
                "results": stale,
                "cached": True,
                "upstream_available": False,
            }

        return {
            "query": term,
            "source": "unavailable",
            "results": [],
            "cached": False,
            "upstream_available": False,
        }


@router.get("/api/search")
def search(q: str = Query(default="", max_length=MAX_QUERY_LENGTH)) -> dict[str, Any]:
    """Live search, for the JSON client."""
    return run_search(q)


# A build with no heartbeat for this long is not running. Generous, because a
# single upstream request can legitimately take 30s plus retries.
STALLED_AFTER_SECONDS = 90


def build_status(request_id: str) -> dict[str, Any] | None:
    """Progress for one queued build. Shared by the API and the status page."""
    job = db.query_one(
        """
        SELECT id, artist_mbid, status, error, progress, progress_pct,
               EXTRACT(EPOCH FROM (now() - requested_at))::int AS waiting_seconds,
               EXTRACT(EPOCH FROM (now() - COALESCE(heartbeat_at, started_at)))::int
                   AS since_heartbeat
          FROM build_queue WHERE id = %s
        """,
        (request_id,),
    )
    if job is None:
        return None

    artist = db.query_one(
        "SELECT slug FROM artists WHERE mbid = %s AND status = 'published'",
        (job["artist_mbid"],),
    )

    since = job["since_heartbeat"]
    # Nothing has touched this row recently. Either no worker is running or it
    # died mid-build; from here those look identical, and both mean the same
    # thing to whoever is waiting — say so rather than showing a hopeful bar.
    stalled = (
        job["status"] in ("queued", "running")
        and (since is None or since > STALLED_AFTER_SECONDS)
        and job["waiting_seconds"] > STALLED_AFTER_SECONDS
    )

    return {
        "status": job["status"],
        "progress": job["progress"],
        "progress_pct": job["progress_pct"],
        "waiting_seconds": job["waiting_seconds"],
        "stalled": stalled,
        "error": job["error"],
        "slug": artist["slug"] if artist else None,
        "done": bool(artist) or job["status"] == "done",
    }


@router.get("/api/pending/{request_id}")
def pending_status(request_id: str) -> dict[str, Any]:
    """Polled by the status page.

    Keyed on the request id, not a slug: no artist row exists until the worker
    mints one, which is the whole reason the slug is trustworthy.
    """
    try:
        uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such request") from None

    status = build_status(request_id)
    if status is None:
        raise HTTPException(status_code=404, detail="No such request")
    return status


@router.get("/health", response_class=PlainTextResponse)
def health() -> str:
    """Load balancer health check.

    Deliberately does not touch Postgres: this answers "is this process alive",
    and a database blip should not pull every web server out of rotation at
    once. `db.healthy()` exists for a deeper check if one is ever wanted.
    """
    return "ok"
