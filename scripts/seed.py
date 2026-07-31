#!/usr/bin/env python3
"""Pre-build a set of well-known artists so the tool is instant on arrival.

    python scripts/seed.py            # queue the roster
    python scripts/seed.py --run      # queue, then build them (slow)
    python scripts/seed.py --check    # report the spread across seeded artists
    python scripts/seed.py --list     # show the roster without touching anything

A cold build takes 30 seconds to four minutes depending on catalogue size.
Someone searching a famous name and watching a progress bar for a minute is the
worst possible first impression, so the recognisable names are built in advance
and cold builds remain the honest fallback for everyone else.

**Run this against production after deploying, and start it early.** At one
request per second it is tens of minutes, not seconds. It gets faster as it
goes: contributors are cached globally, so later artists reuse the writers and
producers earlier ones already resolved.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from r3 import config, db, diagnostics, musicbrainz as mb  # noqa: E402

# Chosen for range, not fame. A demo of all-green profiles proves nothing, and
# neither does all-red: the point is that the flags discriminate.
#
#   * groups AND solo artists — the artist-level IPI flag is suppressed for
#     groups, and that gating is invisible unless both are present
#   * catalogues MusicBrainz covers meticulously, and ones it barely covers
#   * a spread of eras and regions, because coverage correlates with both
#
# `mbid` is optional: left out, the name is resolved by search at seed time.
ROSTER: list[dict[str, str]] = [
    # --- groups: exercise the "bands have no IPI, correctly" path ----------
    {"name": "Radiohead", "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711"},
    {"name": "Portishead", "mbid": "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11"},
    {"name": "Massive Attack", "mbid": "10adbe5e-a2c0-4bf3-8249-2b4cbf6e6ca8"},
    {"name": "Daft Punk"},
    {"name": "Fleetwood Mac"},
    {"name": "The xx"},

    # --- solo artists: the artist-level IPI flag can actually fire ---------
    {"name": "Björk", "mbid": "87c5dedd-371d-4a53-9f7f-80522fb7f3cb"},
    {"name": "Nina Simone"},
    {"name": "Kendrick Lamar"},
    {"name": "Sufjan Stevens"},
    {"name": "FKA twigs"},
    {"name": "Sampha"},

    # --- thinner coverage: where the red flags are real --------------------
    {"name": "Kabza De Small", "mbid": "36f9accc-d9f6-4dd2-8c23-91da477ce35c"},
    {"name": "Burna Boy"},
    {"name": "Angélique Kidjo"},
    {"name": "Sons of Kemet"},
    {"name": "Little Simz"},
    {"name": "Nubya Garcia"},
]

# Below this, a search match is too weak to trust unattended — better to skip
# and let a human look than to seed the wrong artist under a famous name.
MIN_SCORE = 85


def resolve(entry: dict[str, str]) -> str | None:
    """Find the MBID for a roster entry, by search if it wasn't given."""
    if entry.get("mbid"):
        return entry["mbid"]

    try:
        results = mb.search_artists(entry["name"], limit=3)
    except mb.MusicBrainzError as exc:
        print(f"  ! {entry['name']}: lookup failed ({exc})")
        return None

    if not results:
        print(f"  ! {entry['name']}: no match")
        return None

    best = results[0]
    score = int(best.get("score") or 0)
    if score < MIN_SCORE:
        print(f"  ! {entry['name']}: best match only scored {score}, skipping")
        return None

    print(f"    resolved {entry['name']} -> {best['name']} "
          f"({best.get('type')}, {best.get('country') or '??'}, score {score})")
    return best["id"]


def enqueue_roster() -> int:
    """Queue every roster entry. Already-built artists are skipped."""
    queued = 0
    for entry in ROSTER:
        built = db.query_one(
            "SELECT slug FROM artists WHERE name = %s AND status = 'published'",
            (entry["name"],),
        )
        if built:
            print(f"  = {entry['name']} already built ({built['slug']})")
            continue

        mbid = resolve(entry)
        if mbid is None:
            continue

        row = db.query_one(
            """
            INSERT INTO build_queue (artist_mbid) VALUES (%s)
            ON CONFLICT (artist_mbid) DO UPDATE
               SET status = CASE WHEN build_queue.status = 'failed'
                                 THEN 'queued' ELSE build_queue.status END
            RETURNING id, status
            """,
            (mbid,),
        )
        print(f"  + {entry['name']} queued ({row['status']})")
        queued += 1

    return queued


