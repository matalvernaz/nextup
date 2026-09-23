"""The request ledger and the audiobook caches. One SQLite file, and nothing
in it that cannot be rebuilt.

Delete this database and you lose the record of who asked for what, plus a
similarity graph and a taste model that cost requests to build: the films and
series themselves stay in Radarr and Sonarr, the books stay in Listenarr, and
the library stays in Jellyfin. Nothing here is the only copy of anything.

The ledger is keyed `(user_key, medium, item_key)` rather than on an
identifier alone. Two media can hand out the same number -- TMDB 1399 is a
series and TMDB 1399 is also a film -- and a key that leaves the medium out
would let one of them satisfy a request for the other. A book row is
`(user_key, 'book', asin)`, which is what made merging the audiobook service's
own ledger a copy rather than a reshape.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, logs

log = logs.get("store")

# Bumped whenever the shape of a cached sims payload changes. A stale entry is
# worse than a miss: it looks fresh and silently scores zero on fields that
# were not being kept when it was written.
SIMS_SCHEMA_VERSION = 4

# The same idea for cached Audible products, versioned apart from sims so a
# reshaped product does not throw away a similarity graph that costs one
# request per seed per axis to rebuild.
PRODUCTS_SCHEMA_VERSION = 3

# And again for anything cached from an outside catalogue that is not Audible:
# TMDb's recommendation graph, and a book's community rating. Versioned apart
# for the same reason the two above are -- reshaping one of these must not cost
# a similarity graph that is one request per seed to rebuild.
EXTERNAL_SCHEMA_VERSION = 1

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
    -- Books only, and load-bearing there. An audiobook does not arrive under
    -- the ASIN it was asked for: the marketplace it was found in and the
    -- tagger's marketplace issue different ASINs for the same edition, so
    -- arrival is decided on the title with an author to agree with it.
    authors      TEXT NOT NULL DEFAULT '',
    requested_at REAL NOT NULL,
    fulfilled_at REAL,
    PRIMARY KEY (user_key, medium, item_key)
);
CREATE INDEX IF NOT EXISTS requests_by_day
    ON requests(user_key, medium, requested_at);
CREATE INDEX IF NOT EXISTS requests_by_item
    ON requests(medium, item_key);

-- When an account was last given its day's requests back, per medium.
--
-- A marker rather than a rewrite of the ledger. The alternative -- backdating
-- `requested_at` until the rows fall out of the day's window -- spends the one
-- record of when a thing was actually asked for, which is also what decides
-- when "on its way" becomes "still looking" and how the list is ordered.
CREATE TABLE IF NOT EXISTS allowance_resets (
    user_key TEXT NOT NULL,
    medium   TEXT NOT NULL,
    reset_at REAL NOT NULL,
    PRIMARY KEY (user_key, medium)
);

-- What one account is allowed in a day on one medium, where that differs from
-- what the setting says.
--
-- An override rather than a copy: no row means the configured cap, so raising
-- MOVIE_DAILY_CAP still raises it for everybody who has not been given a
-- number of their own. Storing every account's cap here instead would freeze
-- the household at whatever the setting happened to be the day each account
-- was first seen.
--
-- Zero is a real value and means an allowance of nothing. Absent is the only
-- way to say "the default", which is why clearing deletes the row rather than
-- writing a NULL.
CREATE TABLE IF NOT EXISTS account_caps (
    user_key  TEXT NOT NULL,
    medium    TEXT NOT NULL,
    daily_cap INTEGER NOT NULL,
    set_at    REAL NOT NULL,
    PRIMARY KEY (user_key, medium)
);

-- What one account has told this service about itself, as opposed to what the
-- household has told it about its wiring. Today that is one thing: a personal
-- Hardcover token, which names a person -- `me` answers with the account that
-- issued it -- and reads that person's own ratings and shelves.
--
-- In SCHEMA rather than BOOK_SCHEMA deliberately. Everything in that one is a
-- cache and is DROPPED on a version bump; this is something somebody typed in,
-- and losing it silently would look like a feature that stopped working.
CREATE TABLE IF NOT EXISTS user_settings (
    user_key TEXT NOT NULL,
    name     TEXT NOT NULL,
    value    TEXT NOT NULL,
    set_at   REAL NOT NULL,
    PRIMARY KEY (user_key, name)
);

-- A list somebody uploaded, part way between arriving and being asked for.
--
-- In SCHEMA rather than BOOK_SCHEMA even though every row of it is
-- disposable: a version bump DROPS everything in that one, and the thing this
-- holds is a file a person is in the middle of importing. Losing a cache
-- costs seconds; losing this loses a five-hundred-row match they waited for
-- and have not yet looked at.
--
-- `payload` is the whole batch as JSON -- the parsed rows, what each matched,
-- and afterwards what became of each request. One blob rather than a table of
-- rows because nothing ever queries inside it: it is written once by the
-- matching pass, read whole by the page, and deleted. `done` is out here on
-- its own because it is the one field that changes while somebody is watching,
-- and rewriting the blob per row to move a counter is five hundred writes of
-- everything to record one number.
CREATE TABLE IF NOT EXISTS imports (
    import_id  TEXT PRIMARY KEY,
    user_key   TEXT NOT NULL,
    medium     TEXT NOT NULL,
    state      TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    total      INTEGER NOT NULL DEFAULT 0,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    touched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS imports_by_user
    ON imports(user_key, created_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: The audiobook engine's caches and its own user-scoped state. Separate from
#: the ledger above because none of it is a request: it is an Audible
#: similarity graph, a TF-IDF document model, and the shelves derived from
#: them. All of it is disposable -- a cold cache costs requests and seconds,
#: not correctness.
BOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS sims (
    cache_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audible_aliases (
    source_asin  TEXT PRIMARY KEY,
    audible_asin TEXT NOT NULL,
    resolved_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS external_facts (
    cache_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shelves (
    user_key    TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    computed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    asin        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_vectors (
    item_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    built_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submitted (
    asin         TEXT PRIMARY KEY,
    title        TEXT,
    user_key     TEXT NOT NULL,
    submitted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dismissed (
    user_key     TEXT NOT NULL,
    asin         TEXT NOT NULL,
    dismissed_at REAL NOT NULL,
    PRIMARY KEY (user_key, asin)
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key    TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    seeds       INTEGER,
    owned       INTEGER,
    unowned     INTEGER,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS recommendation_items (
    recommendation_id TEXT PRIMARY KEY,
    run_id            INTEGER NOT NULL,
    user_key          TEXT NOT NULL,
    surface           TEXT NOT NULL,
    item_key          TEXT NOT NULL,
    rank              INTEGER NOT NULL,
    score             REAL NOT NULL,
    source            TEXT NOT NULL,
    reasons           TEXT NOT NULL,
    ranker_version    TEXT NOT NULL,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS recommendation_items_user_run
    ON recommendation_items(user_key, run_id);
CREATE INDEX IF NOT EXISTS recommendation_items_created
    ON recommendation_items(created_at);
CREATE TABLE IF NOT EXISTS feedback_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key          TEXT NOT NULL,
    asin              TEXT NOT NULL,
    action            TEXT NOT NULL,
    recommendation_id TEXT,
    occurred_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_events_user_time
    ON feedback_events(user_key, occurred_at);
CREATE INDEX IF NOT EXISTS feedback_events_time
    ON feedback_events(occurred_at);
CREATE INDEX IF NOT EXISTS feedback_events_recommendation
    ON feedback_events(recommendation_id);
"""


