"""Somebody who asked for a title's audio description is told when it is ready.

The notifications plan of 2026-10-31 (Part 4) keeps one new notice in this
service, because it is about something a person asked for: "the description
you asked for is ready". Asking for a film's description remembers who asked.
The arrivals pass asks describarr what became of it, tells them once when it
is there, and forgets it when describarr gave up, when the title leaves the
library, or after 30 days. Same switch, same server flag and same way of
sending as every other request notice.
"""
import threading
import time

import harness

harness.setup(DESCRIBARR_URL="http://describarr.invalid:8686", REQUEST_NOTICES="on")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, config, describarr, jellyfin, notices, store  # noqa: E402

check = harness.Check("description notices")
store.init()

KID = jellyfin.User(id="u-kid", name="Kid", is_admin=False)
OTHER = jellyfin.User(id="u-other", name="Other", is_admin=False)
QUIET = jellyfin.User(id="u-quiet", name="Quiet", is_admin=False)
NEVER = jellyfin.User(id="u-never", name="Never", is_admin=False)
ACCOUNTS = [KID, OTHER, QUIET, NEVER]
TOKENS = {"kid-token": KID, "other-token": OTHER, "quiet-token": QUIET,
          "never-token": NEVER}
jellyfin.accounts = lambda: list(ACCOUNTS)
jellyfin.user_from_token = lambda token: TOKENS[token]


def film(name: str) -> dict:
    return {"Type": "Movie", "Name": name, "ProductionYear": 2001,
            "Path": f"/media/movies/{name} (2001)/{name} (2001).mkv"}


def file_of(item_id: str) -> str:
    """The name describarr knows a file by, whatever folder it is reached through."""
    return LIBRARY[item_id]["Path"].rsplit("/", 1)[-1]


#: What Jellyfin holds, by item id.
LIBRARY = {
    "s-futurama": {"Type": "Series", "Name": "Futurama",
                   "Path": "/media/TV shows/Futurama"},
    "se-futurama-1": {"Type": "Season", "Name": "Season 1", "SeriesName": "Futurama",
                      "Path": "/media/TV shows/Futurama/Season 1"},
    "e-pilot": {"Type": "Episode", "Name": "Space Pilot 3000", "SeriesName": "Futurama",
                "Path": "/media/TV shows/Futurama/Season 1/"
                        "Futurama - S01E01 - Space Pilot 3000.mkv"},
}
for film_id, film_name in (("m-heat", "Heat"), ("m-up", "Up"), ("m-ghost", "Ghost"),
                           ("m-solaris", "Solaris"), ("m-alien", "Alien"),
                           ("m-a", "Arrival"), ("m-b", "Brick"), ("m-c", "Cube"),
                           ("m-gone", "Gone"), ("m-moved", "Moved"), ("m-after", "After"),
                           ("m-jaws", "Jaws"), ("m-brazil", "Brazil"), ("m-rocky", "Rocky"),
                           ("m-tron", "Tron"), ("m-broken", "Broken"), ("m-later", "Later"),
                           ("m-other", "Other Film"), ("m-old", "Old"),
                           ("m-recent", "Recent")):
    LIBRARY[film_id] = film(film_name)
for item_id, item in LIBRARY.items():
    item["Id"] = item_id

#: Jellyfin not answering item lookups, and items whose answer cannot be read.
jellyfin_items = {"down": False}
UNREADABLE: set[str] = set()


def fake_item_with_path(item_id, user_id):
    """Jellyfin's item lookup: None for an item it does not have, and
    JellyfinUnavailable when it does not answer. The two are different, and a
    pass that treats not answering as gone would forget everybody's asks the
    moment Jellyfin restarts."""
    if jellyfin_items["down"]:
        raise jellyfin.JellyfinUnavailable("Jellyfin did not answer")
    if item_id in UNREADABLE:
        # What `response.json()` raises on an answer that is not JSON, which
        # `item_with_path` does not catch: a failure nobody foresaw.
        raise ValueError("Expecting value: line 1 column 1 (char 0)")
    found = LIBRARY.get(item_id)
    return dict(found) if found is not None else None


jellyfin.item_with_path = fake_item_with_path

#: What describarr says of each file, by the file's name.
OUTCOMES: dict[str, str] = {}
#: File names describarr will not take, the way it refuses a title it cannot place.
REFUSED: set[str] = set()
describarr_up = {"now": True}
#: Every path describarr's /outcome was asked about, empty ones included.
asked_about: list[str] = []


