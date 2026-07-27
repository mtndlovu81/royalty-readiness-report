#!/usr/bin/env python3
"""Build queue consumer.

    python scripts/worker.py                 # poll forever (systemd service)
    python scripts/worker.py --once          # drain the queue and exit (timer)
    python scripts/worker.py --enqueue MBID  # queue one artist
    python scripts/worker.py --prune         # cache maintenance only

⚠ **Web01 only.** This process holds the MusicBrainz token bucket. A second
worker anywhere is a second bucket and twice the outbound rate, which gets the
whole application blocked. `RUN_WORKER` guards the entrypoint as a second line
of defence behind the systemd unit — never set it true on Web02.

Everything that talks to MusicBrainz lives behind this queue. A web request may
make one gated search call; nothing in a request path ever builds a catalogue.
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from r3 import builder, config, db, musicbrainz as mb  # noqa: E402

log = logging.getLogger("r3.worker")

POLL_SECONDS = 5.0

# A build that has failed this many times is not going to succeed by being
# tried again on the same schedule; leave it for a human to look at.
MAX_ATTEMPTS = 3

# Retention, not freshness. Rows older than the 24h serving window are still
# cheap to overwrite in place, so they are kept until they are properly cold.
CACHE_RETENTION = "7 days"
PRUNE_EVERY_SECONDS = 3600.0

_shutdown = False


def _handle_signal(signum, _frame) -> None:
    """Finish the build in flight, then stop. Never abandon a half-written one."""
    global _shutdown
    _shutdown = True
    log.info("signal %s received; finishing current build then stopping", signum)


def claim() -> dict[str, Any] | None:
    """Take the oldest queued build, atomically.

    `FOR UPDATE SKIP LOCKED` means a second worker could never double-process a
    row — but that is belt and braces, not permission. There is still only ever
    one worker, because the rate limit is per process, not per row.
    """
    return db.query_one(
        """
        UPDATE build_queue
           SET status = 'running', started_at = now(), attempts = attempts + 1
         WHERE id = (
               SELECT id FROM build_queue
                WHERE status = 'queued'
                ORDER BY requested_at
                  FOR UPDATE SKIP LOCKED
                LIMIT 1)
        RETURNING id, artist_mbid, attempts
        """
    )


def mark_done(queue_id: str, artist_id: str, error: str | None) -> None:
    """Finished. `error` non-null means a partial build — persisted, incomplete."""
    db.execute(
        """
        UPDATE build_queue
           SET status = 'done', finished_at = now(), artist_id = %s, error = %s,
               progress = 'Finished', progress_pct = 100, heartbeat_at = now()
         WHERE id = %s
        """,
        (artist_id, error, queue_id),
    )


def mark_failed(queue_id: str, attempts: int, reason: str, *, permanent: bool = False) -> None:
    """Nothing was persisted. Retry unless we've spent the attempts.

    `permanent` is for answers that will not change on a retry — a 404 means
    the artist does not exist upstream, and asking again wastes the budget.
    """
    if not permanent and attempts < MAX_ATTEMPTS:
        db.execute(
            "UPDATE build_queue SET status = 'queued', error = %s WHERE id = %s",
            (reason, queue_id),
        )
        log.warning("build failed (attempt %d/%d), requeued: %s", attempts, MAX_ATTEMPTS, reason)
    else:
        db.execute(
            """
            UPDATE build_queue
               SET status = 'failed', finished_at = now(), error = %s
             WHERE id = %s
            """,
            (reason, queue_id),
        )
        log.error(
            "build failed permanently (%s): %s",
            "not retryable" if permanent else f"{attempts} attempts",
            reason,
        )


def _mark_artist_failed(mbid: str) -> None:
    """Only if we already hold a row — never invent one for a build that died."""
    db.execute(
        "UPDATE artists SET status = 'failed' WHERE mbid = %s AND verified_at IS NULL",
        (mbid,),
    )


def process_one() -> bool:
    """Claim and run one build. Returns False when the queue is empty."""
    job = claim()
    if job is None:
        return False

    mbid = job["artist_mbid"]
    log.info("building %s (attempt %d)", mbid, job["attempts"])
    started = time.monotonic()

    def report(message: str, pct: int) -> None:
        """Publish the phase, and touch the heartbeat.

        The heartbeat is what lets the status page say "nothing is running"
        instead of showing a reassuring bar for a build nobody is working on.
        """
        db.execute(
            """
            UPDATE build_queue
               SET progress = %s, progress_pct = %s, heartbeat_at = now()
             WHERE id = %s
            """,
            (message, max(0, min(100, pct)), job["id"]),
        )

    try:
        catalogue = builder.fetch_catalogue(mbid, on_progress=report)
        songs = builder.group_songs(catalogue.recordings, catalogue.works, catalogue.albums)
        contributors = builder.resolve_contributors(songs, on_progress=report)
        report("Saving the catalogue", 96)
        artist_id = builder.persist(catalogue, songs, contributors)
        # Steps 1–4 plus every contributor lookup and retry.
        catalogue.requests = mb.request_count
    except mb.NotFound:
        # Definitive: the artist does not exist upstream. Retrying cannot help.
        mark_failed(job["id"], job["attempts"], f"{mbid} not found upstream", permanent=True)
        return True
    except Exception as exc:  # noqa: BLE001 — the worker must survive anything
        log.exception("build of %s raised", mbid)
        _mark_artist_failed(mbid)
        mark_failed(job["id"], job["attempts"], f"{type(exc).__name__}: {exc}")
        return True

    # A partial build is still a build: what succeeded is persisted and
    # viewable, and the reason the rest is missing is recorded for a re-scan.
    error = "; ".join(catalogue.errors)[:2000] if catalogue.errors else None
    mark_done(job["id"], artist_id, error)

    log.info(
        "built %s in %.1fs — %d songs, %d requests%s",
        mbid,
        time.monotonic() - started,
        len(songs),
        catalogue.requests,
        "" if catalogue.complete else f" ({len(catalogue.errors)} incomplete)",
    )
    return True


def prune_search_cache() -> int:
    """Drop cold cached searches.

    Here, never in a request path: a delete on the read path makes one unlucky
    visitor pay for everyone else's cleanup.
    """
    removed = db.execute(
        "DELETE FROM search_cache WHERE fetched_at < now() - %s::interval",
        (CACHE_RETENTION,),
    )
    if removed:
        log.info("pruned %d cached search(es)", removed)
    return removed


def enqueue(mbid: str) -> bool:
    """Queue an artist. Returns False if it was already waiting."""
    row = db.query_one(
        """
        INSERT INTO build_queue (artist_mbid)
        VALUES (%s)
        ON CONFLICT (artist_mbid) DO NOTHING
        RETURNING id
        """,
        (mbid,),
    )
    return row is not None


def run_once() -> int:
    """Drain the queue, then return how many builds ran."""
    built = 0
    while process_one():
        built += 1
        if _shutdown:
            break
    prune_search_cache()
    return built


def run_forever() -> None:
    log.info("worker started; polling every %.0fs", POLL_SECONDS)
    last_prune = 0.0

    while not _shutdown:
        try:
            worked = process_one()
        except Exception:  # noqa: BLE001
            # A crash here would stop every future build, so the loop outlives
            # any single failure.
            log.exception("worker loop error")
            worked = False

        now = time.monotonic()
        if now - last_prune > PRUNE_EVERY_SECONDS:
            try:
                prune_search_cache()
            except Exception:  # noqa: BLE001
                log.exception("prune failed")
            last_prune = now

        if not worked and not _shutdown:
            time.sleep(POLL_SECONDS)

    log.info("worker stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    parser.add_argument("--enqueue", metavar="MBID", help="queue an artist and exit")
    parser.add_argument("--prune", action="store_true", help="prune the search cache and exit")
    args = parser.parse_args()

    config.configure_logging()

    if args.enqueue:
        queued = enqueue(args.enqueue)
        print(f"{args.enqueue}: {'queued' if queued else 'already queued'}")
        return 0

    if args.prune:
        print(f"pruned {prune_search_cache()} row(s)")
        return 0

    # The systemd unit should already prevent this; the flag is the backstop.
    if not config.RUN_WORKER:
        print(
            "RUN_WORKER is false — refusing to start.\n"
            "Set it true on Web01 only. A second worker doubles the outbound "
            "rate to MusicBrainz and gets the application blocked.",
            file=sys.stderr,
        )
        return 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        if args.once:
            print(f"built {run_once()} artist(s)")
        else:
            run_forever()
    finally:
        mb.close()
        db.close_pool()

    return 0


if __name__ == "__main__":
    sys.exit(main())
