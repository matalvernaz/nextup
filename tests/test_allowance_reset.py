"""Giving one account its day's requests back.

A cap that can only be waited out is a cap with no override, and the only
override available before this was editing the ledger by hand -- backdating
`requested_at` on rows that had not moved, which makes every request it
touches look older than it is and reorders the list they appear in.

So the reset is its own fact rather than a rewrite of the requests: a marker
saying when this account's allowance was last given back, which the spend
query floors its window at. The ledger keeps saying what actually happened.
"""
import time

import harness

harness.setup()

from app import store  # noqa: E402  (after harness.setup)

check = harness.Check("allowance reset")
store.init()

DAY = 24 * 3600
NOW = time.time()


def window() -> float:
    return time.time() - DAY


# Three books today, which is the whole of a default day's allowance.
for index in range(3):
    store.record("alex", "book", f"asin{index}", "book",
                 f"Book {index}", "", 1, "")
store.record("alex", "movie", "tmdb:1", "movie", "A film", "", 1, "")
store.record("someone-else", "book", "asin0", "book", "Book 0", "", 1, "")

check.equal(store.spent_today("alex", "book", window()), 3,
            "the three books count against the day before any reset")

store.reset_allowance("alex", "book")

check.equal(store.spent_today("alex", "book", window()), 0,
            "after a reset the day reads as unspent")

# The point of a marker rather than a rewrite: the requests are still there,
# still saying when they were made, so the list and its ordering are untouched.
rows = store.active("alex", "book")
check.equal(len(rows), 3, "and all three requests survive the reset")
check.that(all(row["requested_at"] >= NOW for row in rows),
           "with the times they were actually made, not backdated ones")

# A reset is one account's and one medium's. Resetting books must not hand the
# same account a fresh set of films, nor anybody else a fresh set of books.
check.equal(store.spent_today("alex", "movie", window()), 1,
            "the same account's other medium is untouched")
check.equal(store.spent_today("someone-else", "book", window()), 1,
            "and another account's books are untouched")

# What a reset does not do is make the rest of the day free.
store.record("alex", "book", "asin-after", "book", "Later", "", 1, "")
check.equal(store.spent_today("alex", "book", window()), 1,
            "a request made after the reset counts again")

# Resetting twice is not an error and does not go backwards.
store.reset_allowance("alex", "book")
check.equal(store.spent_today("alex", "book", window()), 0,
            "a second reset clears what was spent since the first")

# The floor is the later of the two bounds, never the earlier: a reset that
# happened more than a day ago must not widen the window back past it.
old = time.time() - (3 * DAY)
store.reset_allowance("stale", "book", at=old)
store.record("stale", "book", "asin-x", "book", "A book", "", 1, "")
check.equal(store.spent_today("stale", "book", window()), 1,
            "a reset older than the window does not reopen it")

harness.cleanup()
raise SystemExit(check.report())