#: One lock per ledger key, so a read-modify-write over the same rows runs once
#: at a time. SQLite serialises the statements; it does not serialise a decision
#: taken between two of them, which is what the allowance check is.
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def key_lock(*parts: str) -> threading.Lock:
    """The lock for one account's requests in one medium.

    Held across "is it already asked for, is there allowance, ask the backend,
    write the row". Those are four statements with three decisions between
    them, so two taps arriving together both read an unspent allowance and both
    spend it. One worker serves this application, so a lock in the process is
    the whole of the fix; a deployment that ever runs more would need the
    reservation to live in the database instead.
    """
    name = "\x00".join(parts)
    with _key_locks_guard:
        return _key_locks.setdefault(name, threading.Lock())


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # Write-ahead logging, so a reader is not held behind a writer. A shelf
    # rebuild is twelve seconds of work that writes as it goes, and under the
    # rollback journal every request arriving during one waits on the lock it
    # takes. Set per connection because it is a property of the database file
    # and reasserting it is free.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        _drop_outdated_caches(conn)
        conn.executescript(SCHEMA)
        conn.executescript(BOOK_SCHEMA)
        _add_missing_columns(conn)
    log.info("store ready at %s", config.DB_PATH)


def _drop_outdated_caches(conn: sqlite3.Connection) -> None:
    """Throw away cached payloads written to a shape this code no longer reads.

    `CREATE TABLE IF NOT EXISTS` will not reshape a table that already exists,
    so a version bump has to DROP. Both constants carried this meaning in the
    audiobook service and the check did not survive the merge into this one --
    which made them decoration: bumping either did nothing, and a reshaped
    payload would have gone on being served for its whole TTL, 720 hours in the
    case of a product.

    Versioned apart on purpose. Reshaping a cached product must not throw away
    a similarity graph that costs one Audible request per seed per axis.
    """
    for key, version, tables in (
        ("sims_schema_version", SIMS_SCHEMA_VERSION,
         ("sims", "doc_vectors", "audible_aliases", "products")),
        ("products_schema_version", PRODUCTS_SCHEMA_VERSION, ("products",)),
        ("external_schema_version", EXTERNAL_SCHEMA_VERSION,
         ("external_facts",)),
    ):
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
        try:
            current = int(row["value"]) if row else None
        except (TypeError, ValueError):
            current = None
        if current == version:
            continue
        dropped = [table for table in tables
                   if conn.execute(
                       "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone()]
        for table in tables:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(version)))
        if row is not None:
            log.info("dropped %s: %s moved from %s to %d",
                     ", ".join(tables), key, row["value"], version)
        elif dropped:
            # Loud, because this is the one path that discards a cache without
            # anybody having changed a version. A database written before this
            # check existed carries no row, and the shape of what is in it
            # cannot be known -- so it goes, and the log says why rather than
            # leaving a cold first shelf looking like a fault.
            log.warning("dropped %s: no %s recorded, so what was cached could "
                        "not be trusted to match this code",
                        ", ".join(sorted(dropped)), key)


