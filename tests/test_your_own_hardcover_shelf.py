"""A listener's own Hardcover shelf, which is theirs and nobody else's.

The community rating in `external_books` answers "what do strangers think of
this book" -- one number, the same for everybody, belonging to the household.
This is the other kind: what *this person* has read, scored and said they want.
A Hardcover token names an account (`me` answers with it), so it is the one
credential that can answer it, and using somebody else's would not be a
degraded answer but another person's reading history presented as yours.
"""
import harness

harness.setup()

from app import external_books, store  # noqa: E402
from app.books import engine, hardcover_shelf  # noqa: E402

check = harness.Check("your own hardcover shelf")
store.init()

ALEX = "alex-jellyfin-id"
SAM = "sam-jellyfin-id"


# --- the token belongs to one account ----------------------------------------

check.equal(store.user_setting(ALEX, "HARDCOVER_TOKEN"), None,
            "an account that has set nothing has nothing")

store.put_user_setting(ALEX, "HARDCOVER_TOKEN", "hc_pat_alex")
check.equal(store.user_setting(ALEX, "HARDCOVER_TOKEN"), "hc_pat_alex",
            "a token is kept for the account that saved it")
check.equal(store.user_setting(SAM, "HARDCOVER_TOKEN"), None,
            "and is NOT visible to another account, which is the whole point")

check.equal(store.accounts_with_setting("HARDCOVER_TOKEN"), 1,
            "the doctor can count them without reading one")

store.put_user_setting(ALEX, "HARDCOVER_TOKEN", "")
check.equal(store.user_setting(ALEX, "HARDCOVER_TOKEN"), None,
            "clearing deletes the row rather than storing an empty string, so "
            "'not set' stays distinguishable from 'set to nothing'")
check.equal(store.accounts_with_setting("HARDCOVER_TOKEN"), 0,
            "and the count follows")

check.raises(ValueError,
             lambda: store.put_user_setting(ALEX, "DB_PATH", "/etc/passwd"),
             "only allowlisted names may be written per account")


# --- no token is the shipped state, and costs nothing ------------------------

check.equal(hardcover_shelf.for_user(SAM), None,
            "an account with no token has no shelf, and nothing is asked of "
            "Hardcover to find that out")
check.equal(hardcover_shelf.rated(None), [],
            "and every reader of it copes with no shelf")
check.equal(hardcover_shelf.finished(None), [],
            "including the exclusion list, which absent must not empty")
check.equal(hardcover_shelf.wanted(None), [],
            "and the want list")


# --- reading the shelf -------------------------------------------------------
#
# Status ids are Hardcover's own, read off `user_book_statuses` live rather
# than guessed: 1 Want to Read, 2 Currently Reading, 3 Read, 4 Paused, 5 Did
# Not Finish, 6 Ignored. `book_statuses` is a DIFFERENT enum -- moderation
# state, OK / To Review / Deleted / Deduped -- and reads plausibly enough to be
# picked by mistake.

SHELF = {
    "username": "alex",
    "books_count": 5,
    "books": [
        {"title": "The Way of Kings", "authors": ["Brandon Sanderson"],
         "rating": 5.0, "status_id": hardcover_shelf.READ},
        {"title": "Skyward", "authors": ["Brandon Sanderson"],
         "rating": None, "status_id": hardcover_shelf.WANT_TO_READ},
        {"title": "Elantris", "authors": ["Brandon Sanderson"],
         "rating": 2.0, "status_id": hardcover_shelf.DID_NOT_FINISH},
        {"title": "Warbreaker", "authors": ["Brandon Sanderson"],
         "rating": None, "status_id": hardcover_shelf.CURRENTLY_READING},
        {"title": "Mistborn", "authors": ["Brandon Sanderson"],
         "rating": 4.0, "status_id": hardcover_shelf.IGNORED},
    ],
}

check.equal(sorted(b["title"] for b in hardcover_shelf.rated(SHELF)),
            ["Elantris", "Mistborn", "The Way of Kings"],
            "rated is every book they scored, whatever they did with it after")

