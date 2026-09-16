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


harness.cleanup()
raise SystemExit(check.report())