#: Columns added to `requests` after the first release. `CREATE TABLE IF NOT
#: EXISTS` will not reshape a table that is already there, so a new column has
#: to be added by name. Each carries its own default, so existing rows read as
#: something rather than as null.
_LATER_COLUMNS = (("authors", "TEXT NOT NULL DEFAULT ''"),)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    present = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
    for name, declaration in _LATER_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {declaration}")
            log.info("added requests.%s", name)


#: Written once the ledger's `user_key` column holds Jellyfin account ids.
USER_KEY_SCHEME = "user_key_scheme"


def user_key_scheme() -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (USER_KEY_SCHEME,)).fetchone()
    return row["value"] if row else "name"


def set_user_key_scheme(scheme: str) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (USER_KEY_SCHEME, scheme))


def nothing_to_rekey() -> bool:
    """Whether any user-scoped table holds a row at all.

    Asked at startup, because the migration onto account ids is fatal when
    Jellyfin cannot be reached and a fresh install has nothing to migrate. A
    first `docker compose up` where Jellyfin is not up yet -- the ordinary
    ordering on a single box -- used to crash-loop the container on a
    rekeying of zero rows.

    Every table, not just `requests`. Asking about that one alone meant an
    account with a shelf, a dismissal or a run and no outstanding request took
    the fresh-install path: the marker was written, the migration never ran,
    and their recommendations stayed under a display name nothing would look
    for again.
    """
    with db() as conn:
        return not any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in USER_SCOPED_TABLES)


