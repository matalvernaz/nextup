"""Recommendation signals from outside the library, and the ways they mislead.

Two new sources: TMDb's neighbour graph for films and shows, and a community
rating for books. Both are optional, both fail into silence, and both have one
failure mode that matters far more than fetching -- for TMDb it is a key that
is set but wrong, and for books it is a search result that is nearly the book.

The near-miss cases here are not invented. They are what openlibrary.org
actually returns for "The Way of Kings": the book, a five-star rating from four
people on "The Way of Kings, Part One", and a four-volume omnibus.
"""
import os

import harness

harness.setup()

from app import config, external_books, recommendations, store, tmdb  # noqa: E402
from app.books import engine  # noqa: E402

check = harness.Check("external recommendation sources")
store.init()

# Kept because the shelf block below replaces them, and the cache block after
# it needs the shipped ones back.
_ORIGINALS = {
    "rating": external_books.rating,
    "cached_rating": external_books.cached_rating,
    "pending": external_books.pending,
    "PROVIDERS": external_books.PROVIDERS,
}


# --- is this the book that was asked for? ------------------------------------

SANDERSON = ["Brandon Sanderson"]

check.that(
    external_books.matches(
        "The Way of Kings", SANDERSON, "The Way of Kings", SANDERSON),
    "the book itself matches")

check.that(
    not external_books.matches(
        "The Way of Kings", SANDERSON,
        "The Way of Kings, Part One", SANDERSON),
    "a part of the book is not the book, even by the same author")

check.that(
    not external_books.matches(
        "The Way of Kings", SANDERSON,
        "Brandon Sanderson's Fantasy Firsts : (the Way of Kings, Mistborn: "
        "the Final Empire, Rithmatist, Alcatraz vs. the Evil Librarians)",
        SANDERSON),
    "an omnibus containing the book is not the book")

check.that(
    external_books.matches(
        "Oathbringer", SANDERSON,
        "Oathbringer: Book Three of the Stormlight Archive", SANDERSON),
    "an edition that spells out the series in its subtitle is still the book")

check.that(
    not external_books.matches(
        "The Way of Kings", SANDERSON, "The Way of Kings", ["Karen Traviss"]),
    "the same title by a different author is a different book")

check.that(
    external_books.matches(
        "The Way of Kings", SANDERSON,
        "The Way of Kings", ["Sanderson, Brandon"]),
    "a surname-first catalogue is not a miss")

check.that(
    not external_books.matches(
        "The Way of Kings", SANDERSON, "The Way of Kings", []),
    "a catalogue record with no author is refused: thin records carry junk "
    "ratings, and a wrong rating is worse than none")

check.that(
    external_books.matches("The Way of Kings", [], "The Way of Kings", SANDERSON),
    "but a book whose own author is unknown is matched on the exact title")

check.that(
    not external_books.matches("Dune", ["Frank Herbert"], "", []),
    "an untitled result is never a match")


# --- which result is taken ---------------------------------------------------

def openlibrary_row(title, authors, average, count):
    return {"title": title, "author_name": authors,
            "ratings_average": average, "ratings_count": count}


def read_openlibrary(row):
    return (row["title"], row["author_name"],
            row["ratings_average"], row["ratings_count"])


picked = external_books._pick(
    [
        openlibrary_row("The Way of Kings", SANDERSON, 4.9, 30),
        openlibrary_row("The Way of Kings", SANDERSON, 4.5, 165),
        openlibrary_row("The Way of Kings, Part One", SANDERSON, 5.0, 400),
    ],
    "The Way of Kings", SANDERSON, "openlibrary", read_openlibrary)
check.that(picked is not None and picked.count == 165,
           "the most-rated real match wins, not the first or the highest")
check.that(picked is not None and picked.source == "openlibrary",
           "the answer says which catalogue gave it")

check.that(
    external_books._pick(
        [openlibrary_row("The Way of Kings", SANDERSON, 5.0, 4)],
        "The Way of Kings", SANDERSON, "openlibrary", read_openlibrary) is None,
    "four readers is not a rating, whatever they scored it")

check.that(
    external_books._pick(
        [openlibrary_row("The Way of Kings", SANDERSON, None, 900)],
        "The Way of Kings", SANDERSON, "openlibrary", read_openlibrary) is None,
    "a result with no average at all is skipped rather than read as zero")


# --- Hardcover, against what the live API actually returns -------------------
#
# These five are the real answer to "The Way of Kings Brandon Sanderson" on
# 2026-09-16. They are kept verbatim because three of them are traps and one of
# them defeats the obvious guard.

