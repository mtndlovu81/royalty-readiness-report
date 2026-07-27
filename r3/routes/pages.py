"""Server-rendered HTML routes.

Everything is rendered server-side: pages work with JavaScript disabled, links
preview correctly, and the whole thing is crawlable. `app.js` only manipulates
what is already on the page.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from r3.routes.api import MAX_QUERY_LENGTH, run_search

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