#: Every table whose `user_key` names an account. All of them move together in
#: the rekeying below: leaving one behind would rekey somebody's requests and
#: not their shelf, so their list would read empty while their recommendations
#: stayed put -- a half-migrated account is harder to notice than an
#: unmigrated one.
USER_SCOPED_TABLES = ("requests", "submitted", "dismissed", "runs",
                      "recommendation_items", "feedback_events", "shelves")


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
        unmatched: set[str] = set()
        for table in USER_SCOPED_TABLES:
            existing = {row["user_key"] for row in conn.execute(
                f"SELECT DISTINCT user_key FROM {table}")}
            for key in sorted(existing):
                item_id = name_to_id.get(key)
                if item_id is None:
                    if key not in name_to_id.values():
                        unmatched.add(key)
                    continue
                # OR REPLACE rather than a plain UPDATE: `shelves` is keyed on
                # user_key alone, so an account that already has an id-keyed
                # row would otherwise fail the whole migration on a conflict.
                cur = conn.execute(
                    f"UPDATE OR REPLACE {table} SET user_key=? WHERE user_key=?",
                    (item_id, key))
                moved += cur.rowcount
        for key in sorted(unmatched):
            log.warning("rows for %r match no Jellyfin account; "
                        "left as they are", key)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (USER_KEY_SCHEME, "id"))
    log.info("ledger rekeyed onto account ids, %d row(s) moved", moved)
    return moved


def record(user_key: str, medium: str, item_key: str, unit: str,
           title: str, year: str, cost: int, backend_id: str,
           authors: str = "") -> bool:
    """Write down that this account asked for this thing. True when it is new.

    An existing row is left alone rather than refreshed. Asking twice must not
    restart the clock that decides when "on its way" becomes "still looking",
    or a request could be kept looking new indefinitely by tapping it again --
    nor spend a second day's allowance on the same thing.
    """
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO requests (user_key, medium, item_key, unit, title, "
            "year, cost, backend_id, authors, requested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (user_key, medium, item_key) DO NOTHING",
            (user_key, medium, item_key, unit, title, year, cost, backend_id,
             authors, time.time()))
    return cur.rowcount > 0


def outstanding_item_keys(medium: str) -> set[str]:
    """Everything anybody is still waiting on, for one medium.

    Household-wide on purpose: it is used to keep a thing already on order out
    of a recommendation shelf, and whoever asked for it is beside the point.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT item_key FROM requests "
            "WHERE medium=? AND fulfilled_at IS NULL", (medium,)).fetchall()
    return {row["item_key"] for row in rows}


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

    The window starts at the later of `since` and this account's last reset,
    so giving somebody their requests back does not also reach backwards past
    the day boundary and hand them the previous one.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS spent FROM requests "
            "WHERE user_key=? AND medium=? AND requested_at >= MAX(?, "
            "  COALESCE((SELECT reset_at FROM allowance_resets "
            "            WHERE user_key=? AND medium=?), 0))",
            (user_key, medium, since, user_key, medium)).fetchone()
    return int(row["spent"] or 0)


def reset_allowance(user_key: str, medium: str, at: float | None = None) -> float:
    """Give this account its day's requests back on one medium. Returns when.

    Only ever moves forward. A reset is "everything before now is forgiven",
    and one written with an earlier time than the last would un-forgive
    requests that a previous reset had already cleared.
    """
    when = time.time() if at is None else at
    with db() as conn:
        conn.execute(
            "INSERT INTO allowance_resets (user_key, medium, reset_at) "
            "VALUES (?,?,?) ON CONFLICT (user_key, medium) DO UPDATE SET "
            "reset_at=MAX(reset_at, excluded.reset_at)",
            (user_key, medium, when))
        row = conn.execute(
            "SELECT reset_at FROM allowance_resets WHERE user_key=? AND medium=?",
            (user_key, medium)).fetchone()
    return float(row["reset_at"])


def daily_cap(user_key: str, medium: str, configured: int) -> int:
    """This account's allowance on one medium: its own where it has one.

    Here rather than in a caller because there are two request paths -- the
    shared one and the book engine's, which keeps its own allowance
    arithmetic -- and a rule each of them spells out separately is a rule they
    can disagree about. `capabilities` publishing one number while the other
    path refuses on a different one is the whole failure this exists to stop.
    """
    override = cap_override(user_key, medium)
    return configured if override is None else override


def cap_override(user_key: str, medium: str) -> int | None:
    """This account's own daily cap on one medium, or None to use the setting."""
    with db() as conn:
        row = conn.execute(
            "SELECT daily_cap FROM account_caps WHERE user_key=? AND medium=?",
            (user_key, medium)).fetchone()
    return int(row["daily_cap"]) if row else None


