"""A Jellyfin book shelf survives an installation with no acquisition tool."""
import os

import harness

harness.setup(LISTENARR_URL="", JELLYFIN_USER="")
harness.no_book_ratings()

from fastapi.testclient import TestClient

from app import jellyfin, listenarr, main, media, sessions, store
from app.books import audible, engine

check = harness.Check("book recommendations without Listenarr")
store.init()
user = jellyfin.User(id="reader", name="Reader")
jellyfin.user_from_token = lambda token: user
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: ["books"] if medium == "book" else []
library = [
    {"Id": "first", "Name": "First Volume", "SeriesName": "A Saga",
     "IndexNumber": 1, "AlbumArtist": "A Writer", "UserData": {"Played": True}},
    {"Id": "second", "Name": "Second Volume", "SeriesName": "A Saga",
     "IndexNumber": 2, "AlbumArtist": "A Writer", "UserData": {"Played": False}},
]
jellyfin.books = lambda uid: library
playlists = []
jellyfin.set_playlist = lambda uid, name, ids: playlists.append(list(ids)) or "list"

def no_listenarr():
    raise AssertionError("An unconfigured Listenarr must never be contacted")

listenarr._client = no_listenarr
audible.sims = lambda asin: []
media.forget()
client = TestClient(main.app, raise_server_exceptions=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("reader-token", user.id))

page = client.get("/discover?medium=book")
check.equal(page.status_code, 200, "the book page works with only Jellyfin")
check.that("Second Volume" in page.text, "it recommends the next owned volume")
check.that("Requests for books are not enabled" in page.text,
           "it explains why acquisition is unavailable")
check.that('action="/books/want"' not in page.text, "it offers no dead request button")
check.that("daily limit" not in page.text and "more books today" not in page.text,
           "it advertises no allowance for a disabled feature")
check.that(["second"] in playlists, "other Jellyfin clients still get the reading list")
check.equal(media.available(), {}, "recommendations do not enable acquisition")
check.equal(store.get_audible_alias("item:first"), None,
            "an absent catalogue does not become a cached missing edition")
check.equal(listenarr.queued_asins(), set(), "there is no acquisition queue to query")
check.equal(listenarr.audible_search("a title"), [], "disabled search needs no HTTP")

headers = {"X-Emby-Token": "reader-token"}
caps = client.get("/nextread/api/v1/capabilities", headers=headers).json()
check.equal(caps["libraryIds"], ["books"], "native clients retain the book shelf")
for feature in ("want", "search", "cancel", "seriesWant"):
    check.equal(caps[feature]["supported"], False, f"{feature} reports its dependency")
check.equal(caps["dismiss"]["supported"], True, "personal dismissals still work")
check.equal(caps["summary"]["supported"], True, "direct Audible summaries still work")

shelf = client.get("/nextread/api/v1/shelves", headers=headers)
check.equal(shelf.status_code, 200, "the existing audiobook API still works")
check.equal([r["id"] for r in shelf.json()["owned"]], ["second"],
            "the native and browser shelves agree")

# Connecting the catalogue afterwards must be enough to enrich the same seed.
os.environ["LISTENARR_URL"] = "http://listenarr.invalid"
listenarr.audible_search = lambda query, **kwargs: [
    {"asin": "RESOLVED", "title": "First Volume", "authors": [{"name": "A Writer"}]}]
audible.sims = lambda asin: [{"asin": "NEIGHBOUR"}] if asin == "RESOLVED" else []
check.equal(engine._seed_sims(library[0]), [{"asin": "NEIGHBOUR"}],
            "connecting Listenarr enriches a previously unresolved seed immediately")
check.equal(store.get_audible_alias("item:first"), "RESOLVED",
            "only a real catalogue answer records the edition alias")
caps = client.get("/nextread/api/v1/capabilities", headers=headers).json()
check.equal(caps["want"]["supported"], True, "connecting Listenarr enables requests")

harness.cleanup()
raise SystemExit(check.report())
