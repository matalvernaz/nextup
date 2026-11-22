"""Book shelves follow what an account said about its suggestions, wherever the book is now."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness  # noqa: E402

DB_PATH = harness.use("taste-books")
harness.discard(DB_PATH)

from app import compat_nextread, config, jellyfin, listenarr, taste  # noqa: E402
from app.books import engine, hardcover_shelf, shelves, store  # noqa: E402

harness.no_book_ratings()
store.init()
check = harness.Check("marks on book shelves")
USER = jellyfin.User(id="reader-id", name="Reader")
taste._start = lambda target, name: target()


def book(iid, title, author, *, played=False, asin=None, series=None, position=None,
         overview="kindly heroes build a magical home together"):
    row = {"Id": iid, "Name": title, "Overview": overview, "Genres": ["Fantasy"],
           "People": [{"Type": "Author", "Name": author}],
           "UserData": {"Played": played}, "ProviderIds": {}}
    if asin:
        row["ProviderIds"]["Audible"] = asin
    if series:
        row["SeriesName"] = series
        row["IndexNumber"] = position
    return row


def suggestion(asin, title, author, *, series=None, position=None,
               description="kindly heroes build a magical home together"):
    return {"asin": asin, "title": title, "authors": [author], "narrators": [],
            "series": series, "series_position": position, "description": description,
            "image": None}


LIBRARY = [
    book("played", "Played Book", "A. Writer", played=True, asin="PLAYED"),
    book("same-author", "Unread Same Author", "A. Writer"),
    book("quest-two", "Quest Two", "Q. Author", series="Quest", position=2),
    book("arrived", "Arrived Book", "A. Writer", asin="ARRIVED"),
]
NEIGHBOURS = {
    "PLAYED": [
        suggestion("D-ONE", "Discover One", "A. Writer"),
        suggestion("D-TWO", "Discover Two", "C. New",
                   description="clockwork airships duel over a drowned city"),
        suggestion("TWIN-US", "Twin Title", "T. Twin"),
        suggestion("TWIN-CA", "Twin Title: A Novel", "T. Twin"),
        suggestion("QUEST-1", "Quest One", "Q. Author", series="Quest", position="1"),
    ],
    # What Audible lists beside the book only asked about.
    "D-TWO": [suggestion("D-SIX", "Another Airship", "C. New",
                         description="clockwork airships race a storm front")],
}
during_build: list = []


def neighbours(seed, **kwargs):
    for act in during_build:
        act()
    during_build.clear()
    return NEIGHBOURS.get(engine._asin(seed) or "", [])


playlists: list[list[str]] = []
jellyfin.books = lambda uid: [dict(row) for row in LIBRARY]
engine._seed_sims = neighbours
listenarr.queued_asins = lambda: set()
jellyfin.set_playlist = lambda uid, name, ids: playlists.append(list(ids)) or "playlist"
config.MAX_SHELF = 20


def own_ids(data):
    return [row["id"] for row in data["own"]]


def asins(data):
    return [row["asin"] for row in data["discover"]]


def mark(key, surface, opinion="", had=False):
    """A mark as the route takes it, details and all."""
    from app import feedback
    feedback.take(USER, "book", key, surface, opinion, had)


# --- no marks: the shelves there always were --------------------------------------
plain = engine.run(USER, update_playlist=False)
again = engine.run(USER, update_playlist=False)
check.equal((own_ids(again), asins(again)), (own_ids(plain), asins(plain)),
            "with no marks the shelves are what they always were")
check.that("same-author" in own_ids(plain), "the unread book by a known author is offered")
check.that("D-ONE" in asins(plain) and "D-TWO" in asins(plain),
           "and so are both neighbours")
check.that("quest-two" not in own_ids(plain),
           "book two waits while book one has not been read")
check.equal(plain["marks_revision"], 0, "a shelf says which marks it was built from")
check.equal(next(row for row in plain["own"] if row["id"] == "arrived").get("asin"),
            "ARRIVED", "an owned row keeps its ASIN, for matching marks exactly later")

# The shelf the marks below are made from, as the app saw it.
shelves.invalidate(USER.key)
shelves.result(USER, force=True, update_playlist=False)


# --- had and less -----------------------------------------------------------------
mark("same-author", taste.OWNED, had=True)
mark("D-ONE", taste.CATALOGUE, taste.LESS)
mark("TWIN-US", taste.CATALOGUE, taste.LESS)
marked = engine.run(USER, update_playlist=False)
check.that("same-author" not in own_ids(marked), "a book already read is not offered")
check.that("D-ONE" not in asins(marked), "less like this takes the suggestion away")
check.that("TWIN-US" not in asins(marked) and "TWIN-CA" not in asins(marked),
           "and the other marketplace's edition of it as well")


# --- already read, on the suggestions list, lets book two through -----------------------
mark("QUEST-1", taste.CATALOGUE, had=True)
quested = engine.run(USER, update_playlist=False)
check.that("quest-two" in own_ids(quested),
           "book one read elsewhere makes the library's book two next")
quest = next(row for row in quested["own"] if row["id"] == "quest-two")
check.that(quest["why"][0].startswith("next in Quest"), "and says it is next")
check.that("QUEST-1" not in asins(quested), "book one itself is not offered")


# A different book with the same title and no author arrives. It is not the
# book marked read, so the mark still counts for reading order.
LIBRARY.append(book("impostor", "Quest One", ""))
impostor = engine.run(USER, update_playlist=False)
check.that("quest-two" in own_ids(impostor),
           "a same-title book with no author is not taken for the one read elsewhere")
LIBRARY.pop()


# --- more like this on a book the library does not hold -----------------------------
mark("D-TWO", taste.CATALOGUE, taste.MORE)
more = engine.run(USER, update_playlist=False)
check.that("D-TWO" in asins(more), "the book asked about stays on the list")
check.that("D-SIX" in asins(more), "and a book Audible lists beside it comes in")
d_six = next(row for row in more["discover"] if row["asin"] == "D-SIX")
check.that(any("C. New, like a book you asked for more of" in reason
               for reason in d_six["why"]),
           "an author liked only through more like this is said to be that")
check.that(not any("C. New, who you've listened to" in reason for reason in d_six["why"]),
           "and never someone listened to")
d_two = next(row for row in more["discover"] if row["asin"] == "D-TWO")
check.that(not any("C. New" in reason for reason in d_two["why"]),
           "the asked-about book is not recommended by its own author")
shown = shelves.engine.present(USER.key, more)
check.equal(next(row for row in shown["discover"] if row["asin"] == "D-TWO").get("opinion"),
            "more", "and says it was asked about")


mark("D-SIX", taste.CATALOGUE, taste.MORE)
both = engine.run(USER, update_playlist=False)
six = next(row for row in both["discover"] if row["asin"] == "D-SIX")
check.that(not any("Discover Two" in reason or "Audible" in reason for reason in six["why"]),
           "a book only asked about is not a reason for another one asked about")
check.equal(six["because_of"], [], "nor counted as its neighbour")

# Neither of them is let fall off the list, however full it is: each is where
# its own "Undo more like this" is.
mark("arrived", taste.OWNED, taste.MORE)
config.MAX_SHELF = 1
full = engine.run(USER, update_playlist=False)
check.that({"D-TWO", "D-SIX"} <= set(asins(full)),
           "books asked for more of stay on a full list")
check.that("arrived" in own_ids(full) and len(own_ids(full)) == 2,
           "and so does a library book asked for more of, past the cut")
config.MAX_SHELF = 20
# And when nothing brings one back at all, it is kept from what it said.
NEIGHBOURS["PLAYED"] = [row for row in NEIGHBOURS["PLAYED"] if row["asin"] != "D-TWO"]
gone = engine.run(USER, update_playlist=False)
kept = next((row for row in gone["discover"] if row["asin"] == "D-TWO"), None)
check.that(kept is not None, "a book asked for more of that no neighbour brings back stays")
check.equal(kept and kept["why"], ["you asked for more like this"], "and says why")
# Kept only while it would be offered at all: finished on Hardcover since, it
# goes, like any other book read elsewhere.
real_for_user, real_finished = hardcover_shelf.for_user, hardcover_shelf.finished
hardcover_shelf.for_user = lambda key: {"books": []}
hardcover_shelf.finished = lambda shelf: [{"title": "Discover Two", "authors": ["C. New"]}]
read_since = engine.run(USER, update_playlist=False)
hardcover_shelf.for_user, hardcover_shelf.finished = real_for_user, real_finished
check.that("D-TWO" not in asins(read_since),
           "a book asked for more of and since read elsewhere is not kept")
# Nor out of reading order: book three of a series not yet started.
NEIGHBOURS["PLAYED"].append(
    suggestion("SAGA-3", "Saga Three", "S. Teller", series="Saga X", position="3"))
store.put_shelf(USER.key, {"own": [], "discover": [
    {"asin": "SAGA-3", "title": "Saga Three", "authors": ["S. Teller"], "narrators": [],
     "series": "Saga X", "series_position": "3", "description": "a quest"}]})
with shelves._cache_guard:
    shelves._cache.pop(USER.key, None)
mark("SAGA-3", taste.CATALOGUE, taste.MORE)
NEIGHBOURS["PLAYED"].pop()
ordered = engine.run(USER, update_playlist=False)
check.that("SAGA-3" not in asins(ordered),
           "nor a book asked for more of that is out of reading order")


# --- a mark follows a book into the library ---------------------------------------
# More like this on a suggestion that then arrives, taken back from the library
# book's row: the newest thing said wins.
store.put_shelf(USER.key, {"own": [], "discover": [
    {"asin": "FOLLOW", "title": "Follow Me", "authors": ["F. Writer"], "narrators": [],
     "description": "kindly heroes build a magical home together"}]})
with shelves._cache_guard:
    shelves._cache.pop(USER.key, None)
mark("FOLLOW", taste.CATALOGUE, taste.MORE)
LIBRARY.append(book("follow-lib", "Follow Me", "F. Writer", asin="FOLLOW"))
followed = engine.present(USER.key, engine.run(USER, update_playlist=False))
check.equal(next(row for row in followed["own"] if row["id"] == "follow-lib").get("opinion"),
            "more", "the library book says more like this, said of it as a suggestion")
taste.record(USER.key, "book", "follow-lib", taste.OWNED, "", False)
unfollowed = engine.present(USER.key, engine.run(USER, update_playlist=False))
check.equal(next(row for row in unfollowed["own"] if row["id"] == "follow-lib").get("opinion"),
            None, "and Undo more like this on its row takes it back")
LIBRARY.pop()

check.that("arrived" in own_ids(more), "the arrived book is offered")
mark("ARRIVED", taste.CATALOGUE, had=True)
arrived = engine.run(USER, update_playlist=False)
check.that("arrived" not in own_ids(arrived),
           "already read, said of the suggestion, holds once it is in the library")


# --- every read and every playlist write go by the marks as they stand ----------------
shelves.invalidate(USER.key)
shelves.result(USER, force=True, update_playlist=False)
LIBRARY.append(book("late", "Late Unread", "A. Writer"))
shelves.invalidate(USER.key)
built = shelves.result(USER, force=True, update_playlist=False)
check.that("late" in own_ids(built), "a new unread book is offered")
taste.record(USER.key, "book", "late", taste.OWNED, "", True)
playlists.clear()
served = shelves.result(USER)
check.that("late" not in own_ids(served),
           "a shelf built before already read never offers that book again")
shelves.result(USER)
check.that(playlists and all("late" not in ids for ids in playlists),
           "nor does any playlist write publish it")

# A build that read the marks before one was made, and writes the playlist after.
LIBRARY.append(book("racing", "Racing Unread", "A. Writer"))
playlists.clear()
during_build.append(lambda: taste.record(USER.key, "book", "racing", taste.OWNED, "", True))
raced = engine.run(USER, update_playlist=True)
check.that("racing" in own_ids(raced), "the build itself never saw the mark")
check.that(playlists and "racing" not in playlists[-1],
           "but its playlist write did")
check.that(raced["marks_revision"] < taste.revision(USER.key, "book"),
           "and the shelf reads as older than the marks")


# A write blocked on its way while a mark is made: it settles nothing, and the
# next read writes the playlist again without the book.
import threading  # noqa: E402

LIBRARY.append(book("blocked", "Blocked Unread", "A. Writer"))
shelves.invalidate(USER.key)
shelves.result(USER, force=True, update_playlist=False)
release = threading.Event()
inside = threading.Event()
real_write = jellyfin.set_playlist


def slow_write(uid, name, ids):
    inside.set()
    release.wait(5)
    playlists.append(list(ids))
    return "playlist"


jellyfin.set_playlist = slow_write
playlists.clear()
reader = threading.Thread(target=lambda: shelves.result(USER), daemon=True)
reader.start()
inside.wait(5)
taste.record(USER.key, "book", "blocked", taste.OWNED, "", True)
release.set()
reader.join(5)
jellyfin.set_playlist = real_write
with shelves._cache_guard:
    owed = not shelves._cache[USER.key][2]
check.that(owed, "a mark made while the playlist was being written leaves it owed")
# Here the rebuild the mark asks for runs in place, inside the first read, so
# that read's shelf is replaced under it and the write falls to the next one.
shelves.result(USER)
shelves.result(USER)
check.that(len(playlists) >= 2 and "blocked" not in playlists[-1],
           "and a later read writes it again without the book")


# --- exact ids first, and a title only with an author in common --------------------
def find(marks, **row):
    return engine.BookMarks(marks).for_candidate(row)


def owned(key, title="", authors=(), asin="", opinion="", had=True, revision=1):
    return taste.Mark("book", key, taste.OWNED, opinion, had, title=title,
                      details={"title": title, "authors": list(authors), "asin": asin},
                      revision=revision)


dune = owned("lib-dune", "Dune", ["Frank Herbert"], asin="B-DUNE")
check.that(find([dune], asin="X", title="Dune", authors=["Frank Herbert"]) is dune,
           "another edition by the same author is the same book")
check.equal(find([dune], asin="X", title="Dune", authors=["Someone Else"]), None,
            "a book of the same title by somebody else is not")
check.equal(find([dune], asin="X", title="Dune", authors=["Brian Herbert"]), None,
            "nor is one by somebody who only shares a surname")
check.equal(find([dune], asin="X", title="Dune", authors=[]), None,
            "nor one with no author to compare")
untitled = owned("lib-untitled", "Dune", [])
check.equal(find([untitled], asin="X", title="Dune", authors=["Frank Herbert"]), None,
            "a mark with no author recorded matches by its ids alone")
check.that(find([dune], asin="B-DUNE", title="Retitled", authors=[]) is dune,
           "the library book's own ASIN finds it under any title")
exact = taste.Mark("book", "B-ONE", taste.CATALOGUE, taste.LESS, False, title="One",
                   details={"title": "One", "authors": ["A. Writer"]}, revision=1)
fuzzy = owned("lib-one", "One", ["A. Writer"], opinion=taste.MORE, had=False, revision=9)
check.that(find([exact, fuzzy], asin="B-ONE", title="One", authors=["A. Writer"]) is exact,
           "an exact id wins over a newer match by title")
row_mark = taste.Mark("book", "B-SHARED", taste.CATALOGUE, "", True, title="Old Name",
                      details={"title": "Old Name", "authors": ["Q. Q"]}, revision=1)
check.that(engine.BookMarks([row_mark]).for_own_row(
    {"id": "lib-x", "asin": "B-SHARED", "title": "New Name", "authors": []}) is row_mark,
    "an owned row is found by its ASIN when Jellyfin calls it something else")


# A deferred write of an older shelf that lands after a newer shelf is built
# but before it is cached: it must never be what the playlist ends on.
shelves.invalidate(USER.key)
shelves.result(USER, force=True, update_playlist=False)
with shelves._cache_guard:
    older_shelf = shelves._cache[USER.key][1]
LIBRARY.append(book("newest", "Newest Unread", "A. Writer"))
real_publish_owned = shelves._publish_owned
landed: list[bool] = []


def write_older_in_the_gap(*args, **kwargs):
    if not landed:
        landed.append(True)
        shelves.write_playlist(USER, older_shelf)
    return real_publish_owned(*args, **kwargs)


shelves._publish_owned = write_older_in_the_gap
playlists.clear()
newest = shelves.result(USER, force=True)
shelves._publish_owned = real_publish_owned
check.that(landed, "the older write did land in the gap")
check.that(playlists and "newest" in playlists[-1],
           "the playlist ends on the newer shelf, not the older one")
with shelves._cache_guard:
    check.equal(shelves._cache[USER.key][2], True, "and the newer shelf is published")


# --- reuse only what was built from these marks -------------------------------------
with shelves._cache_guard:
    shelves._cache[USER.key] = (time.monotonic(), {"marks_revision": 3, "own": []}, False)
check.equal(shelves._entry_computed_since(USER.key, 0.0, 4), None,
            "a rebuild that read older marks is not reused for a newer one")
check.that(shelves._entry_computed_since(USER.key, 0.0, 3) is not None,
           "one built from them is")

# An older playlist write finishing after a newer shelf is cached.
older = {"own": [], "marks_revision": 1}
newer = {"own": [], "marks_revision": 2}
with shelves._cache_guard:
    shelves._cache[USER.key] = (time.monotonic(), newer, False)
written_before = len(playlists)
shelves.write_playlist(USER, older)
check.equal(len(playlists), written_before,
            "an older shelf is never written over a newer one")
with shelves._cache_guard:
    check.equal(shelves._cache[USER.key][2], False,
                "an older write never marks a newer shelf as published")


# --- the book screen's route ------------------------------------------------------
shelves.invalidate(USER.key)
shelves.result(USER, force=True, update_playlist=False)
answer = compat_nextread.get_shelves(force=False, held_asks=False, user=USER)
suggestions = {row["asin"]: row for row in answer["suggestions"]}
check.equal(suggestions.get("D-TWO", {}).get("opinion"), "more",
            "a suggestion asked about says so on the book screen's route")
check.that("D-ONE" not in suggestions and "QUEST-1" not in suggestions,
           "and nothing marked less or read is on it")
# FOLLOW is back as a suggestion now its library copy is gone, and the mark
# made on the suggestion is again the only thing said about it.
check.that(all("opinion" not in row for asin, row in suggestions.items()
               if asin not in ("D-TWO", "D-SIX", "FOLLOW")),
           "rows with no mark carry no opinion, as before")
owned = {row["id"]: row for row in answer["owned"]}
check.that("same-author" not in owned and "arrived" not in owned,
           "nor on its owned list")

harness.discard(DB_PATH)
raise SystemExit(check.report())
