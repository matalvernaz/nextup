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
# Stated rather than arrived at by letting a Jellyfin call fail. An index built
# during an outage is not an empty library, and `owned_index` says so by
# raising now, so a test that wants an empty one has to ask for it.
media.jellyfin.owned_index = lambda: jellyfin.Owned(
    frozenset(), frozenset(), frozenset(), {})
media.forget()

USER = jellyfin.User(id="u1", name="matt", is_admin=False)

check.that(media.BOOK in media.available(), "books are offered")
check.equal(sorted(media.available()[media.BOOK].units), ["book", "series"],
            "with a unit for one book and one for the rest of a series")

# --- one search-hit shape for all four media ---------------------------------
# The results template posts `itemKey` into the hidden field its Ask button
# carries, and reads `overview` for the line under the title. The book adapter
# spelled those `item_key` and `detail`, so every book hit posted an empty
# identifier and the ask came back "could not be identified well enough" -- a
# refusal that reads like a catalogue problem rather than like this bug.
adapter.book_search.search = lambda user, query, limit=None: [{
    "asin": "B0HIT", "title": "A Novel", "authors": ["An Author"],
    "narrators": ["A Narrator"], "owned": False, "requested": True,
}]
hits = wants.search("novel", media.BOOK, "book", USER)
check.equal(len(hits), 1, "a book search returns its hits")
check.equal(hits[0].get("itemKey"), "B0HIT",
            "under the same key name the other three media use")
check.that("item_key" not in hits[0],
           "and not under the one the template does not read")
check.that("overview" in hits[0] and "detail" not in hits[0],
           "with the description named the way the template reads it")
check.equal(hits[0].get("requested"), True,
            "a book already asked for says so, rather than offering the button")

# `plan` answers with the rows, not with counts. Reading `missing` as a number
# raised TypeError for every series that had a gap, which is the only case this
# search is for.
adapter.book_series.plan = lambda user, name: {
    "series": "A Series", "have": [{}, {}, {}], "onOrder": [{}],
    "missing": [{"title": "Book Four"}, {"title": "Book Five"}],
}
series_hits = wants.search("a series", media.BOOK, "series", USER)
check.equal(len(series_hits), 1, "a series search answers with one row")
check.equal(series_hits[0].get("itemKey"), "A Series",
            "keyed by the name the library spells it with")
check.equal(series_hits[0].get("owned"), False,
            "not owned while anything is still missing")
check.that("2 to ask for" in series_hits[0].get("overview", ""),
           "and it counts the rows rather than trying to read them as a number")

# --- and the same shape from every adapter, not just this one ----------------
# Checked together because the defect was a divergence, and a divergence is
# only visible when the four are read side by side.
from app import buskarr as _buskarr, radarr as _radarr, sonarr as _sonarr  # noqa: E402

REQUIRED = ("itemKey", "medium", "unit", "title")
built = {
    "movie": _radarr._result({"tmdbId": 1, "title": "A Film", "year": 2001},
                             frozenset()),
    "series": _sonarr._result({"tvdbId": 2, "title": "A Show", "year": 2002},
                              frozenset()),
    "track": _buskarr._result({"title": "A Song", "artist": "A Band"}, "track"),
    "book": hits[0],
    "book series": series_hits[0],
}
for name, hit in built.items():
    missing_keys = [key for key in REQUIRED if key not in hit]
    check.equal(missing_keys, [], f"a {name} hit carries every shared key")

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
