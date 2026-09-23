"""Handing a whole list to the JSON API, the way a native client would.

The same two steps as the browser: a POST that matches and acquires nothing,
then a second POST that asks for the lines a person chose. What is pinned here
is that the first of those spends nothing, that one account cannot confirm
another's list, and that a client is told the line numbers rather than having
to work out what to send back.
"""
import time

import harness

harness.setup(
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_ALBUM_COST="1", MUSIC_ARTIST_COST="3",
    MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0", IMPORT_MAX_ROWS="6",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, arr, buskarr, imports, jellyfin, media, store  # noqa: E402

check = harness.Check("import api")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="user-kid", name="kid", is_admin=False)

#: The API authenticates on the token alone, so the token is what says who is
#: calling. Two of them, because half of what is asserted below is that one
#: account cannot reach the other's list.
BY_TOKEN = {"matt-token": MATT, "kid-token": KID}
jellyfin.user_from_token = lambda token: BY_TOKEN[token]

media._registry = {
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                              ("lib-music",)),
}
media._registry_built_at = time.monotonic()
media._registry_settled = True

CATALOGUE = [
    {"title": "Kind of Blue", "artist": "Miles Davis", "ref": "a1"},
    {"title": "Rumours", "artist": "Fleetwood Mac", "ref": "a2"},
]
buskarr.search = lambda q, unit, limit: [
    buskarr._result(dict(row, source="deezer"), unit) for row in CATALOGUE
    if any(word in f"{row['artist']} {row['title']}".casefold()
           for word in q.casefold().split() if len(word) > 3)
] if unit == "album" else []

asked: list[str] = []
buskarr.add = lambda unit, hit, by: (
    asked.append(hit.get("title", ""))
    or arr.AddResult(True, "Sent to buskarr.", "job:1", hit.get("title", "")))

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)


def as_(who: str) -> dict:
    return {"X-Emby-Token": who}


def settle(url: str, token: str, state: str, seconds: float = 10.0) -> dict:
    """Poll one list until it reaches a state, the way a client would."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = client.get(url, headers=as_(token)).json()
        if body["state"] == state:
            return body
        time.sleep(0.05)
    raise AssertionError(f"stayed in {body['state']!r}, wanted {state!r}")


LIST = ("Artist,Album\n"
        "Miles Davis,Kind of Blue\n"
        "Fleetwood Mac,Rumours\n"
        "Nobody At All,A Record Nobody Pressed\n")

# --- capabilities -----------------------------------------------------------
caps = client.get("/api/v1/capabilities?protocol=2",
                  headers=as_("matt-token")).json()
check.that(caps["importList"]["supported"],
           "a server that can be asked for something says it can be handed a "
           "list of them")
check.equal(caps["importList"]["maxRows"], 6,
            "and says how long a list it will take, so a client can refuse "
            "one before spending the upload")

# --- handing over a list ----------------------------------------------------
started = client.post("/api/v1/import", headers=as_("matt-token"),
                      json={"medium": "music", "unit": "album",
                            "filename": "theirs.csv", "text": LIST})
check.equal(started.status_code, 200, "a list is accepted")
body = started.json()
check.equal(body["total"], 3, "with every row of it counted")
check.equal(asked, [], "and nothing acquired by the call that uploaded it")
where = f"/api/v1/import/{body['importId']}"

body = settle(where, "matt-token", "ready")
states = {row["title"]: row["state"] for row in body["rows"]}
check.equal(states, {"Kind of Blue": "matched", "Rumours": "matched",
                     "A Record Nobody Pressed": "missing"},
            "every row comes back with what became of it, including the one "
            "that matched nothing")
check.that(all("line" in row for row in body["rows"]),
           "each carrying the line of the file it was, which is what a "
           "client sends back to choose it")
check.equal(body["remainingToday"], None,
            "an uncapped account is told so rather than given a number")

# --- one account cannot reach another's -------------------------------------
check.equal(client.get(where, headers=as_("kid-token")).status_code, 404,
            "another account is told there is no such list, which is also "
            "all it should learn")
check.equal(client.post(where + "/confirm", headers=as_("kid-token"),
                        json={"lines": [2, 3]}).status_code, 404,
            "and cannot confirm it")
check.equal(asked, [], "so nothing went to the acquisition tool")

check.equal(client.get(where).status_code, 401,
            "and a call with no token at all is refused, as everywhere else "
            "on this API")

# --- confirming -------------------------------------------------------------
confirmed = client.post(where + "/confirm", headers=as_("matt-token"),
                        json={"lines": [2, 3]})
check.equal(confirmed.status_code, 200, "the owner may confirm")
body = settle(where, "matt-token", "done")
check.equal(sorted(asked), ["Kind of Blue", "Rumours"],
            "and exactly the confirmed lines are asked for")
check.equal(len(body["report"]["asked"]), 2, "the report names both")

# --- a file that cannot be read ---------------------------------------------
bad = client.post("/api/v1/import", headers=as_("matt-token"),
                  json={"medium": "music", "text": "Year,Date\n2020,2020-01-01\n"})
check.equal(bad.status_code, 400,
            "a file with no title column is a bad request, not a list that "
            "matched nothing")
check.that("heading" in bad.json()["detail"],
           "and the reason is in the reply for a client to show")

check.equal(client.get("/api/v1/import/not-a-real-id",
                       headers=as_("matt-token")).status_code, 404,
            "an id that was never issued is not found")

harness.cleanup()
raise SystemExit(check.report())