class FakeDescribarr:
    """describarr over HTTP, answering the way the real one does.

    `/retry` queues one film by `path=`, or a folder by `dir=`, and answers 202.
    `/outcome` answers by the file's name, says unknown when it has no record,
    and answers 400 when given no path at all, which `describarr.outcome`
    raises as an httpx.HTTPError: the same exception as describarr not
    answering. Not answering is a refused connection.
    """

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, params=None, headers=None):
        params = params or {}
        request = httpx.Request("GET", url)
        if not describarr_up["now"]:
            raise httpx.ConnectError("Connection refused", request=request)
        if url.endswith("/retry"):
            where = params.get("path") or params.get("dir") or ""
            if where.rsplit("/", 1)[-1] in REFUSED:
                return httpx.Response(400, text="Could not infer title from path",
                                      request=request)
            return httpx.Response(202, text=f"Accepted, queued {params.get('title')!r}",
                                  request=request)
        path = (params.get("path") or "").strip()
        asked_about.append(path)
        if not path:
            return httpx.Response(400, text="path is required.", request=request)
        state = OUTCOMES.get(path.rsplit("/", 1)[-1], "")
        if not state:
            return httpx.Response(200, json={"state": "unknown", "detail": "", "at": ""},
                                  request=request)
        return httpx.Response(200, json={"state": state, "detail": "",
                                         "at": "2026-10-31T04:00:00"},
                              request=request)


describarr.httpx.Client = FakeDescribarr

#: Every notice Jellyfin was asked to deliver, as (account, payload).
delivered: list[tuple[str, dict]] = []
jellyfin.notify_user = lambda user_id, payload: (
    delivered.append((user_id, payload)) or {"Sent": 1, "Failed": 0, "Dropped": 0})


def told(user):
    for thread in threading.enumerate():
        if thread.name == "request-notice":
            thread.join(5)
    return [payload["Body"] for user_id, payload in delivered if user_id == user.key]


app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)


def ask(user, item_id):
    token = next(token for token, account in TOKENS.items() if account is user)
    return client.post("/api/v1/describe", json={"itemId": item_id},
                       headers={"X-Emby-Token": token})


def waiting(user):
    return [(row["item_id"], row["title"]) for row in store.description_asks(user.key)]


def waiting_ids(user):
    return [item_id for item_id, _ in waiting(user)]


def asked_at(user, item_id):
    """When this account last asked about this title; 0 when it is not waited on,
    so a missing ask fails the check that reads it rather than ending the file."""
    for row in store.description_asks(user.key):
        if row["item_id"] == item_id:
            return row["asked_at"]
    return 0.0


def days_ago(user, item_id, days):
    with store.db() as conn:
        conn.execute("UPDATE description_asks SET asked_at=? WHERE user_key=? AND item_id=?",
                     (time.time() - days * 86400, user.key, item_id))


# --- asking for a film's description remembers who asked -------------------------

check.equal(ask(KID, "m-heat").status_code, 200, "asking for a film's description is taken")
check.equal(waiting(KID), [("m-heat", "Heat")],
            "and who asked is remembered, with what to call the film")

# --- a series or a season is not ---------------------------------------------------

check.equal(ask(KID, "s-futurama").status_code, 200, "a whole series is still asked for")
check.equal(ask(KID, "se-futurama-1").status_code, 200, "and so is a season")
check.equal(waiting(KID), [("m-heat", "Heat")],
            "but neither is remembered: describarr answers per episode, not per folder")

# --- an episode is called by its series and its name ------------------------------

# The route takes films, series and seasons only, so an episode reaches this
# only if it ever takes one; the name is checked where it is decided.
notices.asked_for_description(KID.key, "e-pilot", fake_item_with_path("e-pilot", KID.id))
check.equal(waiting(KID)[-1:], [("e-pilot", "Futurama, Space Pilot 3000")],
            "an episode is called by its series and its own name")

# --- asking again refreshes the ask rather than adding one -------------------------

days_ago(KID, "m-heat", 3)
before = asked_at(KID, "m-heat")
ask(KID, "m-heat")
check.equal(waiting_ids(KID).count("m-heat"), 1, "asking twice is still one ask")
check.that(asked_at(KID, "m-heat") > before + 2 * 86400,
           "and its wait starts again from the second")

# --- only once describarr took it -----------------------------------------------------

