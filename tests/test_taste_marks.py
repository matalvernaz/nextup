"""Marks on suggestions: kept, undone, refused when they could never match, and served by both routes."""
import harness

harness.setup()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, compat_nextread, jellyfin, podcasts,  # noqa: E402
                 recommendations, store, taste)
from app.books import shelves  # noqa: E402

check = harness.Check("taste marks")
store.init()

USER = jellyfin.User("listener-id", "Listener")
OTHER = jellyfin.User("other-id", "Other")

# The rebuilds a mark starts are about other tests; here they are only counted.
rebuilds: list[tuple[str, str]] = []
recommendations.rebuild_after_mark = (
    lambda user, medium: rebuilds.append(("shelf", medium)))
podcasts.rebuild_catalogue_after_mark = (
    lambda user: rebuilds.append(("catalogue", "podcast")))
shelves.rebuild_after_mark = lambda user: rebuilds.append(("books", "book"))


# --- the ledger -----------------------------------------------------------------
previous, current = taste.record(USER.key, "movie", "film-1", taste.OWNED,
                                 taste.MORE, False, "First Film")
check.equal(previous, None, "a first mark has nothing before it")
check.equal(current.state(), {"opinion": "more", "had": False}, "the mark is kept")
first_revision = current.revision

previous, current = taste.record(USER.key, "movie", "film-1", taste.OWNED,
                                 taste.NO_OPINION, True)
check.equal(previous.state(), {"opinion": "more", "had": False},
            "changing a mark answers the whole of what it was")
check.equal(current.state(), {"opinion": "", "had": True}, "the new state is kept")
check.equal(current.title, "First Film", "a mark without a title keeps the one it had")
check.that(current.revision > first_revision, "every change gets a newer revision")

previous, current = taste.record(USER.key, "movie", "film-1", taste.OWNED,
                                 taste.NO_OPINION, False)
check.equal(previous.state(), {"opinion": "", "had": True},
            "taking a mark back answers what it was, which is what Undo puts back")
check.equal([mark for mark in taste.marks(USER.key, "movie") if mark.active], [],
            "a mark taken back is no longer standing")
check.equal([mark.item_key for mark in taste.marks(USER.key, "movie")], ["film-1"],
            "but is kept, so it can override an older mark on the same item")
check.that(taste.revision(USER.key, "movie") == current.revision,
           "taking a mark back still moves the revision forward")
check.equal(taste.revision(OTHER.key, "movie"), 0,
            "one account's marks say nothing about another's")
check.equal(taste.revision(USER.key, "series"), 0,
            "a film's marks say nothing about the show shelf")

taste.record(USER.key, "movie", "film-2", taste.OWNED, taste.LESS, False)
check.equal([mark.item_key for mark in taste.marks(USER.key, "movie") if mark.active],
            ["film-2"], "standing marks are listed")
check.that("taste_marks" in store.USER_SCOPED_TABLES,
           "marks move with an account when the ledger is rekeyed")

for medium, surface, key, opinion, what in (
        ("music", taste.OWNED, "x", "", "a medium with no suggestions"),
        ("movie", "elsewhere", "x", "", "a surface that does not exist"),
        ("movie", taste.CATALOGUE, "x", "", "a film catalogue, which there is none of"),
        ("book", taste.OWNED, "x", "adore", "an opinion that is not more or less"),
        ("book", taste.OWNED, "   ", "", "an empty key"),
        ("book", taste.OWNED, "x" * 300, "", "a key no id is that long")):
    check.raises(taste.Invalid,
                 lambda m=medium, s=surface, k=key, o=opinion: taste.record(
                     USER.key, m, k, s, o, False),
                 f"refuses {what}")


# --- the weights ----------------------------------------------------------------
def mark(opinion, had):
    return taste.Mark("movie", "k", taste.OWNED, opinion, had)


check.equal(taste.weight(1.0, None), 1.0, "no mark leaves a seed alone")
check.equal(taste.weight(1.25, mark(taste.LESS, False)), taste.LESS_WEIGHT,
            "less like this outranks having watched and starred it")
check.equal(taste.weight(0.0, mark(taste.MORE, False)), taste.MORE_WEIGHT,
            "more like this counts like having watched it")
check.equal(taste.weight(1.5, mark(taste.MORE, False)), 1.5,
            "more like this never weakens stronger evidence")
check.equal(taste.weight(0.0, mark(taste.MORE, True)), taste.LIKED_WEIGHT,
            "had it and liked it counts like a favourite")
check.equal(taste.weight(0.0, mark(taste.NO_OPINION, True)), taste.HAD_WEIGHT,
            "had it, with no opinion, counts a little")
check.equal(taste.weight(1.0, mark(taste.NO_OPINION, True)), 1.0,
            "had it never weakens having watched it here")
