"""Books through the shared request path, which is not the same code as books
through their own.

The audiobook engine has its own request path and its own suite, and both are
still here. What this file covers is the seam: `wants.want`, `wants.states` and
`wants.cancel` are what the web page and `/api/v1` call, and every one of them
has to hand a book to that engine rather than to the arm it would otherwise
fall through to.

Two bugs this exists to keep fixed, both of which passed every other test:

* `states` fell through to music, so a book row was asked about by buskarr
  under an empty backend id -- which answers nothing, so an arrived book read
  "on its way" for good.
* the adapter's refusal is its own exception class, because it cannot import
  the module that calls it without a cycle. Untranslated, a capped account's
  fourth book left as a 500 rather than as a sentence.
"""
import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid:4545",
              BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              BOOK_DAILY_CAP="2")

from app import backends, buskarr, jellyfin, media, store, wants  # noqa: E402
from app.books import adapter  # noqa: E402

check = harness.Check("books as a medium")
store.init()

backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media.jellyfin.library_ids = lambda medium: ["lib-" + medium]
media.forget()

USER = jellyfin.User(id="u1", name="matt", is_admin=False)

check.that(media.BOOK in media.available(), "books are offered")
check.equal(sorted(media.available()[media.BOOK].units), ["book", "series"],
            "with a unit for one book and one for the rest of a series")

# --- asking ------------------------------------------------------------------
asked = []
adapter.wants.want = lambda user, asin, title="", rec=None: (
    asked.append((user.key, asin)) or ("on_its_way", f"Asked for {title}."))
adapter.shelves.forget_asin = lambda asin: None

state, message = wants.want(USER, "book", "B0ONE", "book",
                            {"title": "A Novel"})
check.equal(state, "on_its_way", "a book request is accepted")
check.equal(message, "Asked for A Novel.", "with the engine's own words")
check.equal(asked, [("u1", "B0ONE")], "and reaches the book engine")

# A refusal has to arrive as this module's own Denied, or every caller that
# catches wants.Denied lets it out as a server error instead of a sentence.
def refuse(user, asin, title="", rec=None):
    raise adapter.wants.Denied("That is 2 books today.")


adapter.wants.want = refuse
check.raises(wants.Denied,
             lambda: wants.want(USER, "book", "B0TWO", "book", {}),
             "a refusal from the book engine is a refusal here")
try:
    wants.want(USER, "book", "B0TWO", "book", {})
except wants.Denied as denied:
    check.that("2 books today" in str(denied),
               "carrying the reason a person can act on")

# --- reporting ---------------------------------------------------------------
#
# The book is in the library, under a *different* ASIN from the one asked for,
# which is the ordinary case rather than an edge one.
store.record("u1", "book", "B0ASKED", "book", "Splinter Angel: Book 1", "", 1,
             "", authors='["A N Other"]')
store.record("u1", "movie", "tmdb:1", "movie", "A Film", "2011", 1, "r1")

buskarr_asked = []
buskarr.state = lambda backend_id: buskarr_asked.append(backend_id) or None
# The index the engine builds: owned ASINs, and normalised titles to
# normalised author sets. "A N Other" normalises to "an other" -- initials
# collapse -- and getting that wrong here is what made this assertion fail
# the first time, which is worth a comment because the same mistake in
# production would read as a book that never arrives.
adapter.shelves.owned_index = lambda user: (
    {"B0SOMETHINGELSE"}, {"splinter angel book 1": {"an other"}})

rows = {row["itemKey"]: row for row in wants.states(USER)}
check.equal(buskarr_asked, [],
            "buskarr is never asked about a book, whatever its backend id")
check.that("B0ASKED" in rows, "a book request is on the list")
check.equal(rows["B0ASKED"]["medium"], "book", "as a book")
check.equal(rows["B0ASKED"]["state"], "in_library",
            "and reads as arrived on title and author, not on the ASIN it was "
            "asked for -- which the library will never carry")
check.that("tmdb:1" in rows, "the other media are still on the same list")

# Ordered as one list rather than two concatenated ones.
ordered = [row["requestedAt"] for row in wants.states(USER)]
check.equal(ordered, sorted(ordered, reverse=True),
            "the merged list is newest first")

# With no book row, the book path is not asked at all: it costs a Jellyfin
# listing of the whole audiobook library.
consulted = []
adapter.shelves.owned_index = lambda user: consulted.append(user.key) or (set(), {})
store.forget("u1", "book", "B0ASKED")
wants.states(USER)
check.equal(consulted, [],
            "and an account with no book request pays nothing for one")

# --- the series row says what state the series is in -------------------------
check.equal(adapter._series_detail(have=3, on_order=0, missing=0),
            "You already have all 3 that Audible lists.",
            "a complete series says so")
check.that("2 to ask for" in adapter._series_detail(have=3, on_order=1,
                                                    missing=2),
           "and an incomplete one says how much is left")

harness.cleanup()
raise SystemExit(check.report())
