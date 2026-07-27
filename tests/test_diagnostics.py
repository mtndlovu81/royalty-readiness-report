"""Flag rules — TESTS.md §2.

The highest-value test file in the project. The flag rules have already
produced two bugs in design review alone (the group IPI flag, artist shape),
and the failure mode is silent: wrong flags render perfectly, they are just
wrong. Nobody notices a red flag that should not be there until an artist sees
their own band marked critically broken for something unfixable.

`diagnostics.py` is pure, so these are plain dicts in and flags out — no
mocking, no database, no fixtures.
"""

import re

import pytest

from r3 import builder as b
from r3 import diagnostics as d


def artist(**kwargs):
    return {"name": "Test Artist", "type": "Person", "ipis": [], **kwargs}


def song(**kwargs):
    base = {
        "title": "Test Song",
        "work_mbid": "work-1",
        "iswc": "T-123.456.789-0",
        "versions": [{"title": "Test Song", "isrc": "GBAYE0000001", "is_primary": True}],
        "contributors": [],
    }
    return {**base, **kwargs}


def fields(flags):
    return sorted(f.field for f in flags)


def severities(flags):
    return sorted(f.severity for f in flags)


# --- artist-level IPI -----------------------------------------------------


@pytest.mark.parametrize(
    "type_,ipis,expected_flags",
    [
        ("Group", [], 0),                       # bands have no IPI; correct, not broken
        ("Person", ["00055405100"], 0),
        ("Person", [], 1),                      # the real gap
        ("Person", ["a", "b", "c"], 0),         # several societies is normal
        (None, [], 0),                          # suppress on uncertainty
        ("Orchestra", [], 0),
        ("Choir", [], 0),
        ("Character", [], 0),
    ],
)
def test_artist_ipi_gating(type_, ipis, expected_flags):
    flags = d.evaluate_artist(artist(type=type_, ipis=ipis), d.BOTH)
    assert len(flags) == expected_flags


def test_group_never_gets_an_ipi_flag_in_any_shape():
    """The bug this rule exists to prevent — a band marked critically broken."""
    for shape in (d.BOTH, d.PERFORMER, d.WRITER, d.UNKNOWN):
        assert d.evaluate_artist(artist(type="Group", ipis=[]), shape) == []


def test_can_hold_ipi_applies_to_contributors_too():
    """Bands are routinely credited as writers on their own songs."""
    assert d.can_hold_ipi({"type": "Person"}) is True
    assert d.can_hold_ipi({"type": "Group"}) is False
    assert d.can_hold_ipi({"type": None}) is False
    assert d.can_hold_ipi({}) is False


# --- artist shape ---------------------------------------------------------


@pytest.mark.parametrize(
    "writes,performs,expected",
    [
        (True, True, d.BOTH),
        (False, True, d.PERFORMER),
        (True, False, d.WRITER),
        (False, False, d.UNKNOWN),
    ],
)
def test_artist_shape(writes, performs, expected):
    assert d.artist_shape(writes, performs) == expected


@pytest.mark.parametrize(
    "shape,expected_flags",
    [
        (d.BOTH, 1),
        (d.WRITER, 1),
        (d.PERFORMER, 0),   # paid via neighbouring rights, not IPI
        (d.UNKNOWN, 0),
    ],
)
def test_artist_ipi_flag_by_shape(shape, expected_flags):
    flags = d.evaluate_artist(artist(type="Person", ipis=[]), shape)
    assert len(flags) == expected_flags


def test_writer_shape_suppresses_streaming_id_flags():
    """No versions to check — "no streaming ID" on a songwriter is nonsense."""
    subject = song(versions=[{"title": "x", "isrc": None, "is_primary": True}])
    assert "isrc" not in fields(d.evaluate_song(subject, d.WRITER))
    assert "isrc" in fields(d.evaluate_song(subject, d.BOTH))


