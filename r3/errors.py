"""Error surfaces.

Pure helpers, so both the middleware and the exception handlers can decide the
same way whether a caller wants JSON or a page. Kept out of `main.py` to avoid
an import cycle: `ratelimit.py` needs this too, and `main.py` imports both.

The copy follows the same rule as every diagnostic: say what happened, say what
it means, and give a way forward. An error page that only apologises leaves
someone stuck.
"""

from typing import Any

from starlette.requests import Request

# Paths under here are machine-facing and always answer in JSON, whatever the
# browser's Accept header claims.
JSON_PREFIXES = ("/api/",)


def wants_json(request: Request) -> bool:
    """True when the caller is a program rather than a browser."""
    if request.url.path.startswith(JSON_PREFIXES):
        return True
    accept = request.headers.get("accept", "")
    # A browser sends text/html first; fetch() defaults to */* and usually asks
    # for JSON explicitly.
    return "application/json" in accept and "text/html" not in accept


# Status → what the visitor is told. Deliberately free of internals: whether we
# lost the database or a template blew up is our problem, not something to make
# someone else read.
ERROR_COPY: dict[int, dict[str, str]] = {
    400: {
        "title": "That didn't look right",
        "body": "Something in that request didn't make sense to us. Going back and trying again usually sorts it.",
    },
    404: {
        "title": "We don't have that one",
        "body": "There's nothing at this address. It may have moved, or the link may have a typo in it.",
    },
    405: {
        "title": "That didn't work",
        "body": "That action isn't available from here. Try again from the page you started on.",
    },
    # FastAPI's own validation failures. Without an entry here a browser gets
    # the raw `{"detail":[{"type":"string_too_long"...}]}` array.
    422: {
        "title": "That didn't look right",
        "body": "Something you sent us wasn't in a form we could read — usually a field that's too long, or one that didn't get filled in. Going back and trying again usually sorts it.",
    },
    429: {
        "title": "That's a lot of requests",
        "body": "You've made quite a few in a short time, so we've paused for a moment. Wait a few seconds and carry on — nothing is lost.",
    },
    500: {
        "title": "Something broke on our side",
        "body": "That's ours, not yours. We've logged it. Trying again in a moment often works.",
    },
    503: {
        "title": "We're having trouble reaching our records",
        "body": "This is usually brief. Your searches and reports aren't affected — try again shortly.",
    },
}

FALLBACK = {
    "title": "Something went wrong",
    "body": "We couldn't complete that. Trying again in a moment often works.",
}


def error_context(status_code: int, detail: str | None = None) -> dict[str, Any]:
    """Template context for an error page.

    `detail` is shown only when it came from a deliberate `HTTPException` —
    those messages are written for people. Anything else is a crash, and its
    text belongs in the log, not on a page.
    """
    copy = ERROR_COPY.get(status_code, FALLBACK)
    return {
        "status_code": status_code,
        "error_title": copy["title"],
        "error_body": copy["body"],
        "error_detail": detail,
    }