REFUSED.add(file_of("m-ghost"))
check.equal(ask(KID, "m-ghost").status_code, 409, "describarr refusing a film is a refusal")
describarr_up["now"] = False
check.equal(ask(KID, "m-solaris").status_code, 503, "and describarr not answering is an outage")
describarr_up["now"] = True
check.equal(waiting_ids(KID), ["e-pilot", "m-heat"],
            "neither is remembered, since nothing was queued")

# --- only where the server sends notices, whether or not the asker turned them on -

config.REQUEST_NOTICES = False
ask(QUIET, "m-alien")
check.equal(waiting(QUIET), [], "a server that sends no notices remembers nothing")
config.REQUEST_NOTICES = True
ask(QUIET, "m-alien")
check.equal(waiting(QUIET), [("m-alien", "Alien")],
            "one that does remembers it, though this account has not turned notices on yet")

# --- still on its way: it waits ---------------------------------------------------------

notices.set_wanted(KID.key, True)
for state in ("queued", "working"):
    OUTCOMES[file_of("m-heat")] = state
    notices.check_arrivals()
    check.equal(told(KID), [], f"{state}: nothing is said")
    check.that("m-heat" in waiting_ids(KID), f"{state}: and it is still waited on")
del OUTCOMES[file_of("m-heat")]
notices.check_arrivals()
check.equal(told(KID), [], "unknown: nothing is said")
check.that("m-heat" in waiting_ids(KID), "unknown: and it is still waited on")

# --- ready: said once, then forgotten ------------------------------------------------------

OUTCOMES[file_of("m-heat")] = "described"
notices.check_arrivals()
check.equal(told(KID), ["Heat has audio description now."], "a finished description is said")
payload = delivered[-1][1] if delivered else {}
check.equal((payload.get("Title"), payload.get("Kind"), payload.get("ThreadId"),
             payload.get("Data")),
            ("Audio description is ready", "described", "described", {"item": "m-heat"}),
            "as its own kind, not a request the phone would open the request list for, "
            "and naming the item without the itemId key the fork refuses")
check.that("m-heat" not in waiting_ids(KID), "and it is forgotten")
delivered.clear()
notices.check_arrivals()
check.equal(told(KID), [], "so it is said once")

# --- already described is said the same way ------------------------------------------------

ask(KID, "m-up")
OUTCOMES[file_of("m-up")] = "already_described"
notices.check_arrivals()
check.equal(told(KID), ["Up has audio description now."], "a film that had one already is said too")
check.that("m-up" not in waiting_ids(KID), "and forgotten")

# --- the episode -----------------------------------------------------------------------------

delivered.clear()
OUTCOMES[file_of("e-pilot")] = "described"
notices.check_arrivals()
check.equal(told(KID), ["Futurama, Space Pilot 3000 has audio description now."],
            "an episode is said by its series and its name")

# --- describarr gave up: forgotten, and nothing said -------------------------------------

delivered.clear()
for item_id, state in (("m-a", "no_match"), ("m-b", "rejected"), ("m-c", "error")):
    ask(KID, item_id)
    OUTCOMES[file_of(item_id)] = state
notices.check_arrivals()
check.equal(told(KID), [], "describarr giving up is not said")
check.that("m-a" not in waiting_ids(KID), "no_match: forgotten")
check.that("m-b" not in waiting_ids(KID), "rejected: forgotten")
check.that("m-c" not in waiting_ids(KID), "error: forgotten")

# --- gone from the library, or from the disk: forgotten without asking describarr ---------

delivered.clear()
ask(KID, "m-gone")
ask(KID, "m-moved")
ask(KID, "m-after")
OUTCOMES[file_of("m-after")] = "described"
del LIBRARY["m-gone"]
LIBRARY["m-moved"]["Path"] = ""
asked_about.clear()
notices.check_arrivals()
check.that("m-gone" not in waiting_ids(KID), "a title gone from the library is forgotten")
check.that("m-moved" not in waiting_ids(KID), "and so is one with no file on the server")
check.that("" not in asked_about, "describarr is never asked about a file with no path")
check.equal(told(KID), ["After has audio description now."],
            "and neither holds up the next title in the same pass")

# --- Jellyfin not answering: nothing is forgotten ------------------------------------------

delivered.clear()
ask(KID, "m-jaws")
OUTCOMES[file_of("m-jaws")] = "described"
jellyfin_items["down"] = True
notices.check_arrivals()
check.equal(told(KID), [], "with Jellyfin not answering nothing is said")
check.that("m-jaws" in waiting_ids(KID), "and not answering is not gone: the ask waits")
jellyfin_items["down"] = False
notices.check_arrivals()
check.equal(told(KID), ["Jaws has audio description now."], "the next pass that can, says it")