def cap_overrides(user_key: str = "") -> dict[tuple[str, str], int]:
    """Every stored cap, or one account's, keyed `(user_key, medium)`.

    One query because the page that shows this draws a whole household against
    every medium, and asking per cell is ten accounts times four media.
    """
    sql = "SELECT user_key, medium, daily_cap FROM account_caps"
    args: tuple = ()
    if user_key:
        sql += " WHERE user_key=?"
        args = (user_key,)
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {(r["user_key"], r["medium"]): int(r["daily_cap"]) for r in rows}


def set_cap(user_key: str, medium: str, daily_cap: int) -> None:
    """Give this account its own daily cap on one medium.

    Refuses a negative for the reason `config._int` does: below zero has no
    meaning here, and it would read as an allowance of nothing while looking
    like a number somebody chose.
    """
    if daily_cap < 0:
        raise ValueError(f"a daily cap cannot be negative, and is {daily_cap}")
    with db() as conn:
        conn.execute(
            "INSERT INTO account_caps (user_key, medium, daily_cap, set_at) "
            "VALUES (?,?,?,?) ON CONFLICT (user_key, medium) DO UPDATE SET "
            "daily_cap=excluded.daily_cap, set_at=excluded.set_at",
            (user_key, medium, int(daily_cap), time.time()))


def clear_cap(user_key: str, medium: str) -> bool:
    """Put this account back on the configured cap. True if it had its own."""
    with db() as conn:
        return conn.execute(
            "DELETE FROM account_caps WHERE user_key=? AND medium=?",
            (user_key, medium)).rowcount > 0


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


def backend_for(medium: str, item_key: str) -> str:
    """A backend row created by this service, if another account still holds it."""
    with db() as conn:
        row = conn.execute(
            "SELECT backend_id FROM requests WHERE medium=? AND item_key=? "
            "AND backend_id != '' LIMIT 1", (medium, item_key)).fetchone()
    return str(row["backend_id"]) if row else ""


def release(user_key: str, medium: str,
            item_key: str) -> tuple[bool, set[str]]:
    """Drop this account's request and say who else is still waiting on it.

    One transaction, because these used to be two statements with a decision
    between them. Two accounts cancelling the same thing at the same moment
    each read the other as still waiting, so each left the acquisition running
    and each deleted its own row -- and nothing pointed at it afterwards. The
    row in Radarr, Sonarr or Listenarr then ran on with no ledger entry to
    explain it or ever call it off.

    `BEGIN IMMEDIATE` rather than the default deferred transaction: the write
    lock has to be held from before the read, or the second caller reads a row
    the first is about to delete.

    - Returns: (whether there was a row to drop, the other accounts still
      outstanding on the same thing).
    """
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existed = conn.execute(
            "SELECT 1 FROM requests WHERE user_key=? AND medium=? AND item_key=?",
            (user_key, medium, item_key)).fetchone() is not None
        if existed:
            conn.execute(
                "DELETE FROM requests "
                "WHERE user_key=? AND medium=? AND item_key=?",
                (user_key, medium, item_key))
        others = {row["user_key"] for row in conn.execute(
            "SELECT user_key FROM requests WHERE medium=? AND item_key=? "
            "AND fulfilled_at IS NULL", (medium, item_key))}
    return existed, others - {user_key}