check.that(mark(taste.LESS, False).hides and mark("", True).hides
           and not mark(taste.MORE, False).hides,
           "less and had keep a thing off the lists; more does not")


# --- the routes -------------------------------------------------------------------
app = FastAPI()
app.include_router(api.router)
app.include_router(compat_nextread.router)
app.dependency_overrides[api.caller] = lambda: USER
client = TestClient(app, raise_server_exceptions=False)

answer = client.post("/api/v1/feedback", json={
    "medium": "series", "itemKey": "show-1", "surface": "owned",
    "opinion": "more", "had": False, "title": "A Show"})
check.equal(answer.status_code, 200, "a mark is taken")
given = answer.json()
check.equal({key: value for key, value in given.items() if key != "revision"}, {
    "medium": "series", "itemKey": "show-1", "surface": "owned",
    "opinion": "more", "had": False,
    "previous": {"opinion": "", "had": False}},
    "the answer says what it is now and what it was")
check.equal(given.get("revision"), taste.revision(USER.key, "series"),
            "and the revision Undo names it by")
check.equal(rebuilds[-1], ("shelf", "series"), "a show mark rebuilds the show shelves")

undo = client.post("/api/v1/feedback", json={
    "medium": "series", "itemKey": "show-1", "surface": "owned",
    "ifRevision": given["revision"], **given["previous"]})
check.equal(undo.status_code, 200, "posting the previous state back is taken")
check.equal([mark for mark in taste.marks(USER.key, "series") if mark.active], [],
            "and that is Undo")

# An Undo for a change that has been changed again since, on another phone.
first = client.post("/api/v1/feedback", json={
    "medium": "series", "itemKey": "show-2", "surface": "owned", "opinion": "less"})
client.post("/api/v1/feedback", json={
    "medium": "series", "itemKey": "show-2", "surface": "owned", "had": True})
late_undo = client.post("/api/v1/feedback", json={
    "medium": "series", "itemKey": "show-2", "surface": "owned",
    "ifRevision": first.json()["revision"], **first.json()["previous"]})
check.equal(late_undo.status_code, 409, "an Undo of a change made over since is refused")
check.that("changed again" in late_undo.json().get("detail", ""), "and says why")
check.equal([mark.state() for mark in taste.marks(USER.key, "series") if mark.active],
            [{"opinion": "", "had": True}], "the later change is left as it is")

refused = client.post("/api/v1/feedback", json={
    "medium": "movie", "itemKey": "film-3", "surface": "catalogue", "opinion": "more"})
check.equal(refused.status_code, 400, "a mark that could never match is refused")
check.that("catalogue" in refused.json().get("detail", ""),
           "with a sentence that says why")

client.post("/api/v1/feedback", json={
    "medium": "podcast", "itemKey": "feed:abc", "surface": "catalogue",
    "opinion": "less"})
check.that(("catalogue", "podcast") in rebuilds and rebuilds[-2:][0] == ("shelf", "podcast"),
           "a podcast mark rebuilds both podcast shelves")

book = client.post("/nextread/api/v1/feedback", json={
    "itemKey": "B0BOOK", "surface": "catalogue", "had": True,
    "recommendationId": "1:discover:1:B0BOOK"})
check.equal(book.status_code, 200, "the book screen's own route takes a mark")
check.equal(book.json().get("medium"), "book", "always a book there")
check.equal(book.json().get("version"), compat_nextread.LEGACY_PROTOCOL,
            "in the shape that route always answers")
check.equal(rebuilds[-1], ("books", "book"), "a book mark rebuilds the book shelves")
with store.db() as conn:
    events = conn.execute(
        "SELECT asin, action FROM feedback_events WHERE user_key=?",
        (USER.key,)).fetchall()
check.equal([(row["asin"], row["action"]) for row in events], [("B0BOOK", "had")],
            "a book mark is recorded beside the book screen's other outcomes")


# --- capabilities, both routes ----------------------------------------------------
# Nothing configured: no media to ask for, no libraries to rank. The block is
# about marking suggestions, so it is there whatever else is or is not.
from app import listenarr, media  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

media.available = lambda: {}
jellyfin.library_ids = lambda medium: []
listenarr.configured = lambda: False
book_wants.allowance = lambda user: 3
book_wants.daily_cap = lambda user: 3
caps = client.get("/api/v1/capabilities")
check.equal(caps.status_code, 200, "capabilities answer")
check.equal(caps.json().get("feedback"), {"supported": True},
            "the request API says suggestions can be marked")
legacy = client.get("/nextread/api/v1/capabilities")
check.equal(legacy.status_code, 200, "the book screen's capabilities answer")
check.equal(legacy.json().get("feedback"), {"supported": True},
            "and say books can be marked there")

harness.cleanup()
raise SystemExit(check.report())
