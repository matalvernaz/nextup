"""Asking for audio description on something already in the library.

Sonarr and Radarr already hand describarr what they download, so this path is
only ever about the backlog. What it has to get right is the handover: a film
is a file, a series and a season are folders, and a season is named after its
series rather than after itself.
"""
import harness

harness.setup(DESCRIBARR_URL="http://describarr.invalid:8686")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, describarr, jellyfin  # noqa: E402

check = harness.Check("describe")

USER = jellyfin.User(id="u1", name="matt", is_admin=True)

MOVIE = {"Id": "m1", "Type": "Movie", "Name": "The 5th Wave",
         "ProductionYear": 2016, "Path": "/media/movies/The 5th Wave.mkv"}
SERIES = {"Id": "s1", "Type": "Series", "Name": "2 Broke Girls",
          "Path": "/media/TV shows/2 Broke Girls"}
SEASON = {"Id": "s1e", "Type": "Season", "Name": "Season 1",
          "SeriesName": "2 Broke Girls", "IndexNumber": 1,
          "Path": "/media/TV shows/2 Broke Girls/Season 1"}


def post(item, *, answer=(202, "Accepted — queued"), raises=None):
    """Call the endpoint with `item` in the library and a stubbed describarr.

    Returns `(response, params)` — what the caller got, and what describarr was
    asked for, so the handover can be asserted rather than assumed.
    """
    sent = {}
    jellyfin.user_from_token = lambda _token: USER
    jellyfin.item_with_path = lambda _id, found=item: found

    class StubClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, url, params=None, headers=None):
            sent["url"] = url
            sent["params"] = params or {}
            if raises is not None:
                raise raises
            status, body = answer
            return httpx.Response(status, text=body,
                                  request=httpx.Request("GET", url))

    describarr.httpx.Client = StubClient
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/v1/describe", json={"itemId": item.get("Id", "x")},
                           headers={"X-Emby-Token": "token"})
    return response, sent


print("=== a film is handed over as a file ===")
response, sent = post(MOVIE)
check.equal(response.status_code, 200, "a queued film answers 200")
check.equal(response.json()["queued"], True, "and says it was queued")
check.equal(sent["params"].get("path"), MOVIE["Path"],
            "the film goes over as path=, which queues a single-file retry")
check.that("dir" not in sent["params"],
           "and not as a directory, which would walk the whole movies folder")
check.equal(sent["params"].get("title"), "The 5th Wave",
            "with the title Jellyfin knows, not one inferred from the path")
check.equal(sent["params"].get("year"), "2016",
            "and the year, which is how describarr tells two films apart")

print("=== a series is handed over as a folder ===")
response, sent = post(SERIES)
check.equal(response.status_code, 200, "a queued series answers 200")
check.equal(sent["params"].get("dir"), SERIES["Path"],
            "the series goes over as dir=, which walks every season")
check.that("path" not in sent["params"], "and not as a single file")
check.equal(sent["params"].get("title"), "2 Broke Girls", "named as itself")

print("=== a season is named after its series ===")
# The trap. A season's own name is "Season 1", which is not a title anything
# can search a description source for, and describarr searches by title.
response, sent = post(SEASON)
check.equal(response.status_code, 200, "a queued season answers 200")
check.equal(sent["params"].get("dir"), SEASON["Path"],
            "the season goes over as its own folder, which is walked in turn")
check.equal(sent["params"].get("title"), "2 Broke Girls",
            "and is named after its series, never 'Season 1'")

print("=== what cannot be asked for ===")
book = {"Id": "b1", "Type": "AudioBook", "Name": "A Book", "Path": "/media/books/a"}
response, _ = post(book)
check.equal(response.status_code, 400, "an audiobook cannot be described")
check.that("AudioBook" in response.json()["detail"],
           "and the refusal names what was asked about")

pathless = {"Id": "p1", "Type": "Movie", "Name": "Ghost", "Path": ""}
response, _ = post(pathless)
check.equal(response.status_code, 404,
            "an item Jellyfin cannot place on disk is refused here")
check.that("Ghost" in response.json()["detail"],
           "naming the title, not a path the listener never supplied")

print("=== when describarr will not take it ===")
# Its own words are passed on. "No AudioVault match" is the ordinary answer for
# a film nothing has described, and it is not a failure of this service.
# 409, not a 5xx. A client reads 5xx as "the server is down" and says so
# instead of repeating the reason -- and "nothing has described this film" is
# the ordinary answer here, not an outage.
response, _ = post(MOVIE, answer=(400, "Could not infer title from path"))
check.equal(response.status_code, 409, "a refusal is a refusal, not an outage")
check.that("infer title" in response.json()["detail"],
           "carrying describarr's own reason, which is better than ours")

response, _ = post(MOVIE, raises=httpx.ConnectError("no route"))
check.equal(response.status_code, 503, "unreachable is a different answer")
check.that("could not be reached" in response.json()["detail"],
           "so a client can say which of the two happened")

print("=== capabilities ===")
jellyfin.user_from_token = lambda _token: USER
app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)
caps = client.get("/api/v1/capabilities",
                  headers={"X-Emby-Token": "token"}).json()
check.equal(caps["describe"]["supported"], True,
            "a server with describarr configured says so")

harness.cleanup()
raise SystemExit(check.report())
