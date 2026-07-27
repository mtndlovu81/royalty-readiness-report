"""Scoring and flagging.

**Pure.** No database, no HTTP, no file access — assembled dicts in, flags out.
That makes the rules testable in isolation, which matters more here than
anywhere else in the project: wrong flags render perfectly and are simply wrong.

Two principles govern every rule below.

**Severity communicates consequence; framing communicates uncertainty.** A gap
that would cost real money stays red. The copy carries the doubt, because our
source is volunteer-edited and an empty field means "nobody entered it" far more
often than "it doesn't exist".

**Suppress on uncertainty. Never accuse on uncertainty.** Missing `type`, a
group, no writer credits — each one means say nothing. There is deliberately no
green "not a writer" badge, because absence of data is not evidence.

All copy lives in COPY at the top of this module so it stays consistent
site-wide, and every message follows the DESIGN.md §2 formula:

    what we found → why that might be → what it costs if real → what to do
"""

from typing import Any, NamedTuple

# --- shapes ---------------------------------------------------------------

BOTH = "both"
PERFORMER = "performer"
WRITER = "writer"
UNKNOWN = "unknown"

RED = "red"
AMBER = "amber"
GREEN = "green"

# Icon + colour + text, always — never colour alone. Naming the icon here keeps
# a template from rendering severity as a bare colour swatch.
ICONS = {
    RED: "circle-x",
    AMBER: "alert-triangle",
    GREEN: "circle-check",
}


class Flag(NamedTuple):
    severity: str
    field: str
    headline: str
    explanation: str


# --- copy -----------------------------------------------------------------
#
# User-facing strings only: version, song, streaming ID, publishing ID, album,
# performer, contributor. Never the internal vocabulary.

COPY: dict[str, dict[str, str]] = {
    "no_composition": {
        "headline": "No composition record found",
        "explanation": (
            "We couldn't find a composition entry for this song in our sources. "
            "That may mean it was never registered, or just that our sources "
            "don't have it yet. If it isn't registered, there's no publishing ID "
            "and no writer credits for royalties to attach to, so mechanical and "
            "performance income has nothing to pay out against. Your PRO or "
            "publisher can tell you whether a registration exists."
        ),
    },
    "no_publishing_id": {
        "headline": "No publishing ID found",
        "explanation": (
            "We couldn't find one in our sources — that may mean the composition "
            "isn't registered, or just that our sources don't have it yet. If it "
            "isn't registered, mechanical and performance royalties have nothing "
            "to attach to. Check with your PRO."
        ),
    },
    # Same severity, different framing: not this artist's gap to fix, but still
    # their problem, because a sync placement needs both sides clean.
    "no_publishing_id_performer": {
        "headline": "No publishing ID found",
        "explanation": (
            "You didn't write this one, so it isn't yours to register — but we "
            "couldn't find a publishing ID in our sources either. If it's "
            "genuinely unregistered, a sync placement will stall on it: a music "
            "supervisor who hits this gap usually picks a different song rather "
            "than chasing the paperwork. Worth raising with the writers."
        ),
    },
    "no_streaming_id_primary": {
        "headline": "No streaming ID on the main version",
        "explanation": (
            "We couldn't find a streaming ID for the version most likely to be "
            "played. It may never have been issued one, or our sources may simply "
            "not list it. Without one, plays can fail to match to you and the "
            "income sits unallocated until someone claims it. Whoever distributed "
            "this can tell you which code was issued."
        ),
    },
    "no_streaming_id_version": {
        "headline": "No streaming ID on this version",
        "explanation": (
            "This version has no streaming ID in our sources — it may never have "
            "been issued one, or our sources may not list it. Plays of this "
            "particular version may not be matched to you, though your main "
            "version is unaffected. Worth checking with whoever distributed it."
        ),
    },
    "contributor_no_ipi": {
        "headline": "No IPI found for {name}",
        "explanation": (
            "We couldn't find an IPI for this contributor in our sources. They "
            "may hold one that isn't listed there, or may not be registered with "
            "a collection society. If they aren't, societies have no way to route "
            "their share of this song and it can sit unclaimed. Worth confirming "
            "they're registered."
        ),
    },
    "artist_no_ipi": {
        "headline": "No IPI found for this performer",
        "explanation": (
            "We couldn't find an IPI in our sources. You may hold one that isn't "
            "listed there, or you may not be registered with a collection society "
            "yet. Without one, societies have no way to route your writer share, "
            "and unclaimed money is typically reallocated after two to three "
            "years. If you aren't registered with a PRO, that's where to start."
        ),
    },
}


def _flag(severity: str, field: str, key: str, **fmt: Any) -> Flag:
    copy = COPY[key]
    return Flag(
        severity=severity,
        field=field,
        headline=copy["headline"].format(**fmt) if fmt else copy["headline"],
        explanation=copy["explanation"],
    )


