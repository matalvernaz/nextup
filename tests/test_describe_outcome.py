"""What became of a request for a title's audio description.

A request used to end at "it will appear when it is ready". When no source had
a description it never appeared and nothing said so: a listener asked for
Heat's described track twice, five days apart (EchoFin audit 2026-09-23,
feature 5). These pin the relay: describarr is asked with the path a client
cannot see, its answer is passed on, and anything it cannot answer is said
as "unknown" rather than guessed at.
"""
import harness

harness.setup(DESCRIBARR_URL="http://describarr.invalid:8686")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, describarr, jellyfin  # noqa: E402

check = harness.Check("describe outcome")

USER = jellyfin.User(id="u1", name="defender", is_admin=False)

HEAT = {"Id": "m1", "Type": "Movie", "Name": "Heat", "ProductionYear": 1995,
        "Path": "/media/movies/Heat (1995)/Heat (1995).mkv"}
SERIES = {"Id": "s1", "Type": "Series", "Name": "Futurama",
          "Path": "/media/TV shows/Futurama"}


def ask(item, *, answer=(200, {"state": "no_match", "detail": "", "at": "2026-09-23T04:10:00"}),
        raises=None):
    """GET the status of `item` with a stubbed describarr. Returns
    `(response, sent)`: what the caller got, and what describarr was asked."""
    sent = {}
    jellyfin.user_from_token = lambda _token: USER
    jellyfin.item_with_path = lambda _id, _user, found=item: found

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
            return httpx.Response(status, json=body,
                                  request=httpx.Request("GET", url))

    describarr.httpx.Client = StubClient
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/api/v1/describe/{item.get('Id', 'x') if item else 'x'}",
                          headers={"X-Emby-Token": "token"})
    return response, sent


print("=== a film is asked about by its file ===")
response, sent = ask(HEAT)
check.equal(response.status_code, 200, "answers 200")
check.equal(sent["url"], "http://describarr.invalid:8686/outcome",
            "describarr's /outcome is what is asked")
check.equal(sent["params"].get("path"), HEAT["Path"],
            "with the path Jellyfin has, which a listener's client cannot see")
check.equal(response.json()["state"], "no_match", "and its answer is passed on")
check.equal(response.json()["at"], "2026-09-23T04:10:00", "with when")
check.equal(response.json()["itemId"], "m1", "naming the item asked about")

print("=== a refused candidate is not the same as finding nothing ===")
response, _ = ask(HEAT, answer=(200, {"state": "rejected",
                                       "detail": "alignment score too low",
                                       "at": "2026-09-18T02:00:00"}))
check.equal(response.json()["state"], "rejected", "a rejection stays a rejection")
check.equal(response.json()["detail"], "alignment score too low", "with its reason")

print("=== what it cannot answer is unknown, not guessed ===")
response, sent = ask(SERIES)
check.equal(response.json()["state"], "unknown",
            "a series is a folder of answers, not one")
check.that("url" not in sent, "and describarr is not asked about it")

response, _ = ask(HEAT, answer=(200, {"state": "exploded"}))
check.equal(response.json()["state"], "unknown",
            "a word this server does not know is not passed on")

response, _ = ask(HEAT, answer=(404, {"detail": "Not found."}))
check.equal(response.status_code, 200, "a describarr that predates /outcome is not an error")
check.equal(response.json()["state"], "unknown", "it is simply not known")

print("=== describarr out of reach is an outage, said as one ===")
response, _ = ask(HEAT, raises=httpx.ConnectError("refused"))
check.equal(response.status_code, 503, "503, so a client reads it as an outage")
check.that("could not be reached" in response.json()["detail"],
           "with a sentence a listener can be told")

response, _ = ask(None)
check.equal(response.status_code, 404, "an item the account cannot see is 404")

harness.cleanup()
raise SystemExit(check.report())