def check_spread() -> int:
    """Report what the seeded profiles actually look like.

    BUILD.md §10: verify the set shows a range before recording anything. An
    all-green roster demonstrates nothing, and an all-red one looks broken.
    """
    artists = db.query(
        """
        SELECT id, slug, name, type, ipis
          FROM artists WHERE status = 'published' ORDER BY name
        """
    )
    if not artists:
        print("No artists built yet.")
        return 1

    print(f"{'artist':<22} {'type':<8} {'songs':>6} {'red':>5} {'amber':>6} {'green':>6}  headline")
    print("-" * 78)

    totals = {"red": 0, "amber": 0, "green": 0}
    groups = people = 0

    for artist in artists:
        songs = db.query(
            "SELECT id, title, iswc, work_mbid, is_primary_catalogue "
            "FROM songs WHERE artist_id = %s",
            (artist["id"],),
        )
        ids = [s["id"] for s in songs]
        versions = db.query(
            "SELECT song_id, isrc, is_primary FROM versions WHERE song_id = ANY(%s)",
            (ids,),
        ) if ids else []
        credits = db.query(
            "SELECT sc.song_id, c.type, c.ipis FROM song_contributors sc "
            "JOIN contributors c ON c.id = sc.contributor_id WHERE sc.song_id = ANY(%s)",
            (ids,),
        ) if ids else []

        by_v: dict = {}
        by_c: dict = {}
        for v in versions:
            by_v.setdefault(v["song_id"], []).append(v)
        for c in credits:
            by_c.setdefault(c["song_id"], []).append(c)
        for s in songs:
            s["versions"] = by_v.get(s["id"], [])
            s["contributors"] = by_c.get(s["id"], [])

        shape = diagnostics.artist_shape(bool(credits), bool(versions))
        counts = {"red": 0, "amber": 0, "green": 0}
        for s in songs:
            counts[diagnostics.worst_severity(diagnostics.evaluate_song(s, shape))] += 1
        for k in counts:
            totals[k] += counts[k]

        if artist["type"] == "Group":
            groups += 1
        elif artist["type"] == "Person":
            people += 1

        flagged, total = diagnostics.headline(songs, shape)
        print(f"{artist['name'][:21]:<22} {str(artist['type'])[:7]:<8} {len(songs):>6} "
              f"{counts['red']:>5} {counts['amber']:>6} {counts['green']:>6}  "
              f"{flagged} of {total}")

    print("-" * 78)
    print(f"{'TOTAL':<22} {'':<8} {sum(totals.values()):>6} "
          f"{totals['red']:>5} {totals['amber']:>6} {totals['green']:>6}")
    print()

    # The three things that make a demo worth recording.
    ok = True
    for label, passed, detail in [
        ("at least one group", groups >= 1, f"{groups} group(s)"),
        ("at least one solo artist", people >= 1, f"{people} person/people"),
        ("all three severities present", all(totals.values()),
         f"red={totals['red']} amber={totals['amber']} green={totals['green']}"),
    ]:
        mark = "PASS" if passed else "FAIL"
        ok = ok and passed
        print(f"  [{mark}] {label}: {detail}")

    if not ok:
        print("\nSeed more artists before recording — the flags need to be seen "
              "discriminating, not all saying the same thing.")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="build the queue after seeding (slow)")
    parser.add_argument("--check", action="store_true",
                        help="report the spread across built artists and exit")
    parser.add_argument("--list", action="store_true",
                        help="show the roster and exit")
    args = parser.parse_args()

    config.configure_logging()

    if args.list:
        for entry in ROSTER:
            print(f"  {entry['name']}{'  (mbid pinned)' if entry.get('mbid') else ''}")
        print(f"\n{len(ROSTER)} artists")
        return 0

    if args.check:
        return check_spread()

    print(f"Seeding {len(ROSTER)} artists…")
    queued = enqueue_roster()
    print(f"\n{queued} queued.")

    if not queued:
        return 0

    if not args.run:
        print("Start the worker to build them:")
        print("  RUN_WORKER=true python scripts/worker.py --once")
        return 0

    if not config.RUN_WORKER:
        print("RUN_WORKER is false — refusing to build. Web01 only.", file=sys.stderr)
        return 1

    # Imported here so `--list` and `--check` don't need the worker at all.
    sys.path.insert(0, str(ROOT / "scripts"))
    import worker  # noqa: E402

    started = time.monotonic()
    built = worker.run_once()
    print(f"\nBuilt {built} artist(s) in {(time.monotonic() - started) / 60:.1f} minutes.")
    return check_spread()


if __name__ == "__main__":
    sys.exit(main())
