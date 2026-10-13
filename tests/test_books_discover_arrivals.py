"""Books added outside Nextup leave a cached Discover shelf.

Guinea Pigs Online has an ordinary first-book title and named sequels. The
library listing must remove the first book and other editions of it without
mistaking that title for every book in the series. None of this needs a
Nextup request or a Listenarr import event.
"""
import threading
import time

import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid:4545", JELLYFIN_USER="")

from fastapi.testclient import TestClient  # noqa: E402

from app import backends, compat_nextread, jellyfin, main, media, sessions, store  # noqa: E402
from app.books import engine, shelves, wants  # noqa: E402

check = harness.Check("Discover book arrivals")
store.init()
MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
OTHER = jellyfin.User(id="user-other", name="other")
AUTHORS = ["Amanda Swift", "Jennifer Gray", "Sarah Horne"]
FIRST = {"asin": "B07CGQ38SZ", "title": "Guinea Pigs Online", "authors": AUTHORS}
EDITION = {**FIRST, "asin": "OTHER-EDITION"}
SEQUEL = {"asin": "B071VCPVRC", "title": "Guinea Pigs Online: Furry Towers",
          "authors": AUTHORS}
UNRELATED = {**FIRST, "asin": "OTHER-AUTHOR", "authors": ["Somebody Else"]}
LATER = {"asin": "LATER", "title": "Guinea Pigs Online 2", "authors": AUTHORS}
CANDIDATES = [FIRST, EDITION, SEQUEL, UNRELATED, LATER]
INITIAL = [r["asin"] for r in CANDIDATES]
AFTER = [SEQUEL["asin"], UNRELATED["asin"], LATER["asin"]]
LIBRARY_BOOK = {
    "Id": "guinea-1", "Name": FIRST["title"], "IndexNumber": 1,
    "SeriesName": FIRST["title"], "ProviderIds": {"Audible": FIRST["asin"]},
    "AlbumArtist": "Jennifer Gray, Amanda Swift",
}
library = []
builds = []
reads = []
build_gate = threading.Event()
build_gate.set()
read_gate = threading.Event()
read_gate.set()


def fake_books(uid):
    reads.append(uid)
    if not read_gate.wait(5):
        raise AssertionError("the test never released its library read")
    return list(library) if uid == MATT.id else []


def fake_run(user, update_playlist=True):
    if not build_gate.wait(5):
        raise AssertionError("the test never released its shelf build")
    builds.append(user.key)
    visible = list(library) if user.id == MATT.id else []
    index = engine._owned_index(visible)
    return {
        "own": [{"id": "read-next", "title": "Something To Read", "why": []}],
        "discover": [dict(row, why=[], recommendation_id=f"1:discover:{n}:{row['asin']}")
                     for n, row in enumerate(CANDIDATES, 1)
                     if not engine._already_owned(row, *index)],
        "owned_index": index, "books_by_series": engine._books_by_series(visible),
        "playlist_name": "Next Read", "playlist_id": None,
        "seeds": 1, "library": len(visible), "ratings": 0,
    }


def asins(data):
    return [row["asin"] for row in data["discover"]]


def settle(key, refreshing):
    deadline = time.monotonic() + 5
    while key in refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    check.that(key not in refreshing, "the background read finishes")


engine.run = fake_run
jellyfin.books = fake_books
shelves.write_playlist = lambda user, data: None

# A shelf was built before a book was added by another route. A later search
# or arrival sweep sees the new listing while the shelf is still fresh.
original = shelves.result(MATT, update_playlist=False)
check.equal(asins(original), INITIAL, "all unowned suggestions start on the shelf")
library.append(LIBRARY_BOOK)
shelves._list_and_publish(MATT)
served = shelves.result(MATT, update_playlist=False)
check.equal(asins(served), AFTER,
            "the cached shelf drops an outside arrival and its other edition")
check.equal(len(builds), 1, "an arrival does not pay for a new recommendation build")
check.equal(served["own"], original["own"], "the reading shelf stays available")
check.equal(asins(original), INITIAL, "the old snapshot is not mutated under another reader")
check.equal(asins(store.get_shelf(MATT.key)[0]), INITIAL,
            "filtering does not permanently dismiss a book or erase its attribution")
check.equal(served["discover"][0]["recommendation_id"],
            original["discover"][2]["recommendation_id"],
            "a surviving suggestion keeps its original attribution")
check.equal(asins(shelves.result(OTHER, update_playlist=False)), INITIAL,
            "another account's library visibility is respected")

# The same filter must cover a persisted stale shelf while its expensive
# recommendation rebuild is outstanding.
build_gate.clear()
with shelves._cache_guard:
    shelves._cache.pop(MATT.key)
started = time.monotonic()
restored = shelves.result(MATT, update_playlist=False)
check.equal(asins(restored), AFTER, "a disk-restored shelf drops the arrival too")
check.that(time.monotonic() - started < 1,
           "restoring a shelf does not wait for its recommendation rebuild")
check.equal(len(builds), 2, "the stale answer is served before its rebuild finishes")
build_gate.set()
settle(MATT.key, shelves._refreshing)

