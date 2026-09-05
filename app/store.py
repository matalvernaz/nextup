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
        conn.executescript(SCHEMA)
        conn.executescript(BOOK_SCHEMA)
        _add_missing_columns(conn)
    log.info("store ready at %s", config.DB_PATH)


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


def ledger_is_empty() -> bool:
    """Whether there is any request at all, of any medium, from anybody.

    Asked at startup, because the migration onto account ids is fatal when
    Jellyfin cannot be reached and a fresh install has nothing to migrate. A
    first `docker compose up` where Jellyfin is not up yet -- the ordinary
    ordering on a single box -- used to crash-loop the container on a
    rekeying of zero rows.
    """
    with db() as conn:
        return conn.execute("SELECT 1 FROM requests LIMIT 1").fetchone() is None


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
