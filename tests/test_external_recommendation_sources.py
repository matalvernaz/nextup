"""Recommendation signals from outside the library, and the ways they mislead.

Two new sources: TMDb's neighbour graph for films and shows, and a community
rating for books. Both are optional, both fail into silence, and both have one
failure mode that matters far more than fetching -- for TMDb it is a key that
is set but wrong, and for books it is a search result that is nearly the book.

The near-miss cases here are not invented. They are what openlibrary.org
actually returns for "The Way of Kings": the book, a five-star rating from four
people on "The Way of Kings, Part One", and a four-volume omnibus.
"""
import harness

harness.setup()

from app import config, external_books, recommendations, store, tmdb  # noqa: E402
from app.books import engine  # noqa: E402

check = harness.Check("external recommendation sources")
store.init()


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


def fake_rating(title, authors):
    asked.append(title)
    return RATINGS.get(title)


external_books.rating = fake_rating
external_books.cached_rating = lambda title, authors: RATINGS.get(title)
external_books.pending = lambda title, authors: True

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

external_books.pending = lambda title, authors: False
asked.clear()
engine.apply_ratings(
    [{"title": "Loved", "authors": SANDERSON, "score": 1.0, "why": []}],
    budget=10)
check.equal(asked, [],
            "a book every catalogue has already answered for costs nothing")


# --- which catalogues are available ------------------------------------------

check.equal(external_books.configured_sources(), ("openlibrary",),
            "open library needs no credential, so it is always available")

config.__dict__.pop("GOOGLE_BOOKS_API_KEY", None)
import os  # noqa: E402

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