# Discover itself has to start the brief ownership refresh. Otherwise a warm
# shelf on the web never asks for a newer library until the whole shelf expires.
library.clear()
shelves.result(MATT, force=True, update_playlist=False)
with shelves._cache_guard:
    at, index = shelves._owned_cache[MATT.key]
    shelves._owned_cache[MATT.key] = (
        time.monotonic() - shelves.OWNED_TTL_SECONDS - 1, index)
library.append(LIBRARY_BOOK)
read_gate.clear()
before_reads = len(reads)
before_builds = len(builds)
started = time.monotonic()
waiting = shelves.result(MATT, update_playlist=False)
shelves.result(MATT, update_playlist=False)
check.equal(asins(waiting), INITIAL, "the last known library still answers during refresh")
check.that(time.monotonic() - started < 1, "a slow library refresh does not block Discover")
read_gate.set()
settle(MATT.key, shelves._refreshing_owned)
check.equal(len(reads) - before_reads, 1, "concurrent readers share one library refresh")
check.equal(len(builds), before_builds, "ownership refresh does not rebuild recommendations")
check.equal(asins(shelves.result(MATT, update_playlist=False)), AFTER,
            "the next read removes the arrival as soon as the new listing is known")

# Web and EchoFin must see the same answer, including on the cached path.
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media._registry = {
    media.BOOK: media.Medium(media.BOOK, "Books", ("book", "series"), 3, ("lib-books",))}
jellyfin.user_from_token = lambda token: MATT
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: ["lib-books"] if medium == media.BOOK else []
wants.states = lambda key, owned: []
main.app.dependency_overrides[compat_nextread.caller] = lambda: MATT
client = TestClient(main.app, raise_server_exceptions=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", MATT.id))
api = client.get("/nextread/api/v1/shelves")
check.equal(api.status_code, 200, "EchoFin's shelf route answers")
check.equal([row["asin"] for row in api.json().get("suggestions", [])], AFTER,
            "EchoFin is no longer offered an owned book")
page = client.get("/discover?medium=book")
check.equal(page.status_code, 200, "the web Discover page answers")
check.that('value="B07CGQ38SZ"' not in page.text
           and 'value="OTHER-EDITION"' not in page.text
           and 'value="B071VCPVRC"' in page.text,
           "the web page removes the first book's ask button, keeping the sequel")

# After a restart, the API can obtain ownership during its request-status
# check, after it already restored the old shelf. That same response must use
# the new index, rather than waiting for the next page load to agree with it.
store.put_shelf(MATT.key, original)
with shelves._cache_guard:
    shelves._cache.pop(MATT.key)
    shelves._owned_cache.pop(MATT.key)
build_gate.clear()
cold_api = client.get("/nextread/api/v1/shelves")
check.equal(cold_api.status_code, 200, "the first API read after a restart answers")
check.equal([row["asin"] for row in cold_api.json().get("suggestions", [])], AFTER,
            "ownership learned during a cold API read filters that same response")
build_gate.set()
settle(MATT.key, shelves._refreshing)

# A failed read is not evidence that anything arrived. Keep the previous
# known-good answer, and allow another read to retry.
# Restore the pre-arrival recommendation snapshot, since the cold API's full
# rebuild has legitimately replaced it with recommendations for the new library.
known = shelves._owned_cache[MATT.key][1]
with shelves._cache_guard:
    shelves._cache[MATT.key] = (time.monotonic(), original, False)
    shelves._owned_cache[MATT.key] = (
        time.monotonic() - shelves.OWNED_TTL_SECONDS - 1, known)


def unavailable(uid):
    raise jellyfin.JellyfinUnavailable("library is down")


jellyfin.books = unavailable
check.equal(asins(shelves.result(MATT, update_playlist=False)), AFTER,
            "a failed refresh preserves the last known library answer")
settle(MATT.key, shelves._refreshing_owned)
check.equal(shelves._owned_cache[MATT.key][1], known,
            "a failed refresh does not replace ownership with an empty library")
jellyfin.books = fake_books
library.clear()
shelves._list_and_publish(MATT)
check.equal(asins(shelves.result(MATT, update_playlist=False)), INITIAL,
            "a book removed from the library can be suggested again")

# A named sequel and the first book must not count as each other under title
# fallback. Marketing subtitles can still bridge two editions of one book.
first_index = engine._owned_index([LIBRARY_BOOK])
check.that(not wants._arrived(SEQUEL, *first_index),
           "owning the first book does not fulfil a named sequel request")
sequel_index = engine._owned_index([{**LIBRARY_BOOK, "Name": SEQUEL["title"],
                                     "ProviderIds": {"Audible": SEQUEL["asin"]}}])
check.that(not engine._already_owned(FIRST, *sequel_index),
           "owning only a named sequel does not hide book one")
check.that(engine._already_owned(
    {**EDITION, "title": "Guinea Pigs Online: An Unabridged Edition"}, *first_index),
    "a descriptive subtitle can still identify another edition of book one")

main.app.dependency_overrides.clear()
harness.cleanup()
raise SystemExit(check.report())
