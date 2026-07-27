"""Release-first catalogue fetch.

Steps 1–3 of BUILD.md §4: artist lookup, release browse, collapse to
release-groups, pick one canonical release per group, then browse that
release's recordings. Returns an in-memory structure; **nothing here writes to
the database** — persistence is M2-B.

Why release-first. Browsing recordings by artist returns everything MusicBrainz
holds for them: 12,470 for Radiohead against 214 works, a 58x inflation, mostly
bootlegs and compilation appearances. Browsing releases and collapsing to
groups gets the catalogue an artist would recognise as theirs, in roughly half
the requests.

Failures are per-release. One album that won't fetch marks the build incomplete
and leaves the rest intact — a partial catalogue beats no catalogue.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from r3 import config, db, musicbrainz as mb, slugs

log = logging.getLogger(__name__)

PAGE_SIZE = 100

# Both filters are mandatory on a release browse — without them the endpoint
# returns nothing at all.
RELEASE_TYPES = "album|ep"
RELEASE_STATUS = "official"

# A release browse costs one request per 100 and a huge catalogue is mostly
# reissues; this bounds a pathological artist rather than trimming a real one.
MAX_RELEASE_PAGES = 20

# Box sets exist, but a release with more than a few hundred tracks is a data
# error we should not spend the request budget on.
MAX_RECORDING_PAGES = 5

# CLAUDE.md: never read the Legal name alias, for anyone. Dropped here at the
# fetch boundary so it cannot reach the database or a template later.
FORBIDDEN_ALIAS_TYPE = "legal name"

MAX_WORK_PAGES = 20

# Work relationship types that make someone a writer of the composition. The
# schema's `role` column takes these values directly.
WRITER_ROLES = {"composer", "lyricist", "writer"}


@dataclass
class Album:
    """A release-group, plus the one release chosen to represent it."""

    release_group_mbid: str
    title: str
    primary_type: str | None
    secondary_types: list[str]
    first_released: str | None
    canonical_release_mbid: str
    canonical_track_count: int | None
    release_count: int  # how many releases collapsed into this group


@dataclass
class Recording:
    recording_mbid: str
    title: str
    length_ms: int | None
    isrcs: list[str]
    artist_credit: list[dict[str, Any]]
    work_rels: list[dict[str, Any]]
    release_group_mbid: str


@dataclass
class Credit:
    """A contributor's credit on one composition."""

    mbid: str
    name: str          # primary name, the fallback for display
    credited_as: str | None  # NEVER the Legal name alias
    role: str


@dataclass
class Work:
    """A composition."""

    work_mbid: str
    title: str
    iswc: str | None
    credits: list[Credit] = field(default_factory=list)


@dataclass
class Song:
    """One composition and every version of it we hold.

    A song with no composition record is still a song — one version, no work
    MBID, no publishing ID. It is the case that carries a red flag, so it must
    reach the page rather than being filtered out of the catalogue.
    """

    title: str
    work_mbid: str | None
    iswc: str | None
    versions: list[Recording]
    credits: list[Credit] = field(default_factory=list)
    # DESIGN.md §6: appears on at least one release-group with no secondary
    # type. Only the primary catalogue feeds the headline count.
    is_primary_catalogue: bool = True

    @property
    def primary(self) -> Recording:
        return self.versions[0]


@dataclass
class FetchedCatalogue:
    artist: dict[str, Any]
    albums: list[Album] = field(default_factory=list)
    recordings: list[Recording] = field(default_factory=list)
    works: list[Work] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    requests: int = 0

    @property
    def complete(self) -> bool:
        return not self.errors


def _track_count(release: dict[str, Any]) -> int | None:
    """Total tracks across all media, or None when the browse omitted them."""
    media = release.get("media")
    if not media:
        return None
    total = sum(m.get("track-count") or 0 for m in media)
    return total or None


def _release_date(release: dict[str, Any]) -> str:
    """Sortable date. Missing dates sort last, not first."""
    return release.get("date") or "9999"


