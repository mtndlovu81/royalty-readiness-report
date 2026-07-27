"""JSON routes.

Search resolves against Postgres first and only falls back to MusicBrainz when
we hold nothing locally — the miss is a discovery question ("who do you mean?"),
answered by one rate-gated request. Catalogue building never happens here; that
is the worker's job, and it is what keeps the throttle invariant intact.
"""

import logging
import re
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Form, HTTPException, Query
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


# --- requests and reports -------------------------------------------------
#
# Both write to one `issues` table with a type discriminator. They differ by
# payload, not by machinery — same triage, same rate limiting, same dedup.
#
# A request is not a claim of ownership, and a report is not a correction:
# anyone can type any name. Nothing here changes what the site displays.

SPOTIFY_ARTIST_ID = re.compile(r"^[A-Za-z0-9]{22}$")

# Named so the message can say what they actually pasted. "Invalid URL" sends
# someone back to the same mistake; "that's an album link" doesn't.
SPOTIFY_WRONG_KIND = {
    "track": "That's a link to a track. Open the artist's own page on Spotify and copy that link instead.",
    "album": "That's a link to an album. Open the artist's own page on Spotify and copy that link instead.",
    "playlist": "That's a link to a playlist. Open the artist's own page on Spotify and copy that link instead.",
    "episode": "That's a link to a podcast episode, not an artist.",
    "show": "That's a link to a podcast, not an artist.",
    "user": "That's a link to a listener's profile, not an artist.",
    "search": "That's a search results link. Open the artist's page itself and copy that link.",
}

# Field names a report may be filed against — whitelisted, because this value
# is stored and later used to route triage.
REPORTABLE_FIELDS = {
    "publishing_id", "streaming_id", "versions", "contributors",
    "title", "artist", "other",
}

REPORTABLE_ENTITIES = {"song", "version", "contributor", "artist"}

MAX_FREETEXT = 2000


def parse_spotify_artist_id(url: str) -> tuple[str | None, str | None]:
    """Pull the artist id out of a Spotify link. Returns (id, error message).

    Accepts the share URL and the `spotify:artist:…` URI. Rejects every other
    kind of Spotify link by name, because people paste track and album links
    constantly and a generic rejection teaches them nothing.
    """
    raw = (url or "").strip()
    if not raw:
        return None, "Paste a link to the artist's Spotify page."

    uri = raw.removeprefix("spotify:")
    if uri.startswith("artist:"):
        candidate = uri.removeprefix("artist:").split("?")[0]
        if SPOTIFY_ARTIST_ID.match(candidate):
            return candidate, None
        return None, "That doesn't look like a Spotify artist link."

    parsed = urlparse(raw if "//" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in {"open.spotify.com", "play.spotify.com", "spotify.com"}:
        return None, "That isn't a Spotify link. Paste the artist's Spotify page."

    # /artist/{id}, or /intl-de/artist/{id} on localised share links.
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]

    if not parts:
        return None, "That link doesn't point at anything. Open the artist's page and copy the link."

    kind = parts[0].lower()
    if kind != "artist":
        return None, SPOTIFY_WRONG_KIND.get(
            kind, "That's not a link to an artist page on Spotify."
        )

    if len(parts) < 2 or not SPOTIFY_ARTIST_ID.match(parts[1]):
        return None, "That artist link looks incomplete. Copy it again from Spotify."

    return parts[1], None


def record_artist_request(name: str, spotify_url: str) -> tuple[str | None, str | None]:
    """File a request for an artist we can't build. Returns (issue id, error).

    Deduplicated on the Spotify artist id: names are hopeless for this and
    links are exact. A repeat request increments `request_count` instead of
    inserting, and that counter is what orders the build queue — so the
    most-wanted artists get built first, for free.
    """
    name = (name or "").strip()[:200]
    if not name:
        return None, "Tell us the artist's name."

    spotify_id, error = parse_spotify_artist_id(spotify_url)
    if error:
        return None, error

    row = db.query_one(
        """
        INSERT INTO issues (type, requested_name, spotify_artist_id)
        VALUES ('artist_request', %s, %s)
        ON CONFLICT (spotify_artist_id)
          WHERE type = 'artist_request' AND spotify_artist_id IS NOT NULL
          DO UPDATE SET request_count = issues.request_count + 1,
                        updated_at = now()
        RETURNING id, request_count
        """,
        (name, spotify_id),
    )
    log.info("artist request %s (count now %s)", spotify_id, row["request_count"])
    return str(row["id"]), None


def record_data_report(
    entity_type: str,
    entity_id: str,
    field: str,
    user_says: str,
    suggested_value: str | None = None,
) -> tuple[bool, str | None]:
    """File a report against one field. Returns (accepted, error).

    Field-level rather than free-text: a report arrives as entity + field +
    what they say, which is actionable in seconds. A general "this profile is
    wrong" box produces "the third song is wrong", which nobody can act on.
    """
    if entity_type not in REPORTABLE_ENTITIES:
        return False, "We can't take a report on that."
    if field not in REPORTABLE_FIELDS:
        return False, "We can't take a report on that field."

    user_says = (user_says or "").strip()[:MAX_FREETEXT]
    if not user_says:
        return False, "Tell us what's wrong so we know what to look at."

    try:
        uuid.UUID(entity_id)
    except (ValueError, TypeError):
        return False, "We couldn't tell which item that report is about."

    db.execute(
        """
        INSERT INTO issues (type, entity_type, entity_id, field, user_says,
                            suggested_value)
        VALUES ('data_report', %s, %s, %s, %s, %s)
        ON CONFLICT (entity_type, entity_id, field)
          WHERE type = 'data_report'
          DO UPDATE SET request_count = issues.request_count + 1,
                        updated_at = now(),
                        -- Keep the first correction offered; a later blank must
                        -- not erase it.
                        suggested_value = COALESCE(EXCLUDED.suggested_value,
                                                   issues.suggested_value)
        """,
        (entity_type, entity_id, field, user_says,
         (suggested_value or "").strip()[:MAX_FREETEXT] or None),
    )
    return True, None


@router.post("/api/request-artist")
def api_request_artist(
    name: str = Form(...),
    spotify_url: str = Form(...),
    website: str = Form(default=""),
) -> dict[str, Any]:
    """JSON form of the request. `website` is the honeypot."""
    if website:
        # Accept and discard. Telling a bot it failed only helps it retry.
        log.info("honeypot tripped on artist request")
        return {"ok": True}

    issue_id, error = record_artist_request(name, spotify_url)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"ok": True, "request_id": issue_id}


@router.post("/api/report")
def api_report(
    entity_type: str = Form(...),
    entity_id: str = Form(...),
    field: str = Form(...),
    user_says: str = Form(...),
    suggested_value: str = Form(default=""),
    website: str = Form(default=""),
) -> dict[str, Any]:
    if website:
        log.info("honeypot tripped on data report")
        return {"ok": True}

    accepted, error = record_data_report(
        entity_type, entity_id, field, user_says, suggested_value
    )
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"ok": accepted}


@router.get("/health", response_class=PlainTextResponse)
def health() -> str:
    """Load balancer health check.

    Deliberately does not touch Postgres: this answers "is this process alive",
    and a database blip should not pull every web server out of rotation at
    once. `db.healthy()` exists for a deeper check if one is ever wanted.
    """
    return "ok"