check.equal(sorted(b["title"] for b in hardcover_shelf.finished(SHELF)),
            ["Elantris", "Mistborn", "The Way of Kings"],
            "finished counts abandoned and ignored, not only read: somebody "
            "who gave up on a book has decided about it more firmly than "
            "somebody who finished one")

check.equal([b["title"] for b in hardcover_shelf.wanted(SHELF)], ["Skyward"],
            "want-to-read does not include what they are reading now, which "
            "nobody needs offered as something to start")


# --- matching a shelf entry to a library book --------------------------------

read_index = engine._shelf_index(hardcover_shelf.finished(SHELF))
want_index = engine._shelf_index(hardcover_shelf.wanted(SHELF))

check.that(
    engine.shelf_entry(read_index, "The Way of Kings", ["Brandon Sanderson"]),
    "a book they finished is recognised in the library")
check.that(
    not engine.shelf_entry(read_index, "Skyward", ["Brandon Sanderson"]),
    "a book they only want is not one they finished")
check.that(
    not engine.shelf_entry(read_index, "The Way of Kings", ["Karen Traviss"]),
    "and the same title by somebody else is not their read")

# The part trap again, from the other direction. Their shelf holds the whole
# book; the library holds a part of it. Different main titles, so the part is
# not claimed as read -- which matters, because marking it read would hide it.
check.that(
    not engine.shelf_entry(
        read_index, "The Way of Kings, Part 1", ["Brandon Sanderson"]),
    "finishing the book does not mark a part of it read")

check.that(
    engine.shelf_entry(
        read_index, "The Way of Kings: Book One of the Stormlight Archive",
        ["Brandon Sanderson"]),
    "but an edition with a subtitle is the same book")

check.that(engine.shelf_entry(want_index, "Skyward", ["Brandon Sanderson"]),
           "a want-to-read entry is found the same way")
check.equal(engine._shelf_index([]), {},
            "an empty shelf indexes to nothing rather than to a trap")

# The two matchers must file a book identically or one will claim what the
# other rejects.
check.equal(external_books.main_title("The Way of Kings: Book One"),
            external_books.main_title("The Way of Kings"),
            "both matchers strip a subtitle the same way")


# --- being told beats anything inferred --------------------------------------

check.that(engine.W_WANT_TO_READ > engine.W_SIMS_VOTE * engine.MAX_SIMILARITY_WEIGHT,
           "a book they asked for outranks anything Audible inferred")
check.that(engine.W_WANT_TO_READ > engine.W_AUTHOR * engine.MAX_AFFINITY_WEIGHT,
           "and anything an author overlap inferred")
check.that(engine.W_WANT_TO_READ < engine.W_SERIES_NEXT,
           "but not the next book in a series they are partway through, which "
           "they want whether or not they ever wrote it down")


# --- a refusal is not an empty shelf -----------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")
        return self

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, response): self.response = response
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, headers=None, json=None): return self.response


saved_client = hardcover_shelf.httpx.Client

hardcover_shelf.httpx.Client = lambda **kw: FakeClient(
    FakeResponse({"error": "invalid_token"}, status=401))
check.raises(hardcover_shelf.Refused,
             lambda: hardcover_shelf.fetch("hc_pat_wrong"),
             "a rejected token is a refusal, not an outage: one is something "
             "the person can fix and the other is not")

hardcover_shelf.httpx.Client = lambda **kw: FakeClient(
    FakeResponse({"error": "ilike and related operations are not permitted"}))
check.raises(hardcover_shelf.Refused,
             lambda: hardcover_shelf.fetch("hc_pat_alex"),
             "and so is a bare error with no errors array, which would "
             "otherwise read as a reader who has shelved nothing")

