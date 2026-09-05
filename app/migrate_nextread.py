"""Bring an existing audiobook-service database into this one.

    python -m app.migrate_nextread /path/to/nextread.db            # says what it would do
    python -m app.migrate_nextread /path/to/nextread.db --apply    # does it

Only needed by an installation that ran the two services separately. A fresh
one has nothing to import and should not run this.

What moves, and what does not:

* **Requests move.** ``(user_key, asin)`` becomes
  ``(user_key, 'book', asin)`` -- the old key is the new key with the medium
  left out -- so this is an INSERT ... SELECT and not a reshape. Titles and
  authors come with them, because arrival is decided on the title with an
  author to agree with it and a row without them is decided by the title
  alone.
* **Dismissals, runs, recommendation snapshots and feedback move**, so a
  hidden book stays hidden and a request stays attributable to the run that
  suggested it.
* **The caches do not.** The Audible similarity graph, the cached products and
  the document vectors are all rebuildable, and copying a stale graph in would
  make the first shelf look right while scoring on fields that were not being
  kept when it was written. Warming them again costs requests and a slow first
  shelf, not correctness.
* **Shelves do not.** They are derived from everything above and are recomputed
  on the first read.

Nothing is deleted from the source database, and an existing row in this one is
never overwritten: a conflict is reported and skipped, because the row already
here is the one this service has been serving.
"""
import argparse
import sqlite3
import sys

from . import config, store

#: Both services key user state on Jellyfin's account id exactly as Jellyfin
#: reports it, with no normalisation on either side -- verified against a live
#: server, which returns ids undashed and lower-case. So the keys need no
#: translation. If that ever stops being true, this is the assumption to
#: revisit first: a mismatch would give one person two keys, and their book
#: allowance would read unspent.
_TABLES_MOVED = ("requests", "dismissed", "runs", "recommendation_items",
                 "feedback_events", "submitted")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if not _has_table(conn, table):
        return []
    return list(conn.execute(f"SELECT * FROM {table}"))


def _import_requests(source: sqlite3.Connection,
                     target: sqlite3.Connection) -> tuple[int, int]:
    moved = skipped = 0
    for row in _rows(source, "requests"):
        keys = row.keys()
        asin = row["asin"] if "asin" in keys else None
        if not asin:
            continue
        existing = target.execute(
            "SELECT 1 FROM requests WHERE user_key=? AND medium='book' "
            "AND item_key=?", (row["user_key"], asin)).fetchone()
        if existing:
            skipped += 1
            continue
        target.execute(
            "INSERT INTO requests (user_key, medium, item_key, unit, title, "
            "year, cost, backend_id, authors, requested_at, fulfilled_at) "
            "VALUES (?, 'book', ?, 'book', ?, '', 1, '', ?, ?, ?)",
            (row["user_key"], asin, row["title"] or "",
             (row["authors"] if "authors" in keys else None) or "",
             row["requested_at"], row["fulfilled_at"]))
        moved += 1
    return moved, skipped


def _import_verbatim(source: sqlite3.Connection, target: sqlite3.Connection,
                     table: str) -> int:
    """A table whose shape is the same on both sides."""
    rows = _rows(source, table)
    if not rows:
        return 0
    columns = [name for name in rows[0].keys()
               if name in {row["name"] for row in
                           target.execute(f"PRAGMA table_info({table})")}]
    if not columns:
        return 0
    placeholders = ",".join("?" for _ in columns)
    names = ",".join(columns)
    # OR IGNORE, not OR REPLACE: a row already here is the one this service has
    # been serving, and the imported one is by definition older.
    target.executemany(
        f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({placeholders})",
        [tuple(row[name] for name in columns) for row in rows])
    return len(rows)


def run(source_path: str, apply: bool) -> int:
    store.init()
    try:
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"cannot open {source_path}: {exc}", file=sys.stderr)
        return 2
    source.row_factory = sqlite3.Row

    counts: dict[str, int] = {}
    with source, store.db() as target:
        scheme = source.execute(
            "SELECT value FROM meta WHERE key='user_key_scheme'").fetchone() \
            if _has_table(source, "meta") else None
        if not scheme or scheme["value"] != "id":
            # Refused rather than translated. Name-keyed rows would need
            # Jellyfin to say which id each display name belongs to, and
            # importing them as they stand would file everybody's requests
            # under a key nothing looks up.
            print("refusing: that database still keys user state on display "
                  "names. Start the audiobook service once against a "
                  "reachable Jellyfin so it rekeys itself, then run this.",
                  file=sys.stderr)
            return 3

        moved, skipped = _import_requests(source, target)
        counts["requests"] = moved
        if skipped:
            print(f"{skipped} request(s) already here, left as they are")
        for table in _TABLES_MOVED:
            if table == "requests":
                continue
            counts[table] = _import_verbatim(source, target, table)
        if not apply:
            # Everything above ran against the real connection, so rolling
            # back is what makes this a dry run. The source was opened
            # read-only and could not have been touched either way.
            target.rollback()

    for table, count in counts.items():
        print(f"{table}: {count} row(s)")
    print("\nCaches and shelves are not imported: they rebuild themselves, "
          "and a stale similarity graph looks fresh while scoring on fields "
          "that were not being kept when it was written.")
    print("nothing was written; pass --apply to do it for real" if not apply
          else f"imported into {config.DB_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import an audiobook-service database into this one.")
    parser.add_argument("source", help="path to the old nextread.db")
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (without this, nothing is written)")
    args = parser.parse_args()
    return run(args.source, args.apply)


if __name__ == "__main__":
    sys.exit(main())