def fetch_artist(mbid: str) -> dict[str, Any]:
    """Step 1. Identity, `type` for the IPI gating, and `ipis`."""
    data = mb.get_artist(mbid, inc=["aliases"])

    aliases = [
        alias.get("name")
        for alias in (data.get("aliases") or [])
        if (alias.get("type") or "").lower() != FORBIDDEN_ALIAS_TYPE and alias.get("name")
    ]

    return {
        "mbid": data.get("id"),
        "name": data.get("name"),
        "sort_name": data.get("sort-name"),
        # Person | Group | Orchestra | ... | None. Drives can_hold_ipi().
        "type": data.get("type"),
        "country": data.get("country"),
        "disambiguation": data.get("disambiguation") or None,
        "ipis": data.get("ipis") or [],
        "aliases": aliases,
    }


def browse_releases(mbid: str) -> list[dict[str, Any]]:
    """Step 2. Every official album and EP, paginated.

    `inc=media` costs no extra requests and carries the per-medium track count
    that canonical selection needs.
    """
    releases: list[dict[str, Any]] = []
    offset = 0

    for _ in range(MAX_RELEASE_PAGES):
        page = mb.get(
            "release",
            artist=mbid,
            type=RELEASE_TYPES,
            status=RELEASE_STATUS,
            inc=["release-groups", "media"],
            limit=PAGE_SIZE,
            offset=offset,
        )
        batch = page.get("releases") or []
        releases.extend(batch)

        total = page.get("release-count") or 0
        offset += len(batch)
        if not batch or offset >= total:
            break
    else:
        log.warning("release browse for %s hit the page cap", mbid)

    return releases


