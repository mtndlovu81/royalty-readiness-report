#!/usr/bin/env python3
"""Create the r³ schema.

    python scripts/init_db.py            # create; fails if tables exist
    python scripts/init_db.py --reset    # drop everything first, then create

`schema.sql` is deliberately not idempotent, so a plain run against an existing
database fails loudly rather than silently leaving it on an older shape. Use
--reset when you want the tables rebuilt.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from r3 import config, db  # noqa: E402

# Reverse dependency order, though CASCADE makes the ordering a formality.
TABLES = [
    "album_versions",
    "song_contributors",
    "versions",
    "albums",
    "songs",
    "build_queue",
    "issues",
    "search_cache",
    "contributors",
    "artists",
]


def existing_tables(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tablename
              FROM pg_tables
             WHERE schemaname = current_schema()
             ORDER BY tablename
            """
        )
        return [row["tablename"] for row in cur.fetchall()]


def drop_all(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for table in TABLES:
            # Table names are from the constant above, never from user input.
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop every r3 table before creating (destroys all data)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt for --reset",
    )
    args = parser.parse_args()

    config.configure_logging()

    schema = config.SCHEMA_PATH.read_text()
    # Show where we are connecting without leaking the password.
    target = config.DATABASE_URL.split("@")[-1]

    try:
        conn = db.connect()
    except psycopg.OperationalError as exc:
        print(f"Could not connect to {target}:\n  {exc}", file=sys.stderr)
        return 1

    with conn:
        present = existing_tables(conn)

        if args.reset:
            if present and not args.yes:
                print(f"About to DROP {len(present)} table(s) on {target}:")
                print("  " + ", ".join(present))
                if input("Type 'drop' to confirm: ").strip() != "drop":
                    print("Aborted.")
                    return 1
            drop_all(conn)
            print(f"Dropped {len(present)} table(s).")
        elif present:
            print(
                f"{len(present)} table(s) already exist on {target}:\n"
                "  " + ", ".join(present) + "\n"
                "Re-run with --reset to drop and rebuild.",
                file=sys.stderr,
            )
            return 1

        try:
            with conn.cursor() as cur:
                cur.execute(schema)
        except psycopg.Error as exc:
            print(f"Schema failed to apply:\n  {exc}", file=sys.stderr)
            return 1

        created = existing_tables(conn)

    print(f"Created {len(created)} table(s) on {target}:")
    for table in created:
        print(f"  {table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
