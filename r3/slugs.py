"""Slug generation and collision handling.

Slugs are immutable once minted (DESIGN.md §7) — they are the public URL, and a
rename breaks every shared link. So this module is deliberately boring and
deterministic, and it is pure: no database, no config, nothing to mock.

**Not the same job as `r3_normalize()` in schema.sql.** That folds a *search
term* to match against names; this mints a *permanent identifier*. They fold
similarly on purpose, but they are allowed to diverge and neither should be
implemented in terms of the other — a search tweak must never be able to change
a URL that already exists in the wild.
"""

import re
import unicodedata

# Long enough for real titles, short enough to keep URLs readable.
MAX_LENGTH = 80

# Used when a name strips to nothing — "!!!", or a title written entirely in a
# script that transliterates to no ASCII. An empty slug is a broken route, so
# there must always be something. Collisions get disambiguated as usual.
FALLBACK = "untitled"

# NFKD decomposition strips combining accents (é → e) but leaves these alone:
# they are distinct letters, not decorated ones. Without the map, "Sigur Rós"
# folds cleanly while "Møster" keeps a character no URL should carry.
_TRANSLITERATIONS = {
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ß": "ss",
    "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "TH",
    "ł": "l", "Ł": "L",
    "ı": "i",
    "ħ": "h", "Ħ": "H",
    "ŋ": "n", "Ŋ": "N",
}


def _fold(text: str) -> str:
    """Reduce to ASCII letters and digits as far as sensibly possible."""
    expanded = "".join(_TRANSLITERATIONS.get(ch, ch) for ch in text)
    decomposed = unicodedata.normalize("NFKD", expanded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def slugify(text: str, max_length: int = MAX_LENGTH) -> str:
    """Fold to a lowercase, hyphen-separated ASCII slug.

    Always returns something non-empty and safe in a path segment: no slashes,
    no leading or trailing hyphens, no runs of hyphens.
    """
    folded = _fold(text or "").lower()

    # Any run of non-alphanumerics becomes a single hyphen, which collapses
    # "AC/DC" to "ac-dc" and "  Spaces  " to "spaces" in one pass.
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")

    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")

    return slug or FALLBACK


def unique_slug(base: str, taken: set[str], max_length: int = MAX_LENGTH) -> str:
    """Return `base`, or `base-2`, `base-3`… if it is already spoken for.

    `taken` is the set of slugs already minted in the relevant scope — every
    artist slug site-wide, or every song slug within one artist. Pure: the
    caller does the lookup and owns the transaction.
    """
    if base not in taken:
        return base

    suffix = 2
    while True:
        tail = f"-{suffix}"
        # Truncate the stem rather than the suffix, or the disambiguator is
        # the thing that gets cut off and the slug collides all over again.
        stem = base[: max_length - len(tail)].rstrip("-") or FALLBACK
        candidate = f"{stem}{tail}"
        if candidate not in taken:
            return candidate
        suffix += 1


def mint(text: str, taken: set[str], max_length: int = MAX_LENGTH) -> str:
    """Slugify and disambiguate in one step. Does not mutate `taken`."""
    return unique_slug(slugify(text, max_length), taken, max_length)
