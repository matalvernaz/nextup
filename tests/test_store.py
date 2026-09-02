"""The ledger: keying, cost arithmetic, and who else is waiting."""
import time

import harness

harness.setup()

from app import store  # noqa: E402

check = harness.Check("store")
store.init()

NOW = time.time()

# The key includes the medium. TMDB hands out the same number to a film and to
# a series, and a key without the medium would let one satisfy the other.
store.record("matt", "movie", "tmdb:1399", "movie", "A film", "2011", 1, "r1")
store.record("matt", "series", "tmdb:1399", "series", "A series", "2011", 1, "s1")
check.that(store.get("matt", "movie", "tmdb:1399") is not None,
           "the film request exists")
check.that(store.get("matt", "series", "tmdb:1399") is not None,
           "the series request exists separately")
check.equal(store.get("matt", "movie", "tmdb:1399")["title"], "A film",
           "and they do not overwrite one another")

# Recording twice must not restart the clock: the wait decides when "on its
# way" becomes "still looking", and a repeated tap would keep it looking new.
first = store.get("matt", "movie", "tmdb:1399")["requested_at"]
time.sleep(0.01)
store.record("matt", "movie", "tmdb:1399", "movie", "Renamed", "2011", 1, "r9")
again = store.get("matt", "movie", "tmdb:1399")
check.equal(again["requested_at"], first, "a repeat does not restart the clock")
check.equal(again["title"], "A film", "and does not rewrite the row")

# Allowance sums cost, not rows.
store.record("kid", "music", "bk:artist:deezer:1", "artist", "Someone", "", 3, "job:1")
store.record("kid", "music", "bk:track:abc", "track", "A song", "", 1, "want:2")
check.equal(store.spent_today("kid", "music", NOW - 3600), 4,
            "an artist and a track spend four, not two")
check.equal(store.spent_today("kid", "movie", NOW - 3600), 0,
            "and spending on music leaves the film counter alone")
check.equal(store.spent_today("kid", "music", NOW + 3600), 0,
            "a cutoff in the future counts nothing")

# Who else is waiting decides whether a cancel may call the acquisition off.
store.record("matt", "movie", "tmdb:500", "movie", "Shared", "", 1, "r5")
store.record("kid", "movie", "tmdb:500", "movie", "Shared", "", 1, "r5")
check.equal(store.others_waiting("matt", "movie", "tmdb:500"), {"kid"},
            "the other waiter is found")
check.equal(store.others_waiting("matt", "movie", "tmdb:1399"), set(),
            "a request nobody else made has no other waiters")

store.mark_arrived("kid", "movie", {"tmdb:500"})
check.equal(store.others_waiting("matt", "movie", "tmdb:500"), set(),
            "somebody whose copy already arrived is not still waiting")

# An arrived request stays visible for a while, then falls off.
rows = store.active("kid", "movie", NOW - 3600)
check.equal(len(rows), 1, "a just-arrived request is still listed")
rows = store.active("kid", "movie", time.time() + 3600)
check.equal(len(rows), 0, "and is gone once the window has passed")

check.that(store.forget("matt", "movie", "tmdb:500"), "a row can be forgotten")
check.that(not store.forget("matt", "movie", "tmdb:500"),
           "and forgetting it twice reports nothing to do")

# The 2026-09-02 rekey: the ledger moves off casefolded display names and
# onto Jellyfin account ids, so renaming an account no longer empties that
# listener's list and recreating a name no longer inherits a stranger's.
store.record("renamed", "movie", "tmdb:9001", "movie", "Theirs", "2020", 1, "r9")
store.record("gone-away", "movie", "tmdb:9002", "movie", "Orphan", "2020", 1, "r8")
check.equal(store.user_key_scheme(), "name", "an unmigrated ledger says so")

moved = store.rekey_users({"renamed": "u-renamed"})
check.equal(moved, 1, "only the rows whose account was found are moved")
check.that(store.get("u-renamed", "movie", "tmdb:9001") is not None,
           "the request now answers to the account id")
check.that(store.get("renamed", "movie", "tmdb:9001") is None,
           "and no longer to the old name")
# Left where they are rather than thrown away: the account may be renamed
# back, and losing somebody's outstanding requests to tidy a key is worse.
check.that(store.get("gone-away", "movie", "tmdb:9002") is not None,
           "rows for an account nobody can name are kept")
check.equal(store.user_key_scheme(), "id", "the ledger records that it moved")
check.equal(store.rekey_users({"renamed": "u-renamed"}), 0,
            "and a second run does nothing")

harness.cleanup()
raise SystemExit(check.report())
