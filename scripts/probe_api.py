"""
Probe the MusicBrainz API to resolve the open questions in DECISIONS.md.

Run this BEFORE writing builder.py. The answers determine the shape of the
catalogue build pipeline, and discovering them late costs a rewrite.

    python scripts/probe_api.py

Respects the 1 req/sec rate limit. Takes roughly 30 seconds.
Paste the summary at the end into DECISIONS.md.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://musicbrainz.org/ws/2"

# Edit this. MusicBrainz blocks requests without a descriptive User-Agent
# that includes contact information.
USER_AGENT = "RoyaltyReadinessReport/1.0 ( https://github.com/mtndlovu81/royalty-readiness-report )"

# Two artists with different catalogue shapes, so an empty result from one
# doesn't get mistaken for an unsupported parameter.
TEST_ARTISTS = ["Radiohead", "Bjork"]

RATE_LIMIT_SECONDS = 1.0
_last_call = 0.0

findings = {}


def get(path, **params):
    """Rate-limited GET. Returns (status, parsed_json_or_error_text)."""
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


def check_user_agent():
    header("Sanity check: User-Agent accepted")
    if "your-email@example.com" in USER_AGENT:
        print("  ! Edit USER_AGENT at the top of this file before running.")
        sys.exit(1)

    status, data = get("artist", query="test", limit=1)
    if status == 200:
        print(f"  OK  — server responded 200")
        return True
    print(f"  FAIL — status {status}: {data}")
    return False


def find_artist(name):
    """Q5: does artist search return enough to disambiguate?"""
    status, data = get("artist", query=name, limit=5)
    if status != 200:
        print(f"  FAIL — search for {name!r}: {status} {data}")
        return None

    artists = data.get("artists", [])
    if not artists:
        print(f"  FAIL — no results for {name!r}")
        return None

    top = artists[0]
    print(f"\n  {name!r} -> {top.get('name')}  ({top.get('id')})")
    print(f"    score:          {top.get('score')}")
    print(f"    disambiguation: {top.get('disambiguation') or '(none)'}")
    print(f"    country:        {top.get('country') or '(none)'}")
    print(f"    type:           {top.get('type') or '(none)'}")
    print(f"    life-span:      {(top.get('life-span') or {}).get('begin') or '(none)'}")
    return top.get("id")


def q1_work_rels(mbid, label):
    """Q1: does the recording browse endpoint accept inc=work-rels?"""
    status, data = get(
        "recording",
        artist=mbid,
        inc="isrcs+artist-credits+work-rels",
        limit=25,
    )

    if status == 400:
        print(f"  [{label}] NOT SUPPORTED — 400: {data.get('error', data)}")
        return False
    if status != 200:
        print(f"  [{label}] inconclusive — status {status}: {data}")
        return None

    recordings = data.get("recordings", [])
    with_rels = [r for r in recordings if r.get("relations")]
    work_rels = [
        rel
        for r in recordings
        for rel in (r.get("relations") or [])
        if rel.get("target-type") == "work"
    ]
    with_isrc = [r for r in recordings if r.get("isrcs")]

    print(f"  [{label}] ACCEPTED — 200")
    print(f"      recordings returned:        {len(recordings)}")
    print(f"      with any relations:         {len(with_rels)}")
    print(f"      with work relations:        {len(work_rels)}")
    print(f"      with ISRCs:                 {len(with_isrc)}")

    if work_rels:
        w = work_rels[0].get("work", {})
        print(f"      example work: {w.get('title')!r} ({w.get('id')})")
    return bool(work_rels)


def q2_releases(mbid, label):
    """Q2: does the recording browse endpoint accept inc=releases?"""
    status, data = get("recording", artist=mbid, inc="isrcs+releases", limit=25)

    if status == 400:
        print(f"  [{label}] NOT SUPPORTED — 400: {data.get('error', data)}")
        return False
    if status != 200:
        print(f"  [{label}] inconclusive — status {status}: {data}")
        return None

    recordings = data.get("recordings", [])
    with_releases = [r for r in recordings if r.get("releases")]
    print(f"  [{label}] ACCEPTED — 200")
    print(f"      recordings returned:        {len(recordings)}")
    print(f"      carrying release data:      {len(with_releases)}")
    return bool(with_releases)


def q_work_browse(mbid, label):
    """Step 3 of the pipeline: does the work browse carry ISWCs and writers?"""
    status, data = get("work", artist=mbid, inc="artist-rels", limit=25)

    if status != 200:
        print(f"  [{label}] FAIL — status {status}: {data}")
        return None

    works = data.get("works", [])
    with_iswc = [w for w in works if w.get("iswcs")]
    writers = [
        rel
        for w in works
        for rel in (w.get("relations") or [])
        if rel.get("type") in ("composer", "lyricist", "writer")
    ]

    print(f"  [{label}] OK — 200")
    print(f"      works returned:             {len(works)}")
    print(f"      with ISWCs:                 {len(with_iswc)}")
    print(f"      writer relationships:       {len(writers)}")

    if writers:
        rel = writers[0]
        artist = rel.get("artist", {})
        print(f"      example writer: {artist.get('name')!r} as {rel.get('type')}")
        print(f"      credited-as present: {'yes' if rel.get('target-credit') else 'no'}")
        print(f"      IPI in the stub:     {'yes' if artist.get('ipis') else 'no'}")
        return artist.get("id")
    return None


def q_artist_ipi(mbid, label):
    """Does a full artist lookup populate `ipis`? The IPI diagnostic depends on it."""
    status, data = get(f"artist/{mbid}")
    if status != 200:
        print(f"  [{label}] FAIL — status {status}: {data}")
        return

    print(f"  [{label}] {data.get('name')!r}")
    print(f"      ipis:  {data.get('ipis')}")
    print(f"      isnis: {data.get('isnis')}")
    print(f"      deprecated singular 'ipi' present: {'ipi' in data}")


def q4_catalogue_size(mbid, label):
    """Q4: how badly does version count inflate?"""
    _, rec = get("recording", artist=mbid, limit=1)
    _, wrk = get("work", artist=mbid, limit=1)

    recordings = rec.get("recording-count", "?")
    works = wrk.get("work-count", "?")
    print(f"  [{label}] recordings: {recordings}   works: {works}")

    if isinstance(recordings, int) and isinstance(works, int) and works:
        pages = -(-recordings // 100) + -(-works // 100)
        print(f"      inflation ratio: {recordings / works:.1f}x")
        print(f"      browse pages needed: ~{pages}  (~{pages}s at 1 req/sec)")
    return recordings, works


def main():
    print(f"User-Agent: {USER_AGENT}")
    if not check_user_agent():
        sys.exit(1)

    header("Q5 — artist search: enough to disambiguate?")
    mbids = {}
    for name in TEST_ARTISTS:
        mbid = find_artist(name)
        if mbid:
            mbids[name] = mbid

    if not mbids:
        print("\nNo artists resolved. Aborting.")
        sys.exit(1)

    header("Q1 — recording browse: inc=work-rels")
    q1 = {label: q1_work_rels(mbid, label) for label, mbid in mbids.items()}

    header("Q2 — recording browse: inc=releases")
    q2 = {label: q2_releases(mbid, label) for label, mbid in mbids.items()}

    header("Work browse: ISWCs and writer relationships")
    writer_ids = {}
    for label, mbid in mbids.items():
        wid = q_work_browse(mbid, label)
        if wid:
            writer_ids[label] = wid

    header("Artist lookup: is `ipis` populated?")
    for label, mbid in mbids.items():
        q_artist_ipi(mbid, label)
    for label, wid in writer_ids.items():
        q_artist_ipi(wid, f"{label} writer")

    header("Q4 — catalogue size and version inflation")
    sizes = {label: q4_catalogue_size(mbid, label) for label, mbid in mbids.items()}

    header("SUMMARY — paste into DECISIONS.md")
    print(f"""
| # | Question | Answer |
|---|---|---|
| 1 | recording browse accepts inc=work-rels | {q1} |
| 2 | recording browse accepts inc=releases  | {q2} |
| 4 | recordings vs works per artist          | {sizes} |

If Q1 is True  -> builder.py links songs to compositions from the recording
                  browse directly. Preferred path.
If Q1 is False -> reconcile recordings to works by normalised title between
                  the recording browse and the work browse results.

If Q2 is False -> the album view needs a release browse
                  (/release?artist=&type=album|ep, filtering is mandatory)
                  plus one recording browse per release. Defer it to the end
                  of M4; it is the most expensive view.
""")


if __name__ == "__main__":
    main()
