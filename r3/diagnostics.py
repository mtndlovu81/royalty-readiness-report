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

# Not a verdict — "we have nothing here". Distinct from green (we looked and
# found it) and from amber (we looked and it's a gap). Absence of data is not
# evidence, so it must not escalate a row's status or count against an artist;
# it exists so a category can stay on the page without claiming anything. Same
# greyed "Not on record" language the table cells already use.
NEUTRAL = "neutral"

# Icon + colour + text, always — never colour alone. Naming the icon here keeps
# a template from rendering severity as a bare colour swatch.
ICONS = {
    RED: "circle-x",
    AMBER: "alert-triangle",
    GREEN: "circle-check",
    NEUTRAL: "circle-dash",
}

# Two label sets, because the same severity answers two different questions.
#
# In the status column it answers "what's the state of this song?" — so the
# problem states use action language. The clear states deliberately do NOT:
# they describe what we found rather than congratulating, because we have not
# checked anyone's registration and must not sound like we have.
#
# "Complete", "Verified", "All good" are all unusable here. They overclaim — a
# present identifier means a code exists in our sources, not that the
# registration behind it is right — and they collide with the hollow/solid icon
# system, which reserves the idea of verification for when a human has actually
# confirmed something.
STATUS_LABELS = {
    RED: "Needs attention",
    AMBER: "Worth checking",
    GREEN: "Nothing missing",
    # The same phrase every empty table cell uses, so absence reads identically
    # wherever it appears.
    NEUTRAL: "Not on record",
}

# Beside a category name it answers "what's the state of this check?", and has
# to read naturally after the label: "Publishing ID: not found". Mostly heard
# rather than seen — these are the accessible names for the category icons.
CHECK_LABELS = {
    RED: "not found",
    AMBER: "incomplete",
    GREEN: "found",
    NEUTRAL: "not on record",
}


class Flag(NamedTuple):
    severity: str
    field: str
    headline: str
    explanation: str