hardcover_shelf.httpx.Client = lambda **kw: FakeClient(FakeResponse(
    {"data": {"me": [{"username": "alex", "books_count": 2, "user_books": [
        {"rating": 5.0, "status_id": 3,
         "book": {"title": "The Way of Kings",
                  "contributions": [{"author": {"name": "Brandon Sanderson"}}]}},
        {"rating": None, "status_id": 1,
         "book": {"title": "", "contributions": []}},
    ]}]}}))
found = hardcover_shelf.fetch("hc_pat_alex")
check.equal(found["username"], "alex", "a good answer names the account")
check.equal(len(found["books"]), 1,
            "and drops an entry with no title, which nothing could match")
check.equal(found["books"][0]["authors"], ["Brandon Sanderson"],
            "authors are flattened out of contributions")

# An account that exists and has shelved nothing. This is Matt's on the day it
# was connected, and it must be indistinguishable from having no token at all
# as far as the shelf is concerned.
hardcover_shelf.httpx.Client = lambda **kw: FakeClient(FakeResponse(
    {"data": {"me": [{"username": "matt1211", "books_count": 0,
                      "user_books": []}]}}))
empty = hardcover_shelf.fetch("hc_pat_matt")
check.equal(empty["books"], [], "an empty account reads as an empty shelf")
check.equal(engine._shelf_index(hardcover_shelf.rated(empty)), {},
            "which contributes nothing, rather than excluding everything")

hardcover_shelf.httpx.Client = saved_client


# --- the cache is per account ------------------------------------------------

store.put_external(hardcover_shelf._cache_key(ALEX), SHELF)
store.put_external(hardcover_shelf._cache_key(SAM), {"username": "sam",
                                                     "books_count": 0,
                                                     "books": []})
store.put_user_setting(ALEX, "HARDCOVER_TOKEN", "hc_pat_alex")
store.put_user_setting(SAM, "HARDCOVER_TOKEN", "hc_pat_sam")

check.equal((hardcover_shelf.for_user(ALEX) or {}).get("username"), "alex",
            "each account reads its own cached shelf")
check.equal((hardcover_shelf.for_user(SAM) or {}).get("username"), "sam",
            "and not another's")

hardcover_shelf.forget(ALEX)
check.equal(store.get_external(
    hardcover_shelf._cache_key(ALEX), hardcover_shelf.TTL_HOURS), None,
    "forgetting drops the row outright, so a newly saved token takes effect "
    "at once rather than in six hours")
check.equal((hardcover_shelf.for_user(SAM) or {}).get("username"), "sam",
            "and leaves everybody else's alone")


# --- end to end: what a connected shelf does to a built shelf ----------------
#
# Stubbed at `for_user`, which is the seam `engine.run` actually calls, so
# everything between it and the finished shelves is the shipped code.

from app import config, jellyfin, listenarr  # noqa: E402

READER = jellyfin.User(id="reader", name="Reader")

LIBRARY = [
    # Played here, so it is a seed whatever Hardcover says.
    {"Id": "played", "Name": "A Book Heard Here", "Type": "AudioBook",
     "Genres": ["Fantasy"], "Overview": "wizards and a long walk",
     "ProviderIds": {}, "UserData": {"PlaybackPositionTicks": 1, "Played": True},
     "RunTimeTicks": 10, "People": []},
    # Never touched here, finished on Hardcover. Must not be suggested.
    {"Id": "read-in-print", "Name": "Elantris", "Type": "AudioBook",
     "Genres": ["Fantasy"], "Overview": "wizards and a long walk",
     "ProviderIds": {}, "UserData": {}, "RunTimeTicks": 10,
     "People": [{"Name": "Brandon Sanderson", "Type": "Author"}]},
    # Never touched here, rated 5 on Hardcover. Must become a seed.
    {"Id": "rated-in-print", "Name": "Mistborn", "Type": "AudioBook",
     "Genres": ["Fantasy"], "Overview": "wizards and a long walk",
     "ProviderIds": {}, "UserData": {}, "RunTimeTicks": 10,
     "People": [{"Name": "Brandon Sanderson", "Type": "Author"}]},
]

