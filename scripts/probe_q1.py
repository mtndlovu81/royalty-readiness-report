"""
Follow-up probe. Settles the two things the first run left open:

  Q1  Do `inc=isrcs` and `inc=work-rels` actually apply on the recording
      BROWSE endpoint, or did we just get 25 empty recordings?

      Method: pick a well-known release, browse its recordings with the
      includes, then look up the SAME recordings individually. If lookup
      returns data that browse omitted, the browse includes don't work.

  P   Is the release-first pipeline cheaper than the recording-first one?
      (Proposed after the first probe showed 58x recording inflation.)

    python scripts/probe_q1.py

Respects the 1 req/sec rate limit. Takes about a minute.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://musicbrainz.org/ws/2"

# Must match the value used elsewhere.
USER_AGENT = "RoyaltyReadinessReport/1.0 ( https://github.com/mtndlovu81/royalty-readiness-report.git )"

# A commercially released album, likely to have ISRCs and work links.
TEST_ARTIST = "Radiohead"
TEST_ALBUM = "OK Computer"

RATE_LIMIT_SECONDS = 1.0
_last_call = 0.0


def get(path, **params):
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_call = time.time()

    params["fmt"] = "json"
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": body[:500]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def header(text):
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def work_rel_count(entity):
    return sum(
        1
        for rel in (entity.get("relations") or [])
        if rel.get("target-type") == "work"
    )


def main():
    if "your-email@example.com" in USER_AGENT:
        print("Edit USER_AGENT at the top of this file first.")
        sys.exit(1)

    # ------------------------------------------------------------------
    header("Setup: find the artist and a known release")

    status, data = get("artist", query=TEST_ARTIST, limit=1)
    if status != 200 or not data.get("artists"):
        print(f"  FAIL — artist search: {status} {data}")
        sys.exit(1)
    artist_mbid = data["artists"][0]["id"]
    print(f"  {TEST_ARTIST}: {artist_mbid}")

    status, data = get(
        "release",
        query=f'release:"{TEST_ALBUM}" AND arid:{artist_mbid}',
        limit=5,
    )
    if status != 200 or not data.get("releases"):
        print(f"  FAIL — release search: {status} {data}")
        sys.exit(1)
    release = data["releases"][0]
    release_mbid = release["id"]
    print(f"  {TEST_ALBUM}: {release_mbid}  ({release.get('date', '?')})")

    # ------------------------------------------------------------------
    header("Q1a — recording BROWSE, filtered by release, with includes")

    status, data = get(
        "recording",
        release=release_mbid,
        inc="isrcs+artist-credits+work-rels",
        limit=100,
    )
    if status != 200:
        print(f"  FAIL — {status}: {data}")
        sys.exit(1)

    browsed = data.get("recordings", [])
    browse_isrcs = {r["id"]: len(r.get("isrcs") or []) for r in browsed}
    browse_works = {r["id"]: work_rel_count(r) for r in browsed}

    print(f"  recordings returned:   {len(browsed)}")
    print(f"  with ISRCs:            {sum(1 for v in browse_isrcs.values() if v)}")
    print(f"  with work relations:   {sum(1 for v in browse_works.values() if v)}")
    for r in browsed[:3]:
        print(
            f"    - {r['title'][:40]:42} "
            f"isrcs={len(r.get('isrcs') or [])} "
            f"works={work_rel_count(r)}"
        )

    if not browsed:
        print("\n  No recordings on that release. Try a different album.")
        sys.exit(1)

    # ------------------------------------------------------------------
    header("Q1b — the SAME recordings, looked up individually")

    sample = browsed[:5]
    lookup_isrcs, lookup_works = {}, {}

    for r in sample:
        status, one = get(f"recording/{r['id']}", inc="isrcs+work-rels")
        if status != 200:
            print(f"  FAIL — lookup {r['id']}: {status} {one}")
            continue
        lookup_isrcs[r["id"]] = len(one.get("isrcs") or [])
        lookup_works[r["id"]] = work_rel_count(one)
        print(
            f"    - {one.get('title', '?')[:40]:42} "
            f"isrcs={lookup_isrcs[r['id']]} "
            f"works={lookup_works[r['id']]}"
        )

    # ------------------------------------------------------------------
    header("Q1 VERDICT")

    isrc_gap = [
        i for i in lookup_isrcs if lookup_isrcs[i] > browse_isrcs.get(i, 0)
    ]
    work_gap = [
        i for i in lookup_works if lookup_works[i] > browse_works.get(i, 0)
    ]
    lookup_has_data = any(lookup_isrcs.values()) or any(lookup_works.values())

    if not lookup_has_data:
        verdict = "INCONCLUSIVE"
        print("  Lookup returned nothing either — this data genuinely isn't in")
        print("  MusicBrainz for these recordings. Try another album.")
    elif isrc_gap or work_gap:
        verdict = "BROWSE INCLUDES DO NOT WORK"
        print(f"  Lookup found data that browse omitted.")
        print(f"    recordings where browse missed ISRCs: {len(isrc_gap)}")
        print(f"    recordings where browse missed works: {len(work_gap)}")
        print("  -> builder.py must look up recordings individually, or")
        print("     reconcile to works by normalised title.")
    else:
        verdict = "BROWSE INCLUDES WORK"
        print("  Browse and lookup agree. The first probe's 25 recordings were")
        print("  simply empty ones — browse ordering is by internal ID, not")
        print("  popularity.")
        print("  -> builder.py can use browse with includes as planned.")

    # ------------------------------------------------------------------
    header("P — is the release-first pipeline cheaper?")

    status, data = get(
        "release",
        artist=artist_mbid,
        type="album|ep",
        status="official",
        inc="release-groups",
        limit=100,
    )
    if status != 200:
        print(f"  FAIL — {status}: {data}")
        print("  (filtering is mandatory on release browse — check type/status)")
    else:
        total = data.get("release-count", "?")
        releases = data.get("releases", [])
        groups = {
            (r.get("release-group") or {}).get("id")
            for r in releases
            if r.get("release-group")
        }
        pages = -(-total // 100) if isinstance(total, int) else "?"

        print(f"  official albums/EPs:        {total}")
        print(f"  distinct release-groups:    {len(groups)} (in first page)")
        print(f"  release browse pages:       {pages}")

        _, w = get("work", artist=artist_mbid, limit=1)
        works = w.get("work-count", "?")
        work_pages = -(-works // 100) if isinstance(works, int) else "?"

        _, rec = get("recording", artist=artist_mbid, limit=1)
        recordings = rec.get("recording-count", "?")
        rec_pages = -(-recordings // 100) if isinstance(recordings, int) else "?"

        print(f"\n  recording-first: {rec_pages} pages "
              f"({recordings} recordings, mostly bootlegs/compilations)")
        print(f"  release-first:   {pages} release pages "
              f"+ ~{len(groups)} per-release calls + {work_pages} work pages")
        print(f"                   = ~{(pages if isinstance(pages, int) else 0) + len(groups) + (work_pages if isinstance(work_pages, int) else 0)} requests")

    # ------------------------------------------------------------------
    header("SUMMARY — paste into DECISIONS.md")
    print(f"""
Q1 verdict: {verdict}
  browse ISRCs: {sum(1 for v in browse_isrcs.values() if v)}/{len(browsed)}
  browse works: {sum(1 for v in browse_works.values() if v)}/{len(browsed)}
  lookup ISRCs: {sum(1 for v in lookup_isrcs.values() if v)}/{len(lookup_isrcs)}
  lookup works: {sum(1 for v in lookup_works.values() if v)}/{len(lookup_works)}
""")


if __name__ == "__main__":
    main()