def test_performer_shape_keeps_streaming_id_flags():
    subject = song(versions=[{"title": "x", "isrc": None, "is_primary": True}])
    assert "isrc" in fields(d.evaluate_song(subject, d.PERFORMER))


def test_publishing_flags_stay_visible_for_performers():
    """Not their gap to fix, but a sync placement needs both sides clean."""
    flags = d.evaluate_song(song(iswc=None), d.PERFORMER)
    publishing = [f for f in flags if f.field == "iswc"]

    assert len(publishing) == 1
    assert publishing[0].severity == d.RED, "severity carries consequence, not blame"
    # Reframed, not softened.
    assert "raising with the writers" in publishing[0].explanation
    assert "didn't write this one" in publishing[0].explanation


def test_performer_framing_differs_from_writer_framing():
    as_performer = d.evaluate_song(song(iswc=None), d.PERFORMER)[0]
    as_writer = d.evaluate_song(song(iswc=None), d.BOTH)[0]

    assert as_performer.severity == as_writer.severity == d.RED
    assert as_performer.explanation != as_writer.explanation


# --- song-level -----------------------------------------------------------


def test_no_composition_record_is_exactly_one_flag():
    """Not three. One absence, one flag."""
    flags = d.evaluate_song(
        song(
            work_mbid=None,
            iswc=None,
            contributors=[{"name": "A Writer", "type": "Person", "ipis": []}],
        )
    )
    assert len(flags) == 1
    assert flags[0].field == "composition"
    assert flags[0].severity == d.RED
    assert "iswc" not in fields(flags)
    assert "contributor_ipi" not in fields(flags)


def test_no_composition_record_still_checks_versions():
    """A missing composition says nothing about whether versions have IDs."""
    flags = d.evaluate_song(
        song(work_mbid=None, versions=[{"title": "x", "isrc": None, "is_primary": True}])
    )
    assert fields(flags) == ["composition", "isrc"]


def test_primary_version_without_streaming_id_is_red():
    flags = d.evaluate_song(song(versions=[{"title": "x", "isrc": None, "is_primary": True}]))
    assert [f.severity for f in flags] == [d.RED]


def test_non_primary_version_without_streaming_id_is_amber():
    flags = d.evaluate_song(
        song(
            versions=[
                {"title": "x", "isrc": "GBAYE0000001", "is_primary": True},
                {"title": "x (live)", "isrc": None, "is_primary": False},
            ]
        )
    )
    assert [f.severity for f in flags] == [d.AMBER]


def test_missing_publishing_id_is_red():
    flags = d.evaluate_song(song(iswc=None))
    assert [(f.severity, f.field) for f in flags] == [(d.RED, "iswc")]


def test_contributor_without_ipi_is_amber():
    flags = d.evaluate_song(
        song(contributors=[{"name": "A Writer", "type": "Person", "ipis": []}])
    )
    assert [(f.severity, f.field) for f in flags] == [(d.AMBER, "contributor_ipi")]
    assert "A Writer" in flags[0].headline


def test_one_amber_per_contributor():
    flags = d.evaluate_song(
        song(
            contributors=[
                {"name": "First Writer", "type": "Person", "ipis": []},
                {"name": "Second Writer", "type": "Person", "ipis": []},
            ]
        )
    )
    assert len(flags) == 2
    assert severities(flags) == [d.AMBER, d.AMBER]
    assert {"First Writer", "Second Writer"} == {f.headline.split("for ")[1] for f in flags}


def test_group_contributor_gets_no_ipi_flag():
    """A band credited as composer must not throw amber — same rule, both tables."""
    flags = d.evaluate_song(
        song(contributors=[{"name": "Radiohead", "type": "Group", "ipis": []}])
    )
    assert flags == []


def test_contributor_with_unknown_type_gets_no_flag():
    flags = d.evaluate_song(
        song(contributors=[{"name": "Someone", "type": None, "ipis": []}])
    )
    assert flags == []