HARDCOVER_HITS = [
    {"title": "Brandon Sanderson Sampler: The Way of Kings and Mistborn",
     "rating": 4.607142857142857, "ratings_count": 14,
     "author_names": ["Brandon Sanderson"]},
    {"title": "The Stormlight Archive, Books 1-5 : The Way of Kings Book, "
              "Words of Radiance Book, Oathbringer Book, Rythm of War Book "
              "& Wind and Truth",
     "rating": 1.0, "ratings_count": 1, "author_names": ["Brandon Sanderson"]},
    {"title": "The Way of Kings", "rating": 4.635322976287817,
     "ratings_count": 3669, "author_names": ["Brandon Sanderson"]},
    {"title": "The Way of Kings (3 of 5) [Dramatized Adaptation]",
     "rating": 4.5, "ratings_count": 2,
     "author_names": ["Brandon Sanderson", "Dylan Lynch"]},
    {"title": "The Way of Kings, Part 1", "rating": 4.536111111111111,
     "ratings_count": 180, "author_names": ["Brandon Sanderson"]},
]


def read_hardcover(row):
    return (row["title"], row["author_names"],
            row["rating"], row["ratings_count"])


hardcover_pick = external_books._pick(
    HARDCOVER_HITS, "The Way of Kings", SANDERSON, "hardcover", read_hardcover)
check.that(hardcover_pick is not None and hardcover_pick.count == 3669,
           "the book itself is taken out of a sampler, an omnibus, a "
           "dramatisation and a part")
check.that(hardcover_pick is not None and hardcover_pick.average > 4.6,
           "with its own score, not the sampler's")

# The one that matters. Part 1 is rated by 180 people, so MIN_RATING_COUNT --
# the obvious guard, and the one that saves us from Open Library's four-reader
# rows -- would have let it straight through. Only the part marker stops it,
# and a shelf claiming a partial edition's score for the whole book is exactly
# the wrong rating this module refuses to produce.
check.that(
    180 > external_books.MIN_RATING_COUNT,
    "Part 1 clears the ratings floor, so the floor is not what rejects it")
check.that(
    not external_books.matches(
        "The Way of Kings", SANDERSON, "The Way of Kings, Part 1", SANDERSON),
    "and the part marker is what does")

# `_ilike` is refused by the server and `_eq` is case sensitive; both were
# tried live. The query must be neither.
check.that("_ilike" not in external_books.HARDCOVER_QUERY,
           "the refused operator is gone")
check.that("_eq" not in external_books.HARDCOVER_QUERY,
           "and the case-sensitive one is not what replaced it")
check.that("search" in external_books.HARDCOVER_QUERY,
           "their own search is")


class FakeResponse:
    def __init__(self, body): self._body = body
    def raise_for_status(self): return self
    def json(self): return self._body


class FakeClient:
    def __init__(self, body): self.body = body; self.sent = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, headers=None, json=None):
        self.sent.append(json)
        return FakeResponse(self.body)


saved_httpx_client = external_books.httpx.Client

# A refused operator answers a BARE {"error": ...} with no `errors` array at
# all. Checking only for the array reads that as "no results", which is how a
# query the server rejects outright looks exactly like a book nobody rated.
external_books.httpx.Client = lambda **kw: FakeClient(
    {"error": "ilike and related operations are not permitted on this server."})
check.raises(ValueError,
             lambda: external_books.hardcover_books("Dune", ["Frank Herbert"]),
             "a refusal with no errors array is still raised, not read as empty")

external_books.httpx.Client = lambda **kw: FakeClient(
    {"errors": [{"message": "field 'ratings_count' not found"}]})
check.raises(ValueError,
             lambda: external_books.hardcover_books("Dune", ["Frank Herbert"]),
             "and so is an ordinary GraphQL error")

sent_client = FakeClient(
    {"data": {"search": {"results": {"hits": [
        {"document": HARDCOVER_HITS[2]}]}}}})
external_books.httpx.Client = lambda **kw: sent_client
docs = external_books.hardcover_books("The Way of Kings", SANDERSON)
check.equal([d["title"] for d in docs], ["The Way of Kings"],
            "a good answer is unwrapped from hits[].document")
# Lower case, because the surname comes through the same normalising the
# matching uses. Confirmed live 2026-09-16 that it makes no difference to what
# comes back: "The Way of Kings sanderson" returns the same three rows.
check.that(
    "sanderson" in (sent_client.sent[0]["variables"]["query"] or "").lower(),
    "and the author rides along in the search text, because a bare title "
    "returns every edition anybody has ever listed")

external_books.httpx.Client = saved_httpx_client


# --- what a rating is worth --------------------------------------------------

def rating(average, count):
    return external_books.Rating(average=average, count=count, source="test")


loved = engine.rating_prior(rating(4.6, 20_000))
liked_by_few = engine.rating_prior(rating(4.6, 25))
poor = engine.rating_prior(rating(2.4, 20_000))
middling = engine.rating_prior(rating(engine.RATING_NEUTRAL, 20_000))