def pick_canonical(releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose the release that best represents its group.

    Earliest date wins, but only among releases whose track count matches the
    group's mode. That mode is the album as most editions agree it exists, which
    filters out the deluxe edition with six bonus tracks and the single-disc
    promo without pushing us onto a later reissue.
    """
    counts = [c for c in (_track_count(r) for r in releases) if c]
    if counts:
        mode = Counter(counts).most_common(1)[0][0]
        preferred = [r for r in releases if _track_count(r) == mode]
    else:
        preferred = []

    return min(preferred or releases, key=lambda r: (_release_date(r), r.get("title") or ""))


def collapse_to_albums(releases: list[dict[str, Any]]) -> list[Album]:
    """Group releases by release-group and pick one canonical release each."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for release in releases:
        group = release.get("release-group") or {}
        group_mbid = group.get("id")
        if not group_mbid:
            continue
        groups.setdefault(group_mbid, []).append(release)

    albums = []
    for group_mbid, members in groups.items():
        canonical = pick_canonical(members)
        group = canonical.get("release-group") or {}
        albums.append(
            Album(
                release_group_mbid=group_mbid,
                title=group.get("title") or canonical.get("title") or "Untitled",
                primary_type=group.get("primary-type"),
                secondary_types=group.get("secondary-types") or [],
                first_released=group.get("first-release-date") or None,
                canonical_release_mbid=canonical["id"],
                canonical_track_count=_track_count(canonical),
                release_count=len(members),
            )
        )

    albums.sort(key=lambda a: (a.first_released or "9999", a.title))
    return albums


def browse_recordings(release_mbid: str, release_group_mbid: str) -> list[Recording]:
    """Step 3. One request per canonical release.

    `inc=isrcs+work-rels` returns streaming IDs and composition links together —
    confirmed by probe against a real release, 12/12 with both.
    """
    recordings: list[Recording] = []
    offset = 0

    for _ in range(MAX_RECORDING_PAGES):
        page = mb.get(
            "recording",
            release=release_mbid,
            inc=["isrcs", "artist-credits", "work-rels"],
            limit=PAGE_SIZE,
            offset=offset,
        )
        batch = page.get("recordings") or []

        for item in batch:
            relations = item.get("relations") or []
            recordings.append(
                Recording(
                    recording_mbid=item["id"],
                    title=item.get("title") or "Untitled",
                    length_ms=item.get("length"),
                    isrcs=item.get("isrcs") or [],
                    artist_credit=item.get("artist-credit") or [],
                    work_rels=[r for r in relations if r.get("work")],
                    release_group_mbid=release_group_mbid,
                )
            )

        total = page.get("recording-count") or 0
        offset += len(batch)
        if not batch or offset >= total:
            break
    else:
        log.warning("recording browse for release %s hit the page cap", release_mbid)

    return recordings


def browse_works(mbid: str) -> list[Work]:
    """Step 4. Compositions the artist is credited on, with their writers.

    Note the asymmetry with step 3: recordings are what they *performed*, works
    are what they are *credited on*. A songwriter who doesn't perform has works
    and no recordings; a session band has recordings and no works. Neither set
    can be assumed populated.
    """
    works: list[Work] = []
    offset = 0

    for _ in range(MAX_WORK_PAGES):
        page = mb.get(
            "work",
            artist=mbid,
            inc=["artist-rels"],
            limit=PAGE_SIZE,
            offset=offset,
        )
        batch = page.get("works") or []

        for item in batch:
            iswcs = item.get("iswcs") or []
            credits = []
            for relation in item.get("relations") or []:
                role = (relation.get("type") or "").lower()
                artist = relation.get("artist") or {}
                if role not in WRITER_ROLES or not artist.get("id"):
                    continue
                credits.append(
                    Credit(
                        mbid=artist["id"],
                        name=artist.get("name") or "Unknown",
                        # DESIGN.md §9: the credited name, never the legal one.
                        credited_as=relation.get("target-credit") or None,
                        role=role,
                    )
                )

            works.append(
                Work(
                    work_mbid=item["id"],
                    title=item.get("title") or "Untitled",
                    iswc=iswcs[0] if iswcs else None,
                    credits=credits,
                )
            )

        total = page.get("work-count") or 0
        offset += len(batch)
        if not batch or offset >= total:
            break
    else:
        log.warning("work browse for %s hit the page cap", mbid)

    return works


def group_songs(
    recordings: list[Recording],
    works: list[Work],
    albums: list[Album],
) -> list[Song]:
    """Collapse recordings into one row per composition (DESIGN.md §6).

    MusicBrainz holds a separate recording per release — album cut, remaster,
    radio edit, one per compilation. Ungrouped, six rows show the same
    publishing ID and the table reads as broken.

    Canonical version is earliest release date, ties broken by longest
    duration, final fallback the order returned. Pure — no I/O.
    """
    works_by_mbid = {w.work_mbid: w for w in works}
    release_dates = {a.release_group_mbid: a.first_released for a in albums}
    # A release-group with no secondary type is a studio album, EP, or single.
    studio_groups = {a.release_group_mbid for a in albums if not a.secondary_types}

    grouped: dict[str, list[Recording]] = {}
    stubs: dict[str, dict[str, Any]] = {}

    for recording in recordings:
        work_ids = [
            (rel.get("work") or {}).get("id")
            for rel in recording.work_rels
            if (rel.get("work") or {}).get("id")
        ]
        if work_ids:
            # A medley links several works; the first is the one we group on.
            key = work_ids[0]
            stubs.setdefault(key, (recording.work_rels[0].get("work") or {}))
        else:
            # No composition record. Still a song — this is the red-flag case,
            # and filtering it out would hide exactly what we exist to report.
            key = f"recording:{recording.recording_mbid}"

        grouped.setdefault(key, []).append(recording)

    def sort_key(rec: Recording) -> tuple[str, int]:
        released = release_dates.get(rec.release_group_mbid) or "9999"
        # Negative length so the longest version wins a date tie.
        return (released, -(rec.length_ms or 0))

    songs = []
    for key, versions in grouped.items():
        versions.sort(key=sort_key)
        work = works_by_mbid.get(key)
        stub = stubs.get(key) or {}

        if work is not None:
            title, work_mbid, iswc, credits = work.title, work.work_mbid, work.iswc, work.credits
        elif stub:
            # Linked to a work we didn't browse — it belongs to another artist.
            # The stub still carries a title and sometimes an ISWC.
            stub_iswcs = stub.get("iswcs") or []
            title = stub.get("title") or versions[0].title
            work_mbid = stub.get("id")
            iswc = stub_iswcs[0] if stub_iswcs else None
            credits = []
        else:
            title, work_mbid, iswc, credits = versions[0].title, None, None, []

        # Decided per song across every group it appears on, not per release: a
        # studio song later included on three compilations is still primary.
        # Classifying releases would demote it the moment a greatest-hits
        # package appeared, which is backwards — wider exposure, not less.
        is_primary_catalogue = any(
            v.release_group_mbid in studio_groups for v in versions
        )

        songs.append(
            Song(
                title=title,
                work_mbid=work_mbid,
                iswc=iswc,
                versions=versions,
                credits=credits,
                is_primary_catalogue=is_primary_catalogue,
            )
        )

    songs.sort(key=lambda s: (sort_key(s.primary), s.title))
    return songs


def resolve_contributors(songs: list[Song]) -> dict[str, dict[str, Any]]:
    """Step 5. Fill in each distinct contributor's `type` and `ipis`.

    Cached globally rather than per artist: a producer credited across forty
    artists is fetched once, ever. This is the largest remaining slice of the
    request budget and the hit rate climbs fast.
    """
    wanted = {credit.mbid: credit.name for song in songs for credit in song.credits}
    if not wanted:
        return {}

    cached = {
        row["mbid"]: row
        for row in db.query(
            """
            SELECT mbid, name, type, ipis
              FROM contributors
             WHERE mbid = ANY(%s)
               AND last_checked_at > now() - make_interval(days => %s)
            """,
            (list(wanted), config.STALE_AFTER_DAYS),
        )
    }

    resolved: dict[str, dict[str, Any]] = dict(cached)
    misses = [mbid for mbid in wanted if mbid not in cached]
    log.info("contributors: %d cached, %d to fetch", len(cached), len(misses))

    for mbid in misses:
        try:
            data = mb.get_artist(mbid)
        except mb.MusicBrainzError as exc:
            # Missing IPIs are already an amber flag; a failed lookup should not
            # sink the build, it just leaves the flag as it would have been.
            log.warning("contributor %s unresolved: %s", mbid, exc)
            resolved[mbid] = {"mbid": mbid, "name": wanted[mbid], "type": None, "ipis": []}
            continue

        resolved[mbid] = {
            "mbid": mbid,
            "name": data.get("name") or wanted[mbid],
            # Needed for can_hold_ipi() on contributors: a band credited as a
            # writer legitimately has no IPI and must not throw amber.
            "type": data.get("type"),
            "ipis": data.get("ipis") or [],
        }

    return resolved


def fetch_catalogue(mbid: str) -> FetchedCatalogue:
    """Steps 1–3 end to end. No database writes."""
    mb.reset_request_count()

    artist = fetch_artist(mbid)
    log.info("building %s (%s)", artist["name"], artist["type"])

    releases = browse_releases(mbid)
    albums = collapse_to_albums(releases)
    log.info("%d releases collapsed to %d albums", len(releases), len(albums))

    catalogue = FetchedCatalogue(artist=artist, albums=albums)

    for album in albums:
        try:
            catalogue.recordings.extend(
                browse_recordings(album.canonical_release_mbid, album.release_group_mbid)
            )
        except mb.MusicBrainzError as exc:
            # Keep going. The rest of the catalogue is still worth having, and
            # the build is marked incomplete so a re-scan can fill the gap.
            log.warning("skipping album %r: %s", album.title, exc)
            catalogue.errors.append(f"{album.title}: {exc}")

    try:
        catalogue.works = browse_works(mbid)
        log.info("%d works", len(catalogue.works))
    except mb.MusicBrainzError as exc:
        # Recordings without works still produce a catalogue, flagged red for
        # having no composition record. Better than abandoning the build.
        log.warning("work browse failed: %s", exc)
        catalogue.errors.append(f"works: {exc}")

    # Peek rather than reset: step 5 (contributor resolution) runs after this
    # and its requests belong in the same total. Resetting here reported 57 for
    # a Björk build that actually cost 140+.
    catalogue.requests = mb.request_count
    log.info(
        "%d recordings, %d works in %d requests (%s)",
        len(catalogue.recordings),
        len(catalogue.works),
        catalogue.requests,
        "complete" if catalogue.complete else f"{len(catalogue.errors)} failure(s)",
    )
    return catalogue


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _as_date(value: str | None) -> str | None:
    """MusicBrainz dates may be a year, a year-month, or a full date.

    Postgres accepts only the last, so partials are padded to their first day.
    Losing the distinction between "1994" and "1994-01-01" is acceptable: the
    date is used for ordering, never displayed as a precise release day.
    """
    if not value:
        return None
    parts = value.split("-")
    if len(parts) == 1:
        return f"{parts[0]}-01-01"
    if len(parts) == 2:
        return f"{parts[0]}-{parts[1]}-01"
    return value


def persist(catalogue: FetchedCatalogue, songs: list[Song],
            contributors: dict[str, dict[str, Any]]) -> str:
    """Write the whole catalogue in one transaction. Returns the artist id.

    Everything lands with `source='musicbrainz'` and `verified_at` null.
    **Verified rows are never overwritten** — nothing is verified in v1, but the
    rule is enforced here rather than left as a comment for later.
    """
    artist = catalogue.artist

    with db.connection() as conn:
        with conn.cursor() as cur:
            # --- artist ------------------------------------------------
            cur.execute("SELECT id, slug FROM artists WHERE mbid = %s", (artist["mbid"],))
            row = cur.fetchone()

            if row:
                artist_id, artist_slug = row["id"], row["slug"]
                # Slug is immutable once minted — it is the public URL.
                cur.execute(
                    """
                    UPDATE artists
                       SET name = %s, type = %s, country = %s, disambiguation = %s,
                           ipis = %s, status = 'published', last_checked_at = now()
                     WHERE id = %s AND verified_at IS NULL
                    """,
                    (artist["name"], artist["type"], artist["country"],
                     artist["disambiguation"], artist["ipis"], artist_id),
                )
            else:
                cur.execute("SELECT slug FROM artists")
                taken = {r["slug"] for r in cur.fetchall()}
                artist_slug = slugs.mint(artist["name"], taken)
                cur.execute(
                    """
                    INSERT INTO artists (slug, name, mbid, disambiguation, country,
                                         type, ipis, status, last_checked_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'published', now())
                    RETURNING id
                    """,
                    (artist_slug, artist["name"], artist["mbid"], artist["disambiguation"],
                     artist["country"], artist["type"], artist["ipis"]),
                )
                artist_id = cur.fetchone()["id"]

            # --- clear the previous build ------------------------------
            # Unverified rows are rebuilt from scratch; verified ones survive
            # untouched, which is what "verified data wins over a fetch" means
            # once anything is actually verified.
            cur.execute(
                "DELETE FROM songs WHERE artist_id = %s AND verified_at IS NULL",
                (artist_id,),
            )
            cur.execute("DELETE FROM albums WHERE artist_id = %s", (artist_id,))

            cur.execute(
                "SELECT slug FROM songs WHERE artist_id = %s", (artist_id,)
            )
            song_slugs = {r["slug"] for r in cur.fetchall()}

            # --- albums ------------------------------------------------
            album_ids: dict[str, str] = {}
            album_slugs: set[str] = set()
            for album in catalogue.albums:
                slug = slugs.mint(album.title, album_slugs)
                album_slugs.add(slug)
                cur.execute(
                    """
                    INSERT INTO albums (artist_id, slug, title, release_group_mbid,
                                        secondary_types, first_released)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (artist_id, slug, album.title, album.release_group_mbid,
                     album.secondary_types, _as_date(album.first_released)),
                )
                album_ids[album.release_group_mbid] = cur.fetchone()["id"]

            # --- contributors (global cache) ---------------------------
            contributor_ids: dict[str, str] = {}
            for mbid, data in contributors.items():
                cur.execute(
                    """
                    INSERT INTO contributors (mbid, name, type, ipis, last_checked_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (mbid) DO UPDATE
                       SET name = EXCLUDED.name,
                           type = EXCLUDED.type,
                           ipis = EXCLUDED.ipis,
                           last_checked_at = now()
                     WHERE contributors.verified_at IS NULL
                    RETURNING id
                    """,
                    (mbid, data["name"], data["type"], data["ipis"]),
                )
                returned = cur.fetchone()
                if returned is None:
                    # The row exists and is verified, so the upsert did nothing.
                    cur.execute("SELECT id FROM contributors WHERE mbid = %s", (mbid,))
                    returned = cur.fetchone()
                contributor_ids[mbid] = returned["id"]

            # --- songs, versions, credits ------------------------------
            album_positions: dict[str, int] = {}
            song_count = version_count = 0

            for song in songs:
                slug = slugs.mint(song.title, song_slugs)
                song_slugs.add(slug)

                cur.execute(
                    """
                    INSERT INTO songs (artist_id, slug, title, iswc, work_mbid,
                                       is_primary_catalogue)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (work_mbid) DO NOTHING
                    RETURNING id
                    """,
                    (artist_id, slug, song.title, song.iswc, song.work_mbid,
                     song.is_primary_catalogue),
                )
                inserted = cur.fetchone()
                if inserted is None:
                    # A verified song survived the delete, or the work belongs
                    # to an artist already built. Leave it as it stands.
                    continue
                song_id = inserted["id"]
                song_count += 1

                for index, version in enumerate(song.versions):
                    album_date = next(
                        (a.first_released for a in catalogue.albums
                         if a.release_group_mbid == version.release_group_mbid),
                        None,
                    )
                    cur.execute(
                        """
                        INSERT INTO versions (song_id, recording_mbid, title, isrc,
                                              length_ms, first_released, is_primary)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (recording_mbid) DO NOTHING
                        RETURNING id
                        """,
                        (song_id, version.recording_mbid, version.title,
                         version.isrcs[0] if version.isrcs else None,
                         version.length_ms, _as_date(album_date), index == 0),
                    )
                    version_row = cur.fetchone()
                    if version_row is None:
                        # `recording_mbid` is globally unique, so a track that
                        # appears on several of this artist's albums inserts
                        # once. It still belongs on every album it appeared on,
                        # so fetch the existing row rather than skipping — or
                        # the album view loses tracks.
                        cur.execute(
                            "SELECT id FROM versions WHERE recording_mbid = %s",
                            (version.recording_mbid,),
                        )
                        version_row = cur.fetchone()
                        if version_row is None:
                            continue
                    else:
                        version_count += 1

                    album_id = album_ids.get(version.release_group_mbid)
                    if album_id:
                        position = album_positions.get(album_id, 0) + 1
                        album_positions[album_id] = position
                        cur.execute(
                            """
                            INSERT INTO album_versions (album_id, version_id, position)
                            VALUES (%s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (album_id, version_row["id"], position),
                        )

                for credit in song.credits:
                    contributor_id = contributor_ids.get(credit.mbid)
                    if not contributor_id:
                        continue
                    cur.execute(
                        """
                        INSERT INTO song_contributors (song_id, contributor_id, role,
                                                       credited_as)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (song_id, contributor_id, credit.role,
                         credit.credited_as or credit.name),
                    )

    log.info(
        "persisted %s: %d songs, %d versions, %d albums, %d contributors",
        artist_slug, song_count, version_count, len(album_ids), len(contributor_ids),
    )
    return artist_id


def build(mbid: str) -> str:
    """Fetch and persist one artist's catalogue. Returns the artist id."""
    catalogue = fetch_catalogue(mbid)
    songs = group_songs(catalogue.recordings, catalogue.works, catalogue.albums)
    contributors = resolve_contributors(songs)
    artist_id = persist(catalogue, songs, contributors)
    # Now that step 5 is done, this is the real cost of the build.
    catalogue.requests = mb.request_count
    return artist_id