def test_contributor_uses_credited_name():
    flags = d.evaluate_song(
        song(
            contributors=[
                {"name": "Primary Name", "credited_as": "Credited Name",
                 "type": "Person", "ipis": []}
            ]
        )
    )
    assert "Credited Name" in flags[0].headline
    assert "Primary Name" not in flags[0].headline


def test_everything_present_produces_no_flags():
    flags = d.evaluate_song(
        song(contributors=[{"name": "A Writer", "type": "Person", "ipis": ["00055405100"]}])
    )
    assert flags == []
    assert d.worst_severity(flags) == d.GREEN


# --- headline -------------------------------------------------------------


def test_headline_counts_songs_needing_attention():
    songs = [song() for _ in range(6)] + [song(iswc=None) for _ in range(4)]
    assert d.headline(songs) == (4, 10)


def test_headline_with_nothing_flagged():
    assert d.headline([song() for _ in range(10)]) == (0, 10)


def test_headline_empty_list_does_not_divide_by_zero():
    assert d.headline([]) == (0, 0)


def test_headline_counts_primary_catalogue_only():
    """Live and compilation filler is excluded from the count, not the page."""
    songs = [
        song(iswc=None, is_primary_catalogue=True),
        song(iswc=None, is_primary_catalogue=True),
        song(iswc=None, is_primary_catalogue=False),
        song(iswc=None, is_primary_catalogue=False),
        song(is_primary_catalogue=True),
    ]
    assert d.headline(songs) == (2, 3)


def test_all_secondary_catalogue_is_zero_of_zero():
    """Not an error, and not a division by zero in the caller."""
    songs = [song(iswc=None, is_primary_catalogue=False) for _ in range(5)]
    assert d.headline(songs) == (0, 0)


def test_secondary_songs_still_get_their_flags():
    """Excluded from the count, not from diagnostics."""
    secondary = song(iswc=None, is_primary_catalogue=False)
    flags = d.evaluate_song(secondary)

    assert [f.field for f in flags] == ["iswc"]
    assert d.worst_severity(flags) == d.RED


def test_tier_defaults_to_primary_when_unknown():
    """Missing data must not quietly shrink the number the artist is judged on."""
    assert d.is_primary_catalogue({}) is True
    assert d.is_primary_catalogue({"is_primary_catalogue": True}) is True
    assert d.is_primary_catalogue({"is_primary_catalogue": False}) is False


def test_headline_counts_songs_not_flags():
    """A song with four problems is still one song needing attention."""
    messy = song(
        iswc=None,
        versions=[
            {"title": "x", "isrc": None, "is_primary": True},
            {"title": "y", "isrc": None, "is_primary": False},
        ],
        contributors=[{"name": "W", "type": "Person", "ipis": []}],
    )
    assert len(d.evaluate_song(messy)) > 1
    assert d.headline([messy]) == (1, 1)


# --- catalogue tier derivation --------------------------------------------
#
# The rule lives in builder.group_songs(), which is a pure function over plain
# dicts — no API, no fixtures — so it is tested here rather than left uncovered
# under TESTS.md's general "don't test builder.py". The exclusion there is about
# the fetch layer needing a live API, which this does not.


def album(mbid, secondary_types=()):
    return b.Album(
        release_group_mbid=mbid,
        title=f"Album {mbid}",
        primary_type="Album",
        secondary_types=list(secondary_types),
        first_released="2000-01-01",
        canonical_release_mbid=f"rel-{mbid}",
        canonical_track_count=10,
        release_count=1,
    )


def recording(mbid, group_mbid):
    return b.Recording(
        recording_mbid=mbid,
        title="A Song",
        length_ms=200_000,
        isrcs=["GBAYE0000001"],
        artist_credit=[],
        work_rels=[{"work": {"id": "work-1", "title": "A Song"}}],
        release_group_mbid=group_mbid,
    )


ALBUMS = [
    album("studio"),
    album("live-1", ["Live"]),
    album("comp-1", ["Compilation"]),
    album("comp-2", ["Compilation"]),
    album("comp-3", ["Compilation"]),
    album("boot-1", ["Compilation", "Bootleg"]),
]