check.that(loved > 0.6, "a well-rated book with real volume is a strong yes")
check.that(0 < liked_by_few < loved,
           "the same score from a handful of readers counts for less")
check.that(poor < 0, "a poorly-rated book is pushed back, not merely not lifted")
check.that(abs(middling) < 0.01, "a book at the neutral point says nothing")
check.that(engine.rating_prior(rating(5.0, 10_000_000)) <= 1.0,
           "the prior saturates rather than running away with the shelf")
check.that(engine.rating_prior(rating(0.0, 10_000_000)) >= -1.0,
           "and saturates the other way too")

check.that(
    engine.W_RATING * loved < engine.W_SERIES_NEXT,
    "no rating outranks the next book in a series somebody is partway through")


# --- folding it into a shelf -------------------------------------------------

RATINGS = {
    "Loved": rating(4.8, 50_000),
    "Panned": rating(2.0, 50_000),
}
asked: list[str] = []


def fake_rating(title, authors, token=None):
    asked.append(title)
    return RATINGS.get(title)


external_books.rating = fake_rating
external_books.cached_rating = lambda title, authors: RATINGS.get(title)
external_books.pending = lambda title, authors, token=None: True

rows = [
    {"title": "Panned", "authors": SANDERSON, "score": 12.0, "why": ["by Brandon Sanderson"]},
    {"title": "Loved", "authors": SANDERSON, "score": 11.0, "why": []},
]
left = engine.apply_ratings(rows, budget=10)

check.equal([row["title"] for row in rows], ["Loved", "Panned"],
            "a rating reorders rows the cheap signals could not separate")
check.equal(len(rows), 2, "and drops none of them")
check.equal(left, 8, "each first-time lookup costs one from the budget")
check.that(any("4.8 out of 5" in reason for reason in rows[0]["why"]),
           "a book strangers love says so")
check.that(all("out of 5" not in reason for reason in rows[1]["why"]),
           "a poorly-rated book is pushed back silently rather than shamed")

spent = engine.apply_ratings(
    [{"title": "Loved", "authors": SANDERSON, "score": 1.0, "why": []},
     {"title": "Panned", "authors": SANDERSON, "score": 1.0, "why": []}],
    budget=1)
check.equal(spent, 0, "the budget stops at zero rather than going negative")

external_books.pending = lambda title, authors, token=None: False
asked.clear()
engine.apply_ratings(
    [{"title": "Loved", "authors": SANDERSON, "score": 1.0, "why": []}],
    budget=10)
check.equal(asked, [],
            "a book every catalogue has already answered for costs nothing")


# --- the cache, which is what makes the budget mean anything -----------------
#
# Exercised for real: the store, the TTL, the key, and the one distinction the
# module rests on -- a catalogue that says "never heard of it" is remembered,
# and one that could not be reached is not. Stubbed at the catalogue boundary
# rather than at `rating`, so everything between is the shipped code.

for name in ("rating", "cached_rating", "pending"):
    setattr(external_books, name, _ORIGINALS[name])

fetches: list[str] = []


def answering(result):
    def fetch(title, authors, token=None):
        fetches.append(title)
        return result
    return fetch


MISSING_BOOK = "A Book Nobody Rated"
external_books.PROVIDERS = (("openlibrary", answering(external_books.NOT_ASKED)),)
check.that(external_books.pending(MISSING_BOOK, SANDERSON),
           "a book nothing has been asked about is pending")
check.equal(external_books.rating(MISSING_BOOK, SANDERSON), None,
            "an unreachable catalogue produces no rating")
check.that(external_books.pending(MISSING_BOOK, SANDERSON),
           "and is NOT remembered: an outage must not switch the signal off "
           "for the whole TTL")

fetches.clear()
external_books.PROVIDERS = (
    ("openlibrary", answering(external_books.Answer(True, None))),)
check.equal(external_books.rating(MISSING_BOOK, SANDERSON), None,
            "a catalogue that has never heard of the book answers nothing")
check.equal(len(fetches), 1, "which cost one request")
check.that(not external_books.pending(MISSING_BOOK, SANDERSON),
           "the miss is remembered, so the budget is not spent on it again")
fetches.clear()
check.equal(external_books.rating(MISSING_BOOK, SANDERSON), None,
            "and asking again gives the same answer")
check.equal(fetches, [], "without a second request")

fetches.clear()
FOUND = external_books.Rating(average=4.4, count=900, source="hardcover")
os.environ["HARDCOVER_TOKEN"] = "probe-token"
external_books.PROVIDERS = (
    ("openlibrary", answering(external_books.Answer(True, None))),
    ("hardcover", answering(external_books.Answer(True, FOUND))),
)
SECOND_BOOK = "Only The Second Catalogue Has It"
found = external_books.rating(SECOND_BOOK, SANDERSON)
check.that(found is not None and found.source == "hardcover",
           "a miss on the first catalogue falls through to the next")