# --- describarr not answering: waits for the next pass -------------------------------------

delivered.clear()
ask(KID, "m-brazil")
OUTCOMES[file_of("m-brazil")] = "described"
describarr_up["now"] = False
notices.check_arrivals()
check.equal(told(KID), [], "with describarr not answering nothing is said")
check.that("m-brazil" in waiting_ids(KID), "and the ask waits")
describarr_up["now"] = True
notices.check_arrivals()
check.equal(told(KID), ["Brazil has audio description now."], "the next pass that can, says it")

# --- the hour after Jellyfin said it cannot send -------------------------------------------

delivered.clear()
ask(KID, "m-rocky")
OUTCOMES[file_of("m-rocky")] = "described"
notices._unavailable_until = time.monotonic() + 3600
notices.check_arrivals()
check.equal(told(KID), [], "nothing is tried in the hour after Jellyfin said it cannot send")
check.that("m-rocky" in waiting_ids(KID), "and the ask waits for after it rather than being lost")
notices._unavailable_until = 0.0
notices.check_arrivals()
check.equal(told(KID), ["Rocky has audio description now."], "which says it")

# --- only those who turned notices on --------------------------------------------------------

delivered.clear()
OUTCOMES[file_of("m-alien")] = "described"
asked_about.clear()
notices.check_arrivals()
check.equal(told(QUIET), [], "somebody who has not turned notices on is told nothing")
check.that(not any(path.endswith(file_of("m-alien")) for path in asked_about),
           "and describarr is not asked about what they wait on")
check.equal(waiting(QUIET), [("m-alien", "Alien")], "which waits for them")
notices.set_wanted(QUIET.key, True)
notices.check_arrivals()
check.equal(told(QUIET), ["Alien has audio description now."],
            "turning notices on after asking still tells them")

# --- a server that sends no notices ------------------------------------------------------------

delivered.clear()
ask(KID, "m-tron")
OUTCOMES[file_of("m-tron")] = "described"
config.REQUEST_NOTICES = False
asked_about.clear()
notices.check_arrivals()
check.equal(told(KID), [], "a server that sends no notices tells nothing")
check.equal(asked_about, [], "asks describarr nothing")
check.that("m-tron" in waiting_ids(KID), "and forgets nothing")
config.REQUEST_NOTICES = True
notices.check_arrivals()
check.equal(told(KID), ["Tron has audio description now."], "and the next pass that can, says it")

# --- one failing does not stop the rest ----------------------------------------------------------

delivered.clear()
notices.set_wanted(OTHER.key, True)
ask(KID, "m-broken")
ask(KID, "m-later")
ask(OTHER, "m-other")
OUTCOMES[file_of("m-later")] = "described"
OUTCOMES[file_of("m-other")] = "described"
# After it was asked for: the ask itself looks the item up too.
UNREADABLE.add("m-broken")
try:
    notices.check_arrivals()
except Exception as exc:  # noqa: BLE001 - raising is the defect here.
    check.that(False, f"the pass raised {exc!r} instead of carrying on")
check.equal(told(KID), ["Later has audio description now."],
            "a title that fails does not stop the same account's next one")
check.equal(told(OTHER), ["Other Film has audio description now."],
            "nor the next account's")
check.that("m-broken" in waiting_ids(KID), "and the one that failed is tried again next time")
UNREADABLE.clear()

# --- 30 days ---------------------------------------------------------------------------------------

delivered.clear()
ask(KID, "m-old")
ask(KID, "m-recent")
ask(NEVER, "m-old")
OUTCOMES[file_of("m-old")] = "queued"
OUTCOMES[file_of("m-recent")] = "queued"
days_ago(KID, "m-old", 31)
days_ago(KID, "m-recent", 29)
days_ago(NEVER, "m-old", 31)
asked_about.clear()
notices.check_arrivals()
check.that("m-old" not in waiting_ids(KID), "a title waited on for more than 30 days is forgotten")
check.that(not any(path.endswith(file_of("m-old")) for path in asked_about),
           "without describarr being asked about it")
check.that("m-recent" in waiting_ids(KID), "one waited on for 29 days is not")
check.equal(waiting(NEVER), [],
            "and so is the ask of somebody the pass never looks at, rather than kept for ever")

harness.cleanup()
raise SystemExit(check.report())