@pytest.mark.parametrize(
    "appears_on,expected_primary",
    [
        (["studio"], True),
        (["live-1"], False),
        # Earned primary on the studio album; later compilations say nothing.
        (["studio", "comp-1", "comp-2", "comp-3"], True),
        (["comp-1", "boot-1"], False),
        (["live-1", "comp-1"], False),
    ],
    ids=["studio-only", "live-only", "studio-plus-three-comps",
         "comp-and-bootleg", "live-and-comp"],
)
def test_catalogue_tier_is_decided_per_song(appears_on, expected_primary):
    recordings = [recording(f"rec-{g}", g) for g in appears_on]
    songs = b.group_songs(recordings, works=[], albums=ALBUMS)

    assert len(songs) == 1
    assert songs[0].is_primary_catalogue is expected_primary


def test_tier_is_not_decided_by_majority():
    """One studio appearance outweighs any number of compilation appearances."""
    recordings = [recording(f"rec-{g}", g)
                  for g in ("comp-1", "comp-2", "comp-3", "boot-1", "studio")]
    songs = b.group_songs(recordings, works=[], albums=ALBUMS)

    assert songs[0].is_primary_catalogue is True


# --- copy -----------------------------------------------------------------

# Matched on word boundaries, not as bare substrings: this has to catch
# "works" and "released" while leaving "paperwork" and "network" alone. A
# substring check flags those and the usual response is to weaken the test.
FORBIDDEN_TERMS = re.compile(r"\b(recording|work|release)\w*", re.IGNORECASE)


@pytest.mark.parametrize("key", sorted(d.COPY))
def test_copy_is_present_and_uses_user_facing_terms(key):
    """No internal vocabulary reaches a user. Never "recording", "work", "release"."""
    entry = d.COPY[key]
    headline_text, explanation = entry["headline"], entry["explanation"]

    assert headline_text.strip(), key
    assert explanation.strip(), key

    leak = FORBIDDEN_TERMS.search(f"{headline_text} {explanation}")
    assert leak is None, f"{key} leaks the internal term {leak.group(0)!r}" if leak else ""


@pytest.mark.parametrize("leaky", ["no recordings found", "the work", "released in 1994",
                                   "these works", "a recording of"])
def test_terminology_pattern_catches_real_leaks(leaky):
    """The other direction.

    A pattern broken into matching nothing would make the test above pass
    silently forever, which is worse than no test at all.
    """
    assert FORBIDDEN_TERMS.search(leaky) is not None


@pytest.mark.parametrize("innocent", ["the paperwork", "a network", "worth raising",
                                      "coursework", "wordless"])
def test_terminology_pattern_ignores_words_that_merely_contain_them(innocent):
    """A test that cries wolf gets weakened or deleted."""
    assert FORBIDDEN_TERMS.search(innocent) is None


@pytest.mark.parametrize("key", sorted(d.COPY))
def test_copy_follows_the_four_part_formula(key):
    """what we found → why that might be → what it costs → what to do.

    Checked loosely: the hedge that carries the uncertainty, and enough length
    to have said all four things. An assertive one-liner fails both.
    """
    explanation = d.COPY[key]["explanation"].lower()
    assert any(h in explanation for h in ("may ", "might ", "couldn't find")), key
    assert len(explanation) > 120, key


def test_no_message_asserts_the_user_is_unregistered():
    """We report what we found, never a fact about someone's registration."""
    forbidden = ["you are not registered", "you have not registered",
                 "is unregistered", "are frozen", "you failed"]
    for key, entry in d.COPY.items():
        text = entry["explanation"].lower()
        for phrase in forbidden:
            assert phrase not in text, f"{key} asserts: {phrase!r}"


def test_every_severity_has_an_icon():
    """Icon + colour + text, always — never colour alone."""
    assert set(d.ICONS) == {d.RED, d.AMBER, d.GREEN}
    assert all(d.ICONS.values())
