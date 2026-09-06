"""The film and television shelves in a browser.

The ranker worked and had no page at all, so nobody could see it. What is
pinned here is the page, and the two things about it that are easy to get
wrong: it must not wait for a cold build, and it must be offered on the
strength of a Jellyfin library alone -- these shelves are built from what the
server already holds, so an installation with no Radarr still has one.
"""
import threading
import time

import harness

# No acquisition tool at all, deliberately: nothing here may depend on one.
harness.setup(RADARR_URL="", SONARR_URL="", LISTENARR_URL="", BUSKARR_URL="",
              JELLYFIN_USER="")

from fastapi.testclient import TestClient  # noqa: E402

from app import (jellyfin, main, media, recommendations,  # noqa: E402
                 sessions, store)

check = harness.Check("discover page")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
jellyfin.user_from_token = lambda token: MATT
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: {
    "movie": ["lib-films"], "series": ["lib-tv"]}.get(medium, [])


def item(item_id, title, *, progress=0, genres=(), people=()):
    return {
        "Id": item_id,
        "Name": title,
        "Genres": list(genres),
        "People": [{"Name": name, "Type": role} for name, role in people],
        "Studios": [],
        "DateCreated": "2026-01-01T00:00:00Z",
        "CommunityRating": 7.0,
        "UserData": {"Played": progress >= 100, "PlayedPercentage": progress,
                     "IsFavorite": False},
    }


LIBRARY = {
    "movie": [
        item("watched", "A Film You Finished", progress=100,
             genres=("Comedy",), people=(("Pat Helm", "Actor"),)),
        item("offer", "The Film To Offer", genres=("Comedy",),
             people=(("Pat Helm", "Director"),)),
    ],
    "series": [
        item("tv-watched", "A Show You Finished", progress=100,
             genres=("Drama",)),
        item("tv-offer", "The Show To Offer", genres=("Drama",)),
    ],
}

# Held shut so the page has to answer while a build is still running. A cold
# film build on the live server is twelve seconds of Jellyfin, and this is the
# only way to assert the page does not sit through one.
gate = threading.Event()
asked: list[str] = []


def slow_library(medium, uid, libraries):
    asked.append(medium)
    gate.wait(10)
    return LIBRARY.get(medium, [])


jellyfin.recommendation_items_for_user = slow_library

media.forget()
client = TestClient(main.app, raise_server_exceptions=False,
                    follow_redirects=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", MATT.id))

# --- offered without an acquisition tool ------------------------------------
check.equal(media.available(), {},
            "no acquisition tool is configured, so nothing can be asked for")
check.equal(main.discover_media(), ["movie", "series"],
            "and the shelves are offered anyway: they are built from what "
            "Jellyfin already holds, so Radarr has no say in it")

# --- a cold shelf does not hold the page ------------------------------------
started = time.monotonic()
cold = client.get("/discover?medium=movie")
took = time.monotonic() - started
check.equal(cold.status_code, 200, "a cold shelf still renders a page")
check.that(took < 5.0,
           f"and does not wait for the build ({took:.1f}s, with the library "
           "read still blocked)")
check.that("Working out what to watch" in cold.text,
           "saying which phase it is in, rather than nothing at all")

# The reload that follows must not start a second build of the same shelf.
client.get("/discover?medium=movie")
check.equal(asked.count("movie"), 1,
            "and a reload joins the build already running rather than "
            "starting another")

# --- and then it is there ---------------------------------------------------
gate.set()
for _ in range(100):
    warm = client.get("/discover?medium=movie")
    if "The Film To Offer" in warm.text:
        break
    time.sleep(0.1)
check.that("The Film To Offer" in warm.text, "the shelf arrives on its own")
check.that("Working out what to watch" not in warm.text,
           "and stops saying it is working on it")
check.that("directed by Pat Helm" in warm.text,
           "each row carries why it is there")
check.that("A Film You Finished" not in warm.text,
           "and nothing already watched is offered back")
check.that('<ol class="shelf"' in warm.text,
           "the shelf is an ordered list, because the order IS the "
           "recommendation")
check.that("Ask for The Film To Offer" not in warm.text,
           "with no request button: everything on this shelf is already in "
           "the library")

# --- one page, three shelves, one noun --------------------------------------
check.that('href="/discover?medium=series"' in warm.text,
           "the picker offers the other shelf")
check.that('aria-current="page">Films' in warm.text,
           "and marks the one being read, which is what announces it")

series = client.get("/discover?medium=series")
check.equal(series.status_code, 200, "the series shelf answers too")
check.that('aria-current="page">Series' in series.text,
           "and the picker follows")

# An unknown medium is a stale link, not an error: somebody in the right place
# with the wrong query string. The search page treats its own picker the same.
fallback = client.get("/discover?medium=music")
check.equal(fallback.status_code, 200, "an unknown shelf falls back")
check.that('aria-current="page">Films' in fallback.text,
           "to the first one this installation has")

# --- refreshing names the shelf it refreshes --------------------------------
refreshed = client.post("/discover/refresh", data={"medium": "movie"})
check.equal(refreshed.status_code, 303, "refreshing redirects")
check.that("medium=movie" in refreshed.headers["location"],
           "back to the shelf that was refreshed, not to whichever is first")
check.equal(recommendations.shelf_or_start(MATT, medium="movie")[1],
            recommendations.BUILDING,
            "and the shelf really was dropped, so the next read rebuilds it")

# --- a build that cannot finish says so, once --------------------------------
recommendations.forget()


def unavailable(medium, uid, libraries):
    raise jellyfin.JellyfinUnavailable("no route to host")


jellyfin.recommendation_items_for_user = unavailable
broken = client.get("/discover?medium=movie")
for _ in range(100):
    broken = client.get("/discover?medium=movie")
    if "could not be reached" in broken.text:
        break
    time.sleep(0.1)
check.equal(broken.status_code, 200,
           "a shelf that cannot be built is still a page")
check.that("Jellyfin could not be reached" in broken.text,
           "which says what went wrong instead of working on it forever")

harness.cleanup()
raise SystemExit(check.report())
