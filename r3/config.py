"""Environment loading and constants.

Importing this module reads `.env` from the repository root and validates
anything that would fail confusingly later. It never touches the database or
the network, so every other module can import it freely.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


DATABASE_URL = _require("DATABASE_URL")

# MusicBrainz requires a descriptive User-Agent with working contact details.
# Requests without one are blocked, so refuse to start on a placeholder rather
# than discovering it as a wall of 403s mid-build.
MB_USER_AGENT = _require("MB_USER_AGENT")

_PLACEHOLDERS = ("your-contact-here", "your-email@example.com", "CHANGE_ME")
if any(p in MB_USER_AGENT for p in _PLACEHOLDERS):
    raise ConfigError(
        "MB_USER_AGENT still contains a placeholder contact. MusicBrainz blocks "
        "requests without real contact details — set it to something reachable."
    )

# HTTPS only. The MusicBrainz docs contain http:// examples that redirect,
# which costs an extra round trip on every call when you get one per second.
MB_BASE_URL = "https://musicbrainz.org/ws/2"

# One request per second is the published limit and a hard rule for this
# project. A misconfigured .env must not be able to lower it.
MB_RATE_LIMIT_FLOOR_SECONDS = 1.0

try:
    _requested_rate = float(os.getenv("MB_RATE_LIMIT_SECONDS") or MB_RATE_LIMIT_FLOOR_SECONDS)
except ValueError as exc:
    raise ConfigError("MB_RATE_LIMIT_SECONDS must be a number, e.g. 1.0") from exc

MB_RATE_LIMIT_SECONDS = max(_requested_rate, MB_RATE_LIMIT_FLOOR_SECONDS)
if _requested_rate < MB_RATE_LIMIT_FLOOR_SECONDS:
    log.warning(
        "MB_RATE_LIMIT_SECONDS=%s is below the %ss floor; using %ss.",
        _requested_rate,
        MB_RATE_LIMIT_FLOOR_SECONDS,
        MB_RATE_LIMIT_SECONDS,
    )

try:
    STALE_AFTER_DAYS = int(os.getenv("STALE_AFTER_DAYS") or 60)
except ValueError as exc:
    raise ConfigError("STALE_AFTER_DAYS must be a whole number of days") from exc

# True on Web01 only. A second worker doubles the outbound rate to MusicBrainz
# and gets the app blocked. This is the second line of defence behind the
# systemd unit, not the first.
RUN_WORKER = _flag("RUN_WORKER", default=False)

# True only when the app really is behind the load balancer. The rate limiter
# keys on X-Forwarded-For when set, and on the socket peer when not. Getting it
# wrong fails badly in both directions: on, when exposed directly, lets anyone
# forge an identity per request and evade limiting entirely; off, when behind
# nginx, puts every visitor in one bucket and throttles the whole site.
TRUST_PROXY_HEADERS = _flag("TRUST_PROXY_HEADERS", default=False)

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "info").strip().upper()

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def configure_logging() -> None:
    """Apply LOG_LEVEL. Called by entrypoints, not on import."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