def waiting(medium: str, item_key: str) -> set[str]:
    """Every account still waiting on this, whoever is asking.

    `others_waiting` excludes the caller, which is the right question for "may
    I cancel my own row". This is the question a deletion asks: the file has
    gone for the whole household, and one unfulfilled row anywhere means
    somebody is owed an acquisition that must keep running.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT user_key FROM requests WHERE medium=? AND item_key=? "
            "AND fulfilled_at IS NULL", (medium, item_key)).fetchall()
    return {row["user_key"] for row in rows}


def drop_settled(medium: str, item_key: str) -> int:
    """Erase every settled ledger row for one thing. Returns how many went.

    Settled only -- an unfulfilled row is somebody's outstanding request and
    deleting it would take away the allowance they spent as well as the row.
    The caller checks `waiting` first and does not get here while any exist;
    the WHERE clause repeats the condition anyway, because this is the one
    statement in the file that reaches across accounts.
    """
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM requests WHERE medium=? AND item_key=? "
            "AND fulfilled_at IS NOT NULL", (medium, item_key))
    return cur.rowcount


def settled_rows(medium: str) -> list[sqlite3.Row]:
    """Every arrived request for one medium, across accounts.

    Books are matched on title and author rather than on a key -- the ASIN a
    book was asked for is not the one it arrives under -- so the book path has
    to look at the rows themselves rather than ask about a key it can build.
    """
    with db() as conn:
        return list(conn.execute(
            "SELECT * FROM requests WHERE medium=? AND fulfilled_at IS NOT NULL",
            (medium,)))


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


# --- The audiobook engine's caches -------------------------------------------
#
# Moved here wholesale when the two services became one. Every one of these is
# disposable: a cold cache costs Audible requests and a slow first shelf, not
# a wrong answer. They are kept in the same file as the ledger because they
# are scoped to the same accounts and there is no second thing to back up.


def _sims_key(asin: str, axis: str) -> str:
    """Cache key. The axis is part of it: one ASIN has a different neighbour
    set per `similarity_type`, and keying on the ASIN alone collides across
    axes."""
    return f"{asin}:{axis}"


def shelf_keys() -> list[str]:
    """Accounts with a persisted shelf, which is to say every account that has
    ever asked for one. Upkeep works from this rather than from the Jellyfin
    account list: nobody wants a shelf built for a household member who has
    never opened the feature."""
    with db() as conn:
        return [row["user_key"] for row in
                conn.execute("SELECT user_key FROM shelves ORDER BY user_key")]


def get_shelf(user_key: str) -> tuple[dict, float] | None:
    """The last shelf computed for this account, and when, or None.

    Kept because the in-memory cache dies with the process and rebuilding
    costs twelve seconds, nine of which is one Jellyfin listing. A restart
    used to hand that bill to whoever opened the screen next.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT payload, computed_at FROM shelves WHERE user_key=?",
            (user_key,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except ValueError:
        return None
    return data, row["computed_at"]


def put_shelf(user_key: str, data: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO shelves(user_key,payload,computed_at) VALUES(?,?,?) "
            "ON CONFLICT(user_key) DO UPDATE SET payload=excluded.payload, "
            "computed_at=excluded.computed_at",
            (user_key, json.dumps(data, default=list), time.time()))


def forget_shelf(user_key: str | None = None) -> None:
    """Drop the persisted shelf, so an invalidation is not undone from disk."""
    with db() as conn:
        if user_key is None:
            conn.execute("DELETE FROM shelves")
        else:
            conn.execute("DELETE FROM shelves WHERE user_key=?", (user_key,))


def get_product(asin: str):
    """One cached Audible product, or None if absent or stale."""
    cutoff = time.time() - config.PRODUCT_TTL_HOURS * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT payload FROM products WHERE asin=? AND fetched_at>?",
            (asin, cutoff)).fetchone()
    return json.loads(row["payload"]) if row else None