check.equal(len(fetches), 2, "having asked both, once each")
fetches.clear()
again = external_books.rating(SECOND_BOOK, SANDERSON)
check.that(again is not None and again.average == 4.4,
           "the hit is cached, down to the score")
check.equal(fetches, [], "and costs nothing the second time")
check.that(not external_books.pending(SECOND_BOOK, SANDERSON),
           "with both catalogues answered, nothing is left to buy")

check.that(external_books.cached_rating(SECOND_BOOK, SANDERSON) is not None,
           "a cached hit is readable without asking anything")
check.equal(external_books.cached_rating("Never Looked Up", SANDERSON), None,
            "and a book nobody has looked up reads as no rating")

# A title differing only in subtitle and punctuation is the same cache entry:
# the key is the normalised main title, which is what the match is on.
check.that(not external_books.pending(
    "Only The Second Catalogue Has It: A Novel", SANDERSON),
    "the cache key is the main title, so an edition's subtitle is not a miss")

os.environ.pop("HARDCOVER_TOKEN")
external_books.PROVIDERS = _ORIGINALS["PROVIDERS"]


# --- which catalogues are available ------------------------------------------

check.equal(external_books.configured_sources(), ("openlibrary",),
            "open library needs no credential, so it is always available")

# Measured against this library: where both answer, Hardcover has an order of
# magnitude more readers behind the same book (3,669 against 165 for The Way of
# Kings; 1,150 against 45 for Skyward). First-wins therefore has to ask it
# first, or a household that supplied a token would only ever have it consulted
# for the books Open Library had never heard of.
check.equal([name for name, _ in external_books.PROVIDERS][0], "hardcover",
            "the catalogue with the readers behind it is asked first")
check.that(
    "openlibrary" in external_books.configured_sources(),
    "and the keyless one still answers, so no token is not a degraded install")

os.environ["HARDCOVER_TOKEN"] = "probe-token"
check.that("hardcover" in external_books.configured_sources(),
           "a token turns hardcover on")
os.environ.pop("HARDCOVER_TOKEN")
check.that("hardcover" not in external_books.configured_sources(),
           "and removing it turns it off again, with no other change")


# --- TMDb: dormant without a key ---------------------------------------------

check.equal(tmdb.configured(), False, "no key means no TMDb")
check.equal(tmdb.recommendations("550", "movie"), [],
            "and asking anyway returns nothing rather than reaching out")
check.equal(tmdb.recommendations("550", "book"), [],
            "a medium TMDb does not carry is refused before any request")


def film(item_id, title, tmdb_id=None, *, progress=0, genres=(), created="2026-01-01T00:00:00Z"):
    item = {
        "Id": item_id,
        "Name": title,
        "Genres": list(genres),
        "People": [],
        "Studios": [],
        "DateCreated": created,
        "UserData": {"Played": progress >= 100, "PlayedPercentage": progress},
    }
    if tmdb_id:
        item["ProviderIds"] = {"Tmdb": tmdb_id}
    return item


check.equal(recommendations._tmdb_id(film("a", "A", "550")), "550",
            "a provider id is read off the item the library already fetched")
check.equal(recommendations._tmdb_id(film("b", "B")), "",
            "and an item without one answers empty rather than raising")

NEIGHBOURS = {"550": ["900", "901"]}
tmdb.configured = lambda: True
tmdb.recommendations = lambda tmdb_id, medium: NEIGHBOURS.get(tmdb_id, [])

watched = film("seen", "Fight Club", "550", progress=100, genres=["Drama"])
first = film("first", "First Neighbour", "900")
second = film("second", "Second Neighbour", "901")
# Nothing in this library's metadata connects either neighbour to the watched
# film: no shared genre, cast or studio. Under the old ranker both were
# invisible, which is the whole point of having a source outside the library.
library = [watched, first, second]

built = recommendations.build(library, medium="movie")
titles = [row["title"] for row in built["recommendations"]]
check.that("First Neighbour" in titles,
           "a title only TMDb connects to something watched now reaches the shelf")
check.equal(titles[:2], ["First Neighbour", "Second Neighbour"],
            "and position one in TMDb's list outranks position two")

top = built["recommendations"][0]
check.equal(top["source"], "neighbour", "the row says where it came from")
check.that(any("Fight Club" in reason for reason in top["reason"]),
           "and names the film somebody actually watched")

check.that(built["ranker_version"].endswith("v2"),
           "the movie ranker version moved, so cached shelves are rebuilt")
check.that(recommendations.ranker_version("series").endswith("v3"),
           "and so did the series one")


harness.cleanup()
raise SystemExit(check.report())
