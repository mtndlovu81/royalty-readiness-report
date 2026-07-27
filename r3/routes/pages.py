"""Server-rendered HTML routes.

Everything is rendered server-side: pages work with JavaScript disabled, links
preview correctly, and the whole thing is crawlable. `app.js` only manipulates
what is already on the page.
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from r3 import db, diagnostics
from r3.routes.api import MAX_QUERY_LENGTH, build_status, run_search

log = logging.getLogger(__name__)

router = APIRouter()

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Autoescaping is on by default here and must stay on. Nothing from a user or
# from MusicBrainz is ever marked safe.
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"autofocus": True})


@router.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = Query(default="", max_length=MAX_QUERY_LENGTH)):
    """Results page. Shares `run_search` with the JSON route.

    One implementation, so the HTML and the API can never disagree about who
    is in the catalogue — and so the upstream fallback is rate-limited and
    cached identically for both.
    """
    outcome = run_search(q)
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": outcome["query"],
            "results": outcome["results"],
            "source": outcome["source"],
            "label": "Artist name",
        },
    )


# The roles we surface, in the order a credit list reads.
ROLE_ORDER = ["composer", "lyricist", "writer", "producer"]


def load_profile(slug: str) -> dict[str, Any] | None:
    """Assemble one artist's profile. Postgres only — never calls upstream.

    Four queries regardless of catalogue size: artist, songs, versions,
    credits. Assembling in Python beats a join that multiplies songs by
    versions by contributors and then has to be unpicked.
    """
    artist = db.query_one(
        """
        SELECT id, slug, name, type, country, disambiguation, ipis, status,
               verified_at, last_checked_at
          FROM artists WHERE slug = %s
        """,
        (slug,),
    )
    if artist is None:
        return None

    songs = db.query(
        """
        SELECT id, slug, title, iswc, work_mbid, is_primary_catalogue, verified_at
          FROM songs WHERE artist_id = %s ORDER BY title
        """,
        (artist["id"],),
    )
    song_ids = [s["id"] for s in songs]

    versions = db.query(
        """
        SELECT song_id, title, isrc, is_primary, first_released, length_ms, verified_at
          FROM versions WHERE song_id = ANY(%s)
         ORDER BY is_primary DESC, first_released NULLS LAST, title
        """,
        (song_ids,),
    ) if song_ids else []

    credits = db.query(
        """
        SELECT sc.song_id, sc.role, sc.credited_as, c.name, c.type, c.ipis
          FROM song_contributors sc
          JOIN contributors c ON c.id = sc.contributor_id
         WHERE sc.song_id = ANY(%s)
         ORDER BY c.name
        """,
        (song_ids,),
    ) if song_ids else []

    by_song_versions: dict[Any, list] = {}
    by_song_credits: dict[Any, list] = {}
    for v in versions:
        by_song_versions.setdefault(v["song_id"], []).append(v)
    for c in credits:
        by_song_credits.setdefault(c["song_id"], []).append(c)

    shape = diagnostics.artist_shape(
        has_writer_credits=bool(credits),
        has_recordings=bool(versions),
    )

    for song in songs:
        song["versions"] = by_song_versions.get(song["id"], [])
        song["contributors"] = by_song_credits.get(song["id"], [])
        song["flags"] = diagnostics.evaluate_song(song, shape)
        song["severity"] = diagnostics.worst_severity(song["flags"])
        song["primary_version"] = next(
            (v for v in song["versions"] if v["is_primary"]),
            song["versions"][0] if song["versions"] else None,
        )
        # Grouped by role, the way a credit list reads.
        grouped: dict[str, list] = {}
        for credit in song["contributors"]:
            grouped.setdefault(credit["role"], []).append(credit)
        song["credits_by_role"] = [
            (role, grouped[role])
            for role in ROLE_ORDER + sorted(set(grouped) - set(ROLE_ORDER))
            if role in grouped
        ]

    return {
        "artist": artist,
        "shape": shape,
        "artist_flags": diagnostics.evaluate_artist(artist, shape),
        "headline": diagnostics.headline(songs, shape),
        "primary_songs": [s for s in songs if s["is_primary_catalogue"]],
        "secondary_songs": [s for s in songs if not s["is_primary_catalogue"]],
    }


MBID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@router.post("/artist/build")
def build_artist(mbid: str = Form(...)):
    """Queue a build and send the visitor to its status page.

    POST rather than a link on purpose: a crawler following a build link would
    queue one build per search result, each a minute of upstream traffic at one
    request per second.

    This does **not** create the artist row. The slug is minted by the worker
    from the name MusicBrainz returns, not from anything the form supplied —
    slugs are permanent public URLs, and one minted from client input could be
    made to say anything.
    """
    mbid = (mbid or "").strip()
    if not MBID_PATTERN.match(mbid):
        raise HTTPException(status_code=400, detail="That isn't a valid artist reference")

    # Already built — nothing to queue.
    existing = db.query_one(
        "SELECT slug FROM artists WHERE mbid = %s AND status = 'published'", (mbid,)
    )
    if existing:
        return RedirectResponse(f"/artist/{existing['slug']}", status_code=303)

    row = db.query_one(
        """
        INSERT INTO build_queue (artist_mbid) VALUES (%s)
        ON CONFLICT (artist_mbid) DO UPDATE
           SET status = CASE WHEN build_queue.status = 'failed'
                             THEN 'queued' ELSE build_queue.status END
        RETURNING id
        """,
        (mbid,),
    )
    # 303 so a refresh of the status page doesn't re-post the form.
    return RedirectResponse(f"/pending/{row['id']}", status_code=303)


@router.get("/pending/{request_id}", response_class=HTMLResponse)
def pending(request: Request, request_id: str):
    """Private status page for a queued build. Never indexed."""
    try:
        uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such request") from None

    job = db.query_one(
        "SELECT id, artist_mbid, status, error FROM build_queue WHERE id = %s",
        (request_id,),
    )
    if job is None:
        raise HTTPException(status_code=404, detail="No such request")

    # Finished — hand over to the real profile.
    artist = db.query_one(
        "SELECT slug FROM artists WHERE mbid = %s AND status = 'published'",
        (job["artist_mbid"],),
    )
    if artist:
        return RedirectResponse(f"/artist/{artist['slug']}", status_code=303)

    status = build_status(request_id)
    if status and status["stalled"]:
        # The page tells the visitor only what they can act on. The cause, and
        # the fix, belong here — where whoever runs this will see them.
        log.warning(
            "build %s has waited %ss with no worker activity — is the worker "
            "running? (RUN_WORKER=true python scripts/worker.py)",
            request_id,
            status["waiting_seconds"],
        )

    return templates.TemplateResponse(
        request, "pending.html", {"job": job, "status": status}
    )


@router.get("/artist/{slug}", response_class=HTMLResponse)
def artist_profile(request: Request, slug: str):
    """The core of the product. Postgres only."""
    profile = load_profile(slug)
    if profile is None:
        raise HTTPException(status_code=404, detail="No profile for that name")

    return templates.TemplateResponse(request, "artist.html", profile)
