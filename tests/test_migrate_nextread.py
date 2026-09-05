"""Importing an audiobook-service database, against one built like the real one."""
import sqlite3

import harness

DB = harness.setup()

from app import migrate_nextread, store  # noqa: E402

check = harness.Check("migrate nextread")

SOURCE = DB.replace(".db", "-source.db")
old = sqlite3.connect(SOURCE)
old.executescript("""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE requests (
    user_key TEXT NOT NULL, asin TEXT NOT NULL, title TEXT, authors TEXT,
    requested_at REAL NOT NULL, fulfilled_at REAL,
    PRIMARY KEY (user_key, asin));
CREATE TABLE dismissed (
    user_key TEXT NOT NULL, asin TEXT NOT NULL, dismissed_at REAL NOT NULL,
    PRIMARY KEY (user_key, asin));
CREATE TABLE sims (
    cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL);
INSERT INTO requests VALUES
    ('u-matt', 'B0OLD', 'An Older Book', '["Somebody"]', 100, NULL),
    ('u-matt', 'B0DONE', 'A Finished One', NULL, 90, 95),
    ('u-kadija', 'B0HERS', 'Hers', '["Another"]', 80, NULL);
INSERT INTO dismissed VALUES ('u-matt', 'B0HIDDEN', 70);
INSERT INTO sims VALUES ('B0OLD:RawSimilarities', '[]', 60);
""")
old.commit()
old.close()

# --- a name-keyed database is refused, not translated ------------------------
#
# Importing those rows as they stand would file everybody's requests under a
# key nothing looks up, and translating them needs Jellyfin.
check.equal(migrate_nextread.run(SOURCE, apply=False), 3,
            "a database still keyed on display names is refused")

with sqlite3.connect(SOURCE) as conn:
    conn.execute("INSERT INTO meta VALUES ('user_key_scheme', 'id')")

# --- a dry run writes nothing ------------------------------------------------
check.equal(migrate_nextread.run(SOURCE, apply=False), 0, "a dry run succeeds")
check.that(store.get("u-matt", "book", "B0OLD") is None,
           "and writes nothing at all")

# --- the real thing ----------------------------------------------------------
check.equal(migrate_nextread.run(SOURCE, apply=True), 0, "the import succeeds")

imported = store.get("u-matt", "book", "B0OLD")
check.that(imported is not None, "an outstanding request came across")
check.equal(imported["medium"], "book", "keyed on the medium it now needs")
check.equal(imported["title"], "An Older Book", "with its title")
check.equal(imported["authors"], '["Somebody"]',
            "and its authors, which is what decides arrival when the ASIN "
            "cannot")
check.equal(imported["requested_at"], 100,
            "and the time it was originally asked for, not the time it moved")

finished = store.get("u-matt", "book", "B0DONE")
check.equal(finished["fulfilled_at"], 95,
            "a fulfilled request stays fulfilled")
check.that(store.get("u-kadija", "book", "B0HERS") is not None,
           "every account's rows come across, not just one")
check.equal(store.dismissed_asins("u-matt"), {"B0HIDDEN"},
            "a hidden book stays hidden")

# --- the caches are left behind on purpose -----------------------------------
with store.db() as conn:
    cached = conn.execute("SELECT COUNT(*) AS n FROM sims").fetchone()["n"]
check.equal(cached, 0,
            "a similarity graph is not imported: it rebuilds itself, and a "
            "stale one looks fresh while scoring on fields that were not "
            "being kept when it was written")

# --- running it twice is safe ------------------------------------------------
with store.db() as conn:
    conn.execute("UPDATE requests SET title='Renamed here' "
                 "WHERE item_key='B0OLD'")
check.equal(migrate_nextread.run(SOURCE, apply=True), 0, "a second run works")
check.equal(store.get("u-matt", "book", "B0OLD")["title"], "Renamed here",
            "and does not overwrite the row this service has been serving")

harness.cleanup()
raise SystemExit(check.report())