# --- gating ---------------------------------------------------------------


def can_hold_ipi(entity: dict[str, Any]) -> bool:
    """IPIs identify people.

    Groups, orchestras, and unclassified artists get no IPI flag. MusicBrainz
    returns `ipis: []` for every band — flagging that would mark each one
    critically broken for something that isn't broken and can't be fixed.

    Applies to **contributors as well as the profile artist**: bands are
    routinely credited as writers on their own songs, and a group credited as
    composer must not throw an amber either.
    """
    return entity.get("type") == "Person"


def artist_shape(has_writer_credits: bool, has_recordings: bool) -> str:
    """Classify what kind of artist this is, from data already fetched.

    Performers are paid through neighbouring rights, tracked by streaming ID,
    not IPI — so flagging a session vocalist for a missing IPI is the same false
    alarm as flagging a band. A writer who doesn't perform has no versions to
    check, so streaming-ID flags are meaningless on their profile.
    """
    if has_writer_credits and has_recordings:
        return BOTH
    if has_recordings:
        return PERFORMER
    if has_writer_credits:
        return WRITER
    return UNKNOWN


# --- evaluation -----------------------------------------------------------


def evaluate_song(song: dict[str, Any], shape: str = BOTH) -> list[Flag]:
    """Flags for one song. Only problems are returned — no flags means green.

    Expects:
        {"iswc": str|None, "work_mbid": str|None,
         "versions": [{"isrc": str|None, "is_primary": bool, "title": str}],
         "contributors": [{"name": str, "type": str|None, "ipis": [str]}]}
    """
    flags: list[Flag] = []

    has_composition = bool(song.get("work_mbid"))

    if not has_composition:
        # One flag for one absence. Emitting missing-publishing-ID and
        # missing-IPI on top would stack three failures on the same fact and
        # make the song look three times as broken as it is.
        flags.append(_flag(RED, "composition", "no_composition"))
    else:
        if not song.get("iswc"):
            key = "no_publishing_id_performer" if shape == PERFORMER else "no_publishing_id"
            flags.append(_flag(RED, "iswc", key))

        for contributor in song.get("contributors") or []:
            if not can_hold_ipi(contributor):
                continue
            if contributor.get("ipis"):
                continue
            flags.append(
                _flag(
                    AMBER,
                    "contributor_ipi",
                    "contributor_no_ipi",
                    name=contributor.get("credited_as") or contributor.get("name") or "this contributor",
                )
            )

    # A writer who doesn't perform has nothing to check here, and "no streaming
    # ID" on a songwriter's profile is nonsense.
    if shape != WRITER:
        for version in song.get("versions") or []:
            if version.get("isrc"):
                continue
            if version.get("is_primary"):
                flags.append(_flag(RED, "isrc", "no_streaming_id_primary"))
            else:
                flags.append(_flag(AMBER, "isrc", "no_streaming_id_version"))

    return flags


def evaluate_artist(artist: dict[str, Any], shape: str = BOTH) -> list[Flag]:
    """Artist-level flags, for the header banner.

    These never become per-song flags. A missing IPI is one fact; repeating it
    on all 47 rows makes the status column stop discriminating between songs.
    """
    flags: list[Flag] = []

    # Every clause here is a suppression: not a person, or no evidence they
    # write, means we say nothing rather than risk accusing.
    if can_hold_ipi(artist) and shape in (BOTH, WRITER) and not artist.get("ipis"):
        flags.append(_flag(RED, "artist_ipi", "artist_no_ipi"))

    return flags


def is_primary_catalogue(song: dict[str, Any]) -> bool:
    """Whether a song belongs to the primary catalogue.

    Defaults to True: a song we know nothing about counts, because excluding it
    on missing data would quietly shrink the number the artist is judged on.
    """
    return song.get("is_primary_catalogue", True) is not False


def headline(songs: list[dict[str, Any]], shape: str = BOTH) -> tuple[int, int]:
    """(songs needing attention, total songs) — primary catalogue only.

    Live takes, compilation cuts and bootleg tracks are excluded from the count
    but not from the page: they render below the table with the same
    diagnostics. Counting them would drown the number in filler an artist would
    not call part of their catalogue — a twelve-second applause clip flagged red
    tells them the tool doesn't understand their work, and the honest flags
    beside it stop being believed.

    Returns (0, 0) for an empty or entirely secondary catalogue — the caller
    renders a proportion and must not divide by zero.
    """
    primary = [song for song in songs if is_primary_catalogue(song)]
    if not primary:
        return (0, 0)
    needs_attention = sum(1 for song in primary if evaluate_song(song, shape))
    return (needs_attention, len(primary))


def worst_severity(flags: list[Flag]) -> str:
    """The severity a row should show. Green when there is nothing to report."""
    if any(f.severity == RED for f in flags):
        return RED
    if any(f.severity == AMBER for f in flags):
        return AMBER
    return GREEN
