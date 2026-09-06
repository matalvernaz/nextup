"""The merged store: books in the one ledger, and the audiobook caches.

The point of this file is that the audiobook service's own tables came across
without either half losing anything -- a book request is an ordinary ledger
row with `medium='book'`, and everything that was a cache is still a cache.
"""
import time

import harness

harness.setup()

from app import store  # noqa: E402

check = harness.Check("book store")
store.init()

# A book is a medium like any other, so an ASIN and a TMDB id cannot collide.
store.record("matt", "book", "B01", "book", "A Novel", "", 1, "la1",
             authors="Somebody")
store.record("matt", "movie", "B01", "movie", "A Film", "2011", 1, "r1")
check.equal(store.get("matt", "book", "B01")["title"], "A Novel",
            "a book row is keyed by its medium")
check.equal(store.get("matt", "movie", "B01")["title"], "A Film",
            "and the same key in another medium is a different request")

# Authors are kept because the ASIN is not enough to recognise the book when
# it lands: it arrives tagged with whichever ASIN the other marketplace
# issued for the same edition, so arrival is decided on the title with an
# author to agree with it.
check.equal(store.get("matt", "book", "B01")["authors"], "Somebody",
            "the authors are kept alongside the request")

# The insert reports whether it did anything, so a second tap on the same book
# can be told from a first one without a second read.
check.equal(store.record("kadija", "book", "B02", "book", "Another", "", 1, ""),
            True, "a new request reports itself as new")
check.equal(store.record("kadija", "book", "B02", "book", "Another", "", 1, ""),
            False, "and asking twice does not")

# Household-wide, because it keeps a book already on order out of everybody's
# shelf and whoever asked for it is beside the point.
check.equal(store.outstanding_item_keys("book"), {"B01", "B02"},
            "outstanding book keys span every account")
store.mark_arrived("matt", "book", {"B01"})
check.equal(store.outstanding_item_keys("book"), {"B02"},
            "and a book that arrived is no longer outstanding")
check.equal(store.outstanding_item_keys("music"), set(),
            "a medium nobody has asked for is empty, not absent")

# --- The caches. All disposable, all still here. -----------------------------

store.put_sims("B01", "RawSimilarities", [{"asin": "B09"}])
check.equal(store.get_sims("B01", "RawSimilarities"), [{"asin": "B09"}],
            "a similarity set round-trips")
check.that(store.get_sims("B01", "InTheSameSeries") is None,
           "and the axis is part of the key, so axes do not collide")

store.put_product("B01", {"title": "A Novel"})
check.equal(store.get_product("B01")["title"], "A Novel",
            "a cached Audible product round-trips")

store.put_audible_alias("B0SOURCE", "B0AUDIBLE")
check.equal(store.get_audible_alias("B0SOURCE"), "B0AUDIBLE",
            "an ASIN alias round-trips")
store.put_audible_alias("B0MISS", "")
check.equal(store.get_audible_alias("B0MISS"), "",
            "and a known miss is an empty string, not an absence")

store.put_shelf("matt", {"own": [1, 2]})
shelf, computed_at = store.get_shelf("matt")
check.equal(shelf, {"own": [1, 2]}, "a shelf round-trips")
check.that(computed_at <= time.time(), "with the time it was computed")
store.forget_shelf("matt")
check.that(store.get_shelf("matt") is None,
           "and dropping it is not undone from disk")

store.put_vectors("book", {"item-1": {"dragon": 0.5}})
check.equal(store.get_vectors("book"), {"item-1": {"dragon": 0.5}},
            "document vectors round-trip")
store.put_vectors("book", {"item-2": {"sword": 0.25}})
check.equal(store.get_vectors("book"), {"item-2": {"sword": 0.25}},
            "and rebuilding a kind replaces it rather than adding to it")

store.dismiss("matt", "B03")
check.equal(store.dismissed_asins("matt"), {"B03"}, "a dismissal is recorded")
check.equal(store.dismissed_asins("kadija"), set(),
            "and belongs to the account that made it")
check.equal(store.restore("matt", "B03"), True, "restoring undoes it")
check.equal(store.restore("matt", "B03"), False,
            "and restoring nothing reports that there was nothing")

# --- First boot ---------------------------------------------------------------
#
# The rekeying onto account ids is deliberately fatal when Jellyfin cannot be
# asked, because serving id-keyed lookups over a name-keyed ledger reads every
# list as empty and every allowance as unspent. An empty ledger has nothing to
# migrate, though, and a fresh install whose Jellyfin is not up yet used to
# crash-loop on a migration of zero rows.
check.equal(store.nothing_to_rekey(), False, "this ledger has rows in it")
with store.db() as conn:
    conn.execute("DELETE FROM requests")
check.equal(store.nothing_to_rekey(), True, "an emptied ledger says so")

# Every user-scoped table, not only `requests`. Asked about that one alone, an
# account with a shelf and no outstanding request took the fresh-install path:
# the marker was written, the migration never ran, and their recommendations
# stayed under a display name nothing would look for again.
with store.db() as conn:
    conn.execute("INSERT INTO shelves (user_key, payload, computed_at) "
                 "VALUES ('matt', '{}', 0)")
check.equal(store.nothing_to_rekey(), False,
            "a shelf with no request is still something to migrate")
with store.db() as conn:
    conn.execute("DELETE FROM shelves")

# --- a cache version bump has to actually throw the cache away ---------------
# Both constants say in their own comments that this is what they are for, and
# the check did not survive the merge, which made bumping either one a no-op.
with store.db() as conn:
    conn.execute("INSERT OR REPLACE INTO products (asin, payload, fetched_at) "
                 "VALUES ('B0OLD', '{}', 0)")
    conn.execute("INSERT INTO meta(key,value) VALUES('products_schema_version','1') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
store.init()
with store.db() as conn:
    left = conn.execute("SELECT 1 FROM products WHERE asin='B0OLD'").fetchone()
    marked = conn.execute(
        "SELECT value FROM meta WHERE key='products_schema_version'").fetchone()
check.equal(left, None, "a payload cached under an older shape is dropped")
check.equal(marked["value"], str(store.PRODUCTS_SCHEMA_VERSION),
            "and the version it was dropped for is written down")

harness.cleanup()
raise SystemExit(check.report())