class Check(NamedTuple):
    """One category of the report, present whether or not anything is wrong.

    Flags say what's broken; checks say what was looked at. A panel that only
    ever lists failures reads as an accusation and leaves the artist unable to
    tell "we checked and found it" from "we never checked".
    """

    key: str
    label: str
    severity: str
    summary: str        # the finding in a few words, or the value we found
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
    # The category-level view. The per-version message above is written about a
    # single version and reads wrong as a summary of several.
    "versions_partial": {
        "headline": "Some versions have no streaming ID",
        "explanation": (
            "Not every version of this song has its own streaming ID in our "
            "sources. They may never have been issued one, or our sources may "
            "not list them. Plays of those versions can fail to match back to "
            "you even when the main one is fine, which quietly splits what you're "
            "owed. Whoever distributed them can confirm which codes exist."
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


# Copy for the states where we DID find something. These carry their own
# caution: finding an identifier proves a code exists in our sources, not that
# the registration behind it is correct or that the splits are right. A green
# tick that reads as "your publishing is sorted" is the same size of error as a
# red one that reads as "you are unregistered".
FOUND_COPY: dict[str, str] = {
    "publishing_id": (
        "A publishing ID is on record for this song. That means the composition "
        "has been registered somewhere our sources can see — it doesn't confirm "
        "the writer splits behind it are right, which only your PRO can tell you."
    ),
    "streaming_id": (
        "A streaming ID is on record for the main version, so plays of it can be "
        "matched back to you. It doesn't confirm the recording is credited to the "
        "right people, only that the code exists."
    ),
    "versions_all": (
        "Every version we know about has its own streaming ID, so plays of each "
        "can be told apart and matched."
    ),
    "contributors_all": (
        "Everyone credited on this song who can hold an IPI has one on record, so "
        "societies have something to route each share to."
    ),
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


def song_checks(song: dict[str, Any], shape: str = BOTH) -> list[Check]:
    """Every category we look at, in a fixed order, found or not.

    Complements `evaluate_song`, which returns only problems and is what the
    headline counts. This is what the expanded row renders: the same categories
    appear on every song, so a reader learns the shape once and can then scan
    it, and a green line is a statement that we looked.
    """
    checks: list[Check] = []
    has_composition = bool(song.get("work_mbid"))
    versions = song.get("versions") or []
    primary = next((v for v in versions if v.get("is_primary")), versions[0] if versions else None)

    # --- publishing -------------------------------------------------------
    if not has_composition:
        checks.append(Check(
            "publishing_id", "Publishing ID", RED,
            "No composition record",
            COPY["no_composition"]["explanation"],
        ))
    elif song.get("iswc"):
        checks.append(Check(
            "publishing_id", "Publishing ID", GREEN,
            song["iswc"],
            FOUND_COPY["publishing_id"],
        ))
    else:
        key = "no_publishing_id_performer" if shape == PERFORMER else "no_publishing_id"
        checks.append(Check(
            "publishing_id", "Publishing ID", RED,
            "Not on record",
            COPY[key]["explanation"],
        ))

    # --- streaming --------------------------------------------------------
    # A writer who doesn't perform has no versions to check, and the category
    # would be noise on their profile rather than information.
    if shape != WRITER and primary is not None:
        if primary.get("isrc"):
            checks.append(Check(
                "streaming_id", "Streaming ID", GREEN,
                primary["isrc"],
                FOUND_COPY["streaming_id"],
            ))
        else:
            checks.append(Check(
                "streaming_id", "Streaming ID", RED,
                "Not on record",
                COPY["no_streaming_id_primary"]["explanation"],
            ))

        # --- other versions ----------------------------------------------
        if len(versions) > 1:
            with_id = sum(1 for v in versions if v.get("isrc"))
            total = len(versions)
            if with_id == total:
                checks.append(Check(
                    "versions", "Other versions", GREEN,
                    f"All {total} versions have a streaming ID",
                    FOUND_COPY["versions_all"],
                ))
            else:
                checks.append(Check(
                    "versions", "Other versions", AMBER,
                    f"{with_id} of {total} versions have a streaming ID",
                    COPY["versions_partial"]["explanation"],
                ))

    # --- contributors -----------------------------------------------------
    if has_composition:
        contributors = song.get("contributors") or []
        if not contributors:
            # Neutral, not amber: nobody credited in our sources is far more
            # often nobody having added them than nobody existing. Flagging it
            # would accuse on an absence, and would also make this row's icon
            # disagree with the status the table shows.
            checks.append(Check(
                "contributors", "Contributors", NEUTRAL,
                "Not on record",
                "We couldn't find anyone credited as a writer on this song — most "
                "often that means nobody has added the credits to our sources yet, "
                "rather than that nobody holds them. If the registration is also "
                "missing them, there's nobody for a society to route a writer's "
                "share to. Worth checking who's listed on the registration.",
            ))
        else:
            # Groups legitimately hold no IPI, so they aren't counted as gaps.
            eligible = [c for c in contributors if can_hold_ipi(c)]
            missing = [c for c in eligible if not c.get("ipis")]
            named = len({c.get("credited_as") or c.get("name") for c in contributors})

            if not eligible or not missing:
                checks.append(Check(
                    "contributors", "Contributors", GREEN,
                    f"{named} credited" + (", all with an IPI" if eligible else ""),
                    FOUND_COPY["contributors_all"],
                ))
            else:
                checks.append(Check(
                    "contributors", "Contributors", AMBER,
                    f"{len(eligible) - len(missing)} of {len(eligible)} have an IPI",
                    COPY["contributor_no_ipi"]["explanation"],
                ))

    return checks


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


def worst_severity(items) -> str:
    """The severity a row should show. Green when there is nothing to report.

    Accepts flags or checks. NEUTRAL never escalates — "we have no data" is not
    a finding against anyone, and letting it would put a warning on the row for
    something we explicitly decline to assert.
    """
    if any(i.severity == RED for i in items):
        return RED
    if any(i.severity == AMBER for i in items):
        return AMBER
    return GREEN