def put_product(asin: str, payload) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO products(asin,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(asin) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (asin, json.dumps(payload), time.time()))


def get_sims(asin: str, axis: str):
    """Cached sims for an (ASIN, axis), or None if absent or stale."""
    cutoff = time.time() - config.SIMS_TTL_HOURS * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT payload FROM sims WHERE cache_key=? AND fetched_at>?",
            (_sims_key(asin, axis), cutoff)).fetchone()
    return json.loads(row["payload"]) if row else None


def put_sims(asin: str, axis: str, payload) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO sims(cache_key,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (_sims_key(asin, axis), json.dumps(payload), time.time()))


def get_audible_alias(source_asin: str) -> str | None:
    """A recent resolution, empty string for a known miss, or None when stale."""
    cutoff = time.time() - config.SIMS_TTL_HOURS * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT audible_asin FROM audible_aliases "
            "WHERE source_asin=? AND resolved_at>?",
            (source_asin, cutoff)).fetchone()
    return row["audible_asin"] if row else None


def put_audible_alias(source_asin: str, audible_asin: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO audible_aliases(source_asin,audible_asin,resolved_at) "
            "VALUES(?,?,?) ON CONFLICT(source_asin) DO UPDATE SET "
            "audible_asin=excluded.audible_asin,resolved_at=excluded.resolved_at",
            (source_asin, audible_asin, time.time()))


def get_vectors(kind: str) -> dict:
    """Cached sparse TF-IDF vectors, keyed by Jellyfin item id."""
    with db() as conn:
        rows = conn.execute(
            "SELECT item_id, payload FROM doc_vectors WHERE kind=?",
            (kind,)).fetchall()
    return {r["item_id"]: json.loads(r["payload"]) for r in rows}


def put_vectors(kind: str, vectors: dict) -> None:
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM doc_vectors WHERE kind=?", (kind,))
        conn.executemany(
            "INSERT INTO doc_vectors(item_id,kind,payload,built_at) "
            "VALUES(?,?,?,?)",
            [(k, kind, json.dumps(v), now) for k, v in vectors.items()])


def dismissed_asins(user_key: str) -> set:
    with db() as conn:
        rows = conn.execute(
            "SELECT asin FROM dismissed WHERE user_key=?", (user_key,)).fetchall()
    return {row["asin"] for row in rows}


def dismiss(user_key: str, asin: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO dismissed(user_key,asin,dismissed_at) VALUES(?,?,?) "
            "ON CONFLICT(user_key,asin) DO NOTHING",
            (user_key, asin, time.time()))


def restore(user_key: str, asin: str) -> bool:
    """Undo a dismissal. True when there was one to undo."""
    with db() as conn:
        cur = conn.execute("DELETE FROM dismissed WHERE user_key=? AND asin=?",
                           (user_key, asin))
    return cur.rowcount > 0


# --- outside catalogues that are not Audible ---------------------------------
#
# One table for all of them, keyed by a string each caller namespaces itself.
# Separate functions per source would each need the same three lines and the
# same TTL argument, and the shapes stored are already opaque JSON.


def get_external(cache_key: str, ttl_hours: float):
    """A cached answer from an outside catalogue, or None if absent or stale.

    A *recorded miss* is not None. These caches store misses deliberately --
    a book no rating source has heard of is an answer, and re-asking three
    catalogues about it on every shelf build is what the cache exists to stop.
    Callers tell the two apart by what they stored.
    """
    cutoff = time.time() - ttl_hours * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT payload FROM external_facts "
            "WHERE cache_key=? AND fetched_at>?",
            (cache_key, cutoff)).fetchone()
    return json.loads(row["payload"]) if row else None


def drop_external(cache_key: str) -> None:
    """Forget one cached answer outright.

    A real DELETE rather than storing null. `get_external` would read a stored
    null as "nothing cached" and do the right thing, but only by accident --
    the row would still be there, and the next person to read this code would
    have to work out which of the two nothings it meant.
    """
    with db() as conn:
        conn.execute("DELETE FROM external_facts WHERE cache_key=?",
                     (cache_key,))