# A candidate with no Audible votes behind it and nothing in common with
# anything: without the want-to-read shelf it has no way onto the discover
# list at all. It is also book three of a series the reader has not started,
# so it is behind the reading-order guard as well.
WANTED_CANDIDATE = {
    "asin": "WANTED", "title": "Skyward", "authors": ["Brandon Sanderson"],
    "narrators": [], "series": "Skyward", "series_position": "3",
    "description": "nothing whatever to do with the library",
}

MY_SHELF = {
    "username": "reader", "books_count": 3,
    "books": [
        {"title": "Elantris", "authors": ["Brandon Sanderson"],
         "rating": None, "status_id": hardcover_shelf.READ},
        {"title": "Mistborn", "authors": ["Brandon Sanderson"],
         "rating": 5.0, "status_id": hardcover_shelf.WANT_TO_READ},
        {"title": "Skyward", "authors": ["Brandon Sanderson"],
         "rating": None, "status_id": hardcover_shelf.WANT_TO_READ},
    ],
}


def build(shelf):
    saved = {
        "books": jellyfin.books,
        "sims": engine._seed_sims,
        "queued": listenarr.queued_asins,
        "playlist": jellyfin.set_playlist,
        "for_user": hardcover_shelf.for_user,
        "keywords": engine._keyword_candidates,
        "max_shelf": config.MAX_SHELF,
    }
    try:
        jellyfin.books = lambda uid: [dict(i) for i in LIBRARY]
        engine._seed_sims = lambda seed: [WANTED_CANDIDATE]
        engine._keyword_candidates = lambda queries, owned: {}
        listenarr.queued_asins = lambda: set()
        jellyfin.set_playlist = lambda uid, name, ids: "playlist"
        hardcover_shelf.for_user = lambda key: shelf
        config.MAX_SHELF = 10
        return engine.run(READER, update_playlist=False)
    finally:
        jellyfin.books = saved["books"]
        engine._seed_sims = saved["sims"]
        engine._keyword_candidates = saved["keywords"]
        listenarr.queued_asins = saved["queued"]
        jellyfin.set_playlist = saved["playlist"]
        hardcover_shelf.for_user = saved["for_user"]
        config.MAX_SHELF = saved["max_shelf"]


harness.no_book_ratings()
without = build(None)
withshelf = build(MY_SHELF)

owned_without = [r["title"] for r in without["own"]]
owned_with = [r["title"] for r in withshelf["own"]]

check.that("Elantris" in owned_without,
           "with no shelf connected, a book nobody here has played is an "
           "ordinary suggestion")
check.that("Elantris" not in owned_with,
           "connected, a book finished in print stops being suggested -- and "
           "nothing in Jellyfin could have known that")

check.that("Mistborn" not in owned_with,
           "a book rated on Hardcover is not offered back either: an opinion "
           "already given is not a suggestion")

discover_with = [r["title"] for r in withshelf["discover"]]
check.that("Skyward" in discover_with,
           "a want-to-read book reaches the discover shelf even as book three "
           "of a series never started, because they asked for it")
wanted_row = next(r for r in withshelf["discover"] if r["title"] == "Skyward")
check.that(wanted_row["why"] and "want-to-read" in wanted_row["why"][0],
           "and says so first, ahead of anything inferred")

check.that("Skyward" not in [r["title"] for r in without["discover"]],
           "without the shelf it is behind the reading-order guard, which is "
           "what makes the case above a real one")

# The rating has to reach the machinery, not just the library. An appended
# seed carried weight zero and did nothing at all; this is the assertion that
# would have caught it.
check.that(withshelf["seeds"] > without["seeds"],
           "a rating given on Hardcover makes that book a seed here")
check.that(withshelf["ratings"] > without["ratings"],
           "and counts toward the ratings ramp, which is what makes it a "
           "rating rather than a book with a flag on it")

