"""The request list must not depend on a fresh read of the whole book library.

That read is 3,550 items with `People` attached on this deployment -- nine of
a shelf build's twelve seconds -- and the home page asks for it, to settle
which books have arrived. Two things followed from asking for it the way this
did, and both were measured on the live server on 2026-09-06:

* an expired index made the home page wait for a new one, 14.4 s against 0.5 s
  with one in hand;
* a listing that failed took the whole list away behind a 503, films and
  series and music included, none of whose state was in any doubt.
"""
import threading
import time

import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid:4545",
              RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
              RADARR_QUALITY_PROFILE_ID="4",
              PLAYLIST_OWNER="matt", JELLYFIN_USER="")

from fastapi.testclient import TestClient  # noqa: E402

from app import (backends, buskarr, jellyfin, main, media,  # noqa: E402
                 radarr, sessions, sonarr, store, wants)
from app.books import shelves  # noqa: E402
from app.books import store as book_store  # noqa: E402

check = harness.Check("book index availability")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
jellyfin.user_from_token = lambda token: MATT
jellyfin.credential_rejected = lambda force=True: False
jellyfin.set_playlist = lambda uid, name, ids: "playlist-1"
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media._registry = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 3, ("lib-films",)),
    media.BOOK: media.Medium(media.BOOK, "Books", ("book", "series"), 3,
                             ("lib-books",)),
}
media._owned._value = jellyfin.Owned()
media._owned._built_at = time.monotonic()
media.episode_counts = lambda provider_ids: {}
sonarr.acquisition_progress = lambda backend_ids: {}
buskarr.state = lambda ref: None
radarr.arrived = lambda keys, owned: set()

INDEX = ({"B0OWNED"}, {"a book you have": {"someone"}})

# Held shut so a caller has to answer while a read of the library is running.
gate = threading.Event()
reads: list[str] = []


def slow_books(uid):
    reads.append(uid)
    gate.wait(10)
    return []


jellyfin.books = slow_books


def expire_index():
    """Age the cached index past its lifetime, as fifteen minutes would."""
    with shelves._cache_guard:
        shelves._owned_cache[MATT.key] = (
            time.monotonic() - shelves.OWNED_TTL_SECONDS - 1, INDEX)


# --- an expired index is served while the new one is built ------------------
expire_index()
started = time.monotonic()
served = shelves.owned_index(MATT)
took = time.monotonic() - started
check.equal(served, INDEX, "the index in hand is what the caller gets")
check.that(took < 5.0,
           f"and it does not wait for a new one ({took:.1f}s, with the "
           "library read still blocked)")
check.equal(len(reads), 1, "a rebuild was started")

# The next four readers join that rebuild rather than each starting one.
for _ in range(4):
    shelves.owned_index(MATT)
check.equal(len(reads), 1, "and the readers behind it do not start their own")

gate.set()
for _ in range(100):
    with shelves._cache_guard:
        entry = shelves._owned_cache.get(MATT.key)
    if entry and time.monotonic() - entry[0] <= shelves.OWNED_TTL_SECONDS:
        break
    time.sleep(0.05)
check.that(entry is not None
           and time.monotonic() - entry[0] <= shelves.OWNED_TTL_SECONDS,
           "the rebuild lands on its own, so the next reader has a fresh one")

# --- a listing that fails does not take the request list away ---------------
book_store.record_request(MATT.key, "B0WAITING", "Still Coming", ["Someone"])
store.record(MATT.key, media.MOVIE, "tmdb:550", "movie", "A Film", "1999",
             1, "r1")

with shelves._cache_guard:
    shelves._owned_cache.clear()


def refuses(uid):
    raise jellyfin.JellyfinUnavailable("timed out")


jellyfin.books = refuses

rows: list[dict] = []
try:
    rows = wants.states(MATT)
    check.that(True, "the request list answers with the book library down")
except jellyfin.JellyfinUnavailable as exc:
    # Caught rather than allowed to end the file: raising here is the defect,
    # and a crash would take every assertion below it with it.
    check.that(False, "the request list answers with the book library down "
                      f"(raised {type(exc).__name__}: {exc})")
by_key = {row["itemKey"]: row for row in rows}
check.that("tmdb:550" in by_key,
           "a film's state is reported, whatever the book library did")
check.that("B0WAITING" in by_key,
           "and the book is still on the list rather than quietly dropped")
check.equal(by_key.get("B0WAITING", {}).get("state"), wants.ON_ITS_WAY,
            "with the state its age implies, since nothing could settle it")

client = TestClient(main.app, raise_server_exceptions=False,
                    follow_redirects=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", MATT.id))
page = client.get("/")
check.equal(page.status_code, 200,
            "and the home page is a page, not a 503 about an outage that "
            "only touched one of its media")
check.that("A Film" in page.text and "Still Coming" in page.text,
            "with everything that was asked for on it")

harness.cleanup()
raise SystemExit(check.report())