def put_external(cache_key: str, payload) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO external_facts(cache_key,payload,fetched_at) "
            "VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload, fetched_at=excluded.fetched_at",
            (cache_key, json.dumps(payload), time.time()))


# --- what one account has told this service about itself ---------------------

#: Only these may be written per account. The same allowlist reasoning as
#: `settings.WRITABLE`: a table that can hold arbitrary names is one where a
#: bug writes something load-bearing.
USER_WRITABLE = frozenset({"HARDCOVER_TOKEN"})

#: Never logged, never rendered back into a form field. All of it, so far --
#: the only thing an account can set is a credential.
USER_SECRET = frozenset({"HARDCOVER_TOKEN"})


def user_setting(user_key: str, name: str) -> str | None:
    """One account's own value for a setting, or None if they have not set it.

    Deliberately NOT falling back to the household's value here. Some callers
    want that fallback and some must not have it -- a personal reading history
    read with somebody else's token is the wrong answer, not a degraded one --
    so the fallback is the caller's decision and is spelled out where it is
    made.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_key=? AND name=?",
            (user_key, name)).fetchone()
    return row["value"] if row else None


def put_user_setting(user_key: str, name: str, value: str) -> None:
    """Set or clear one account's value. An empty value deletes the row.

    Deleting rather than storing an empty string, for the reason `account_caps`
    deletes to clear: absent is the only way to say "I have not set this", and
    an empty string is a value somebody could have meant.
    """
    if name not in USER_WRITABLE:
        raise ValueError(f"{name} is not a per-account setting")
    with db() as conn:
        if not value:
            conn.execute(
                "DELETE FROM user_settings WHERE user_key=? AND name=?",
                (user_key, name))
            return
        conn.execute(
            "INSERT INTO user_settings (user_key, name, value, set_at) "
            "VALUES (?,?,?,?) ON CONFLICT (user_key, name) DO UPDATE SET "
            "value=excluded.value, set_at=excluded.set_at",
            (user_key, name, value, time.time()))


def accounts_with_setting(name: str) -> int:
    """How many accounts have set one thing. For the doctor, never the value."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM user_settings WHERE name=?",
            (name,)).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------
# Imported lists
# --------------------------------------------------------------------------

def put_import(import_id: str, user_key: str, medium: str, state: str,
               total: int, payload) -> None:
    """Write down a list somebody has just uploaded."""
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO imports (import_id, user_key, medium, state, done, "
            "total, payload, created_at, touched_at) VALUES (?,?,?,?,0,?,?,?,?)",
            (import_id, user_key, medium, state, total,
             json.dumps(payload), now, now))


def get_import(import_id: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM imports WHERE import_id=?",
                            (import_id,)).fetchone()


def touch_import(import_id: str, done: int) -> None:
    """Say how many rows of a running import are behind it.

    `touched_at` moves with it, which is what tells a later reader the
    difference between a pass still working and one whose container was
    restarted out from under it.
    """
    with db() as conn:
        conn.execute("UPDATE imports SET done=?, touched_at=? WHERE import_id=?",
                     (done, time.time(), import_id))


def close_import(import_id: str, state: str, payload) -> None:
    """Move an import into its next state, with whatever it worked out."""
    with db() as conn:
        conn.execute(
            "UPDATE imports SET state=?, payload=?, touched_at=? "
            "WHERE import_id=?",
            (state, json.dumps(payload), time.time(), import_id))


def prune_imports(before: float) -> int:
    """Forget imports older than a cutoff. Returns how many went.

    They are somebody's uploaded file and there is no reason to keep one after
    it has been acted on -- but they are deleted on a clock rather than on
    completion, because the report of what was asked for is the most useful
    page in the feature and it should survive being closed and reopened.
    """
    with db() as conn:
        cur = conn.execute("DELETE FROM imports WHERE created_at < ?", (before,))
    return cur.rowcount
