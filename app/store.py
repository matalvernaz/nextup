"""The request ledger. One SQLite file, and nothing in it that cannot be rebuilt.

Delete this database and you lose the record of who asked for what: the films
and series themselves stay in Radarr and Sonarr, and the library stays in
Jellyfin. Nothing here is the only copy of anything.

The ledger is keyed `(user_key, medium, item_key)` rather than on an
identifier alone. Two media can hand out the same number -- TMDB 1399 is a
series and TMDB 1399 is also a film -- and a key that leaves the medium out
would let one of them satisfy a request for the other.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, logs

log = logs.get("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    user_key     TEXT NOT NULL,
    medium       TEXT NOT NULL,
    item_key     TEXT NOT NULL,
    unit         TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    year         TEXT NOT NULL DEFAULT '',
    cost         INTEGER NOT NULL DEFAULT 1,
    backend_id   TEXT NOT NULL DEFAULT '',
    requested_at REAL NOT NULL,
    fulfilled_at REAL,
    PRIMARY KEY (user_key, medium, item_key)
);
CREATE INDEX IF NOT EXISTS requests_by_day
    ON requests(user_key, medium, requested_at);
CREATE INDEX IF NOT EXISTS requests_by_item
    ON requests(medium, item_key);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
    log.info("store ready at %s", config.DB_PATH)


#: Written once the ledger's `user_key` column holds Jellyfin account ids.
USER_KEY_SCHEME = "user_key_scheme"


def user_key_scheme() -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (USER_KEY_SCHEME,)).fetchone()
    return row["value"] if row else "name"


def rekey_users(name_to_id: dict[str, str]) -> int:
    """Move the ledger from casefolded display names onto account ids.

    Run once, from the service's startup, because it needs Jellyfin to say
    which id each name belongs to and the store cannot ask.

    Rows whose name no longer matches an account are left alone and logged:
    the account may be renamed back, and throwing away somebody's outstanding
    requests to tidy a key is a worse answer than leaving them where they are.

    - Parameter name_to_id: casefolded display name to Jellyfin account id.
    - Returns: how many rows moved.
    """
    moved = 0
    with db() as conn:
        if conn.execute("SELECT value FROM meta WHERE key=?",
                        (USER_KEY_SCHEME,)).fetchone():
            return 0
        existing = {row["user_key"] for row in
                    conn.execute("SELECT DISTINCT user_key FROM requests")}
        for key in sorted(existing):
            item_id = name_to_id.get(key)
            if item_id is None:
                if key not in name_to_id.values():
                    log.warning(
                        "ledger rows for %r match no Jellyfin account; left as they are",
                        key)
                continue
            cur = conn.execute(
                "UPDATE requests SET user_key=? WHERE user_key=?", (item_id, key))
            moved += cur.rowcount
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (USER_KEY_SCHEME, "id"))
    log.info("ledger rekeyed onto account ids, %d row(s) moved", moved)
    return moved


def record(user_key: str, medium: str, item_key: str, unit: str,
           title: str, year: str, cost: int, backend_id: str) -> None:
    """Write down that this account asked for this thing.

    An existing row is left alone rather than refreshed. Asking twice must not
    restart the clock that decides when "on its way" becomes "still looking",
    or a request could be kept looking new indefinitely by tapping it again.
    """
    with db() as conn:
        conn.execute(
            "INSERT INTO requests (user_key, medium, item_key, unit, title, "
            "year, cost, backend_id, requested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (user_key, medium, item_key) DO NOTHING",
            (user_key, medium, item_key, unit, title, year, cost, backend_id,
             time.time()))


def get(user_key: str, medium: str, item_key: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM requests WHERE user_key=? AND medium=? AND item_key=?",
            (user_key, medium, item_key)).fetchone()


def active(user_key: str, medium: str | None = None,
           arrived_since: float = 0.0) -> list[sqlite3.Row]:
    """This account's requests worth showing: outstanding, plus just-arrived.

    An arrival is the news. Dropping a request the moment its film lands means
    the only way to learn it landed is to notice it missing, so a fulfilled
    row stays visible for a while and then falls off by itself.
    """
    sql = ("SELECT * FROM requests WHERE user_key=? "
           "AND (fulfilled_at IS NULL OR fulfilled_at >= ?)")
    args: list = [user_key, arrived_since]
    if medium:
        sql += " AND medium=?"
        args.append(medium)
    with db() as conn:
        return list(conn.execute(sql + " ORDER BY requested_at DESC", args))


def outstanding_keys(user_key: str, medium: str) -> set[str]:
    """Item keys this account is still waiting on, for one medium."""
    with db() as conn:
        rows = conn.execute(
            "SELECT item_key FROM requests WHERE user_key=? AND medium=? "
            "AND fulfilled_at IS NULL", (user_key, medium)).fetchall()
    return {row["item_key"] for row in rows}


def spent_today(user_key: str, medium: str, since: float) -> int:
    """Allowance this account has used on this medium since `since`.

    Sums `cost`, not rows: an artist and a single track are both one row and
    are not the same request, which is the whole reason the column exists.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS spent FROM requests "
            "WHERE user_key=? AND medium=? AND requested_at >= ?",
            (user_key, medium, since)).fetchone()
    return int(row["spent"] or 0)


def mark_arrived(user_key: str, medium: str, item_keys: set[str]) -> None:
    """Close the requests whose media are now in the library."""
    if not item_keys:
        return
    now = time.time()
    with db() as conn:
        conn.executemany(
            "UPDATE requests SET fulfilled_at=? "
            "WHERE user_key=? AND medium=? AND item_key=? "
            "AND fulfilled_at IS NULL",
            [(now, user_key, medium, key) for key in item_keys])


def forget(user_key: str, medium: str, item_key: str) -> bool:
    """Take one request off this account's list. True if there was one."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM requests WHERE user_key=? AND medium=? AND item_key=?",
            (user_key, medium, item_key))
    return cur.rowcount > 0


def others_waiting(user_key: str, medium: str, item_key: str) -> set[str]:
    """Other accounts still outstanding on the same thing.

    Cancelling calls an acquisition off, and the row in Radarr or Sonarr
    belongs to the household rather than to whoever asked last. This is what
    stops one person's change of mind deleting somebody else's film.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT user_key FROM requests WHERE medium=? AND item_key=? "
            "AND user_key<>? AND fulfilled_at IS NULL",
            (medium, item_key, user_key)).fetchall()
    return {row["user_key"] for row in rows}