# Matt's account on the day it was connected.
empty = build({"username": "matt1211", "books_count": 0, "books": []})
check.equal([r["title"] for r in empty["own"]], owned_without,
            "a connected account with nothing on it builds exactly the shelf "
            "it built before, rather than excluding everything")
check.equal((empty["seeds"], empty["ratings"]),
            (without["seeds"], without["ratings"]),
            "and changes no weights")


# --- a want-to-read shelf that produces candidates, not just re-ranking ------
#
# The defect this closes: `wanted` used to lift a book Audible's similarity
# graph had already found, so a book somebody shelved that no finished book
# resembles never appeared at all. Everything else on the discover shelf is
# inferred; this is the one channel where they simply said what they want.

SEARCHED = []


def fake_audible_search(query):
    SEARCHED.append(query)
    # Keyed case-insensitively: the surname reaches the query through the same
    # normalising the matcher uses, so it arrives lower case.
    query = query.casefold()
    rows = {
        "soul fraud givler": [
            {"asin": "SOULFRAUD", "title": "Soul Fraud",
             "authors": [{"name": "Andrew Givler"}], "narrators": [],
             "lengthMinutes": 600},
        ],
        # The trap, and the reason the strict matcher is used here rather than
        # the loose one: a search for a book returns a part of it, and asking
        # for the wrong thing here is a download, not a bad row.
        "the way of kings sanderson": [
            {"asin": "WRONGPART", "title": "The Way of Kings, Part 1",
             "authors": [{"name": "Brandon Sanderson"}], "narrators": [],
             "lengthMinutes": 600},
        ],
        # A same-title book by somebody else.
        "ascension rinoz": [
            {"asin": "WRONGAUTHOR", "title": "Ascension",
             "authors": [{"name": "Someone Else"}], "narrators": [],
             "lengthMinutes": 600},
        ],
    }
    return rows.get(query, [])


def want(title, authors):
    return {"title": title, "authors": authors, "rating": None,
            "status_id": hardcover_shelf.WANT_TO_READ}


saved_search = listenarr.audible_search
saved_desc = engine._candidate_description
listenarr.audible_search = fake_audible_search
engine._candidate_description = lambda asin: "a blurb"
try:
    found = engine._want_candidates(
        [want("Soul Fraud", ["Andrew Givler"])],
        owned_check=lambda cand: False, suppressed=set())
    check.equal(list(found), ["SOULFRAUD"],
                "a book on the want-to-read shelf becomes something that can "
                "be acquired, with no similarity link to anything")
    check.equal(found["SOULFRAUD"]["source"], "hardcover_want",
                "and says where it came from")

    check.equal(
        engine._want_candidates(
            [want("The Way of Kings", ["Brandon Sanderson"])],
            owned_check=lambda cand: False, suppressed=set()),
        {},
        "a search returning a PART of the wanted book resolves to nothing: a "
        "wrong row here is a download of the wrong book")

    check.equal(
        engine._want_candidates(
            [want("Ascension", ["RinoZ"])],
            owned_check=lambda cand: False, suppressed=set()),
        {},
        "and so is a same-title book by somebody else")

    check.equal(
        engine._want_candidates(
            [want("Soul Fraud", ["Andrew Givler"])],
            owned_check=lambda cand: True, suppressed=set()),
        {},
        "a wanted book the library already holds is not offered as something "
        "to acquire -- it belongs on the owned shelf")

    SEARCHED.clear()
    check.equal(
        engine._want_candidates(
            [want("Soul Fraud", ["Andrew Givler"])],
            owned_check=lambda cand: False, suppressed={"SOULFRAUD"}),
        {},
        "and one already on order is dropped")

    SEARCHED.clear()
    check.equal(engine._want_candidates([], lambda c: False, set()), {},
                "an empty want list resolves nothing")
    check.equal(SEARCHED, [],
                "without asking Audible anything at all, which is what an "
                "account that has connected nothing must cost")
finally:
    listenarr.audible_search = saved_search
    engine._candidate_description = saved_desc


harness.cleanup()
raise SystemExit(check.report())
