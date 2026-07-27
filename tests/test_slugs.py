"""Slug minting — TESTS.md §3.

Small but nasty. Slugs are immutable once minted and they are the public URL,
so a bug here corrupts links permanently rather than showing up as a wrong
number on a page. The empty-fallback case is the one that bites: a name written
entirely in punctuation strips to nothing, and an empty slug is a broken route.

Pure functions, plain strings — no database, no network.
"""

import re

import pytest

from r3 import slugs


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Björk", "bjork"),                 # unicode folded
        ("Sigur Rós", "sigur-ros"),
        ("AC/DC", "ac-dc"),                 # no path separators
        ("  Spaces  ", "spaces"),           # trimmed
        ("A   B", "a-b"),                   # no doubled hyphens
        ("Café", "cafe"),
        ("Cafe", "cafe"),                   # folds onto the same base
        ("Beyoncé", "beyonce"),
        ("Motörhead", "motorhead"),
        ("Møster", "moster"),               # ø does not decompose under NFKD
        ("Straße", "strasse"),              # ß expands rather than vanishing
        ("Æon", "aeon"),
        ("Nirvana", "nirvana"),
    ],
)
def test_slugify(name, expected):
    assert slugs.slugify(name) == expected


@pytest.mark.parametrize("name", ["!!!", "???", "…", "", "   ", "///", "-", "🎵"])
def test_never_empty(name):
    """An empty slug is a broken route. There must always be something."""
    result = slugs.slugify(name)
    assert result
    assert result == slugs.FALLBACK


@pytest.mark.parametrize("name", ["AC/DC", "a/b/c", "../etc/passwd", "a?b#c", "x y"])
def test_safe_in_a_path_segment(name):
    """Nothing that changes the shape of a URL may survive slugification."""
    result = slugs.slugify(name)
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", result), result


def test_long_name_truncated_but_valid():
    slug = slugs.slugify("Nirvana " * 40)
    assert len(slug) <= slugs.MAX_LENGTH
    assert not slug.endswith("-")


def test_collision_gets_a_disambiguator():
    """Two artists named Nirvana must not share a URL."""
    first = slugs.mint("Nirvana", taken=set())
    second = slugs.mint("Nirvana", taken={first})
    third = slugs.mint("Nirvana", taken={first, second})

    assert first == "nirvana"
    assert second == "nirvana-2"
    assert third == "nirvana-3"
    assert len({first, second, third}) == 3


def test_cafe_and_cafe_do_not_collide_silently():
    """"Café" and "Cafe" fold to one base — the second must still get its own URL."""
    first = slugs.mint("Café", taken=set())
    second = slugs.mint("Cafe", taken={first})

    assert first == "cafe"
    assert second == "cafe-2"
    assert first != second


def test_long_name_collision_stays_unique_and_bounded():
    """Truncation must cut the stem, not the disambiguator."""
    long_name = "The Very Long Band Name That Goes On " * 5
    taken: set[str] = set()
    minted = []
    for _ in range(4):
        slug = slugs.mint(long_name, taken)
        taken.add(slug)
        minted.append(slug)

    assert len(set(minted)) == 4, minted
    assert all(len(s) <= slugs.MAX_LENGTH for s in minted), minted
    assert all(not s.endswith("-") for s in minted), minted


def test_mint_does_not_mutate_taken():
    taken = {"nirvana"}
    slugs.mint("Nirvana", taken)
    assert taken == {"nirvana"}
