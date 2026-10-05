"""Music past the day's import allowance waits in a queue instead of being refused.

What is pinned here: a list confirmed past the allowance finishes at once and
says what waits; the API lists those rows under `asked` as well, marked as
waiting, for a client from before the queue; the queue can be read and stopped
over the API and on the page, and the page asks for a tick before stopping it;
the pass that empties it resolves accounts through Jellyfin, drops the queue of
an account that is gone, and puts a row back where it was when buskarr cannot
be reached; and buskarr's batch state is asked once per thousand, with an old
buskarr falling back and an unreachable one answering unknown.
"""
import json
import sqlite3
import time
from urllib.parse import unquote

import harness

harness.setup(
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_ALBUM_COST="1", MUSIC_ARTIST_COST="3",
    MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0",
    IMPORT_MUSIC_DAILY_SONGS="24", IMPORT_ALBUM_SONGS="12", JELLYFIN_USER="",
)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, arr, buskarr, config, imports, jellyfin, main,  # noqa: E402
                 media, sessions, store, wants)

check = harness.Check("import queue")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="user-kid", name="kid", is_admin=False)
BY_TOKEN = {"matt-token": MATT, "kid-token": KID}
jellyfin.user_from_token = lambda token: BY_TOKEN[token]
jellyfin.credential_rejected = lambda force=True: False

media._registry = {
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                              ("lib-music",)),
}
media._registry_built_at = time.monotonic()
media._registry_settled = True

CATALOGUE = [{"title": f"Record {n}", "artist": f"Band {n}", "ref": f"a{n}"}
             for n in range(5)]
buskarr.search = lambda q, unit, limit, sources=(): [
    buskarr._result(dict(row, source="deezer"), unit) for row in CATALOGUE
    if f"{row['artist']} {row['title']}".casefold() == q.casefold()
] if unit == "album" else []

asked: list[tuple[str, bool]] = []
#: What the fake buskarr does with an add: "ok", or "down" for unreachable.
buskarr_mode = ["ok"]


def fake_add(unit, hit, by, bulk=False):
    if buskarr_mode[0] == "down":
        return arr.AddResult(False, "buskarr could not be reached.",
                             transient=True)
    asked.append((hit.get("title", ""), bulk))
    return arr.AddResult(True, "Sent to buskarr.", f"job:{hit.get('ref')}",
                         hit.get("title", ""))


buskarr.add = fake_add
#: Kept for the end of the file, which tests the client itself.
REAL_STATES = buskarr.states
buskarr.states = lambda backend_ids: {}

api_app = FastAPI()
api_app.include_router(api.router)
client = TestClient(api_app, raise_server_exceptions=False)
web = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
web.cookies.set(sessions.COOKIE_NAME, sessions.issue("kid-token", KID.id))


def as_(who: str) -> dict:
    return {"X-Emby-Token": who}


def until(import_id: str, state: str, seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        body = client.get(f"/api/v1/import/{import_id}",
                          headers=as_("kid-token")).json()
        if body["state"] == state or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


LIST = "Artist,Album\n" + "".join(f"Band {n},Record {n}\n" for n in range(4))

# --- what the server says it can do ------------------------------------------
caps = client.get("/api/v1/capabilities", headers=as_("kid-token")).json()
check.equal(caps["importList"]["queue"],
            {"supported": True, "songsPerDay": 24},
            "capabilities say music past the allowance waits, and how much a day")
check.equal(caps["importList"]["maxRowsByMedium"]["music"],
            config.IMPORT_MAX_ROWS_MUSIC, "and the music row limit")
check.equal(caps["importList"]["maxRows"], config.IMPORT_MAX_ROWS,
            "while maxRows keeps the number older clients read")

# --- a list longer than the day's allowance ----------------------------------
started = client.post("/api/v1/import", headers=as_("kid-token"),
                      json={"medium": "music", "unit": "album",
                            "text": LIST}).json()
done = until(started["importId"], "done")
check.equal([title for title, _ in asked], ["Record 0", "Record 1"],
            "two albums fit in 24 songs a day and are asked for as the list "
            "is looked up, with nobody confirming anything")
check.equal({row["line"]: row["detail"] for row in done["rows"]},
            {2: "Asked for.", 3: "Asked for.", 4: "Waiting for a later day.",
             5: "Waiting for a later day."},
            "every row says what became of it")
check.that(done["remainingToday"] >= 4,
           "a client from before the queue is given enough to cover every "
           "row, so it does not warn that the rest will be refused")
check.equal((done["importAllowance"]["leftToday"],
             done["importAllowance"]["songsPerDay"]), (0, 24),
            "and the real import allowance is there for a client that reads it")
check.equal(done["report"]["waiting"],
            ["Record 2 by Band 2", "Record 3 by Band 3"],
            "the other two wait, and the report names them")
check.equal(done["report"]["asked"][-2:],
            ["Record 2 by Band 2 (waiting)", "Record 3 by Band 3 (waiting)"],
            "and lists them under asked as well, marked, so a client that "
            "reads only asked does not lose most of a long list")
check.equal(done["report"]["refused"], [], "nothing is refused")

queue = client.get("/api/v1/import/queue", headers=as_("kid-token")).json()
check.equal((queue["waiting"], queue["songs"], queue["days"], queue["leftToday"]),
            (2, 24, 1, 0), "the queue says what waits and how long it will take")
check.that(queue["nextAt"] and queue["nextAt"] > time.time(),
           "and when the allowance next comes back")
check.equal(client.get("/api/v1/import/queue", headers=as_("matt-token"))
            .json()["waiting"], 0, "another account sees only its own queue")

# --- the pages ----------------------------------------------------------------
page = " ".join(web.get("/import").text.split())
check.that("2 music requests from your lists are waiting" in page,
           "the import page says what is waiting")
check.that('action="/import/queue/clear"' in page and "required" in page,
           "with a way to stop it, behind a tick box")
media.owned = lambda *args, **kwargs: jellyfin.Owned()
home = " ".join(web.get("/").text.split())
check.that("2 music requests from your imported lists are waiting" in home,
           "and so does the list of what this account has asked for")
refused = web.post("/import/queue/clear", data={})
check.that("Nothing was dropped" in unquote(refused.headers["location"]),
           "stopping without the tick does nothing, and says so")
check.equal(store.queued_count(KID.key), 2, "so both rows still wait")

# --- the pass that empties it -------------------------------------------------
jellyfin.all_users = lambda: {"matt": MATT.id, "kid": KID.id}
jellyfin.user = lambda name: {"matt": MATT, "kid": KID}[name]
asked.clear()
check.equal(imports.release_queue(), 0,
            "while the allowance is spent, the pass asks for nothing")

with store.db() as conn:
    conn.execute("UPDATE requests SET requested_at = requested_at - 90000")
buskarr_mode[0] = "down"
check.equal(imports.release_queue(), 0, "with buskarr down, nothing goes in")
check.equal([row["label"] for row in store.queued(KID.key)],
            ["Record 2 by Band 2", "Record 3 by Band 3"],
            "and nothing is lost: the row it tried is put back where it was")

buskarr_mode[0] = "ok"
check.equal(imports.release_queue(), 2, "a day later both go in")
check.equal(asked, [("Record 2", True), ("Record 3", True)],
            "in order, and in bulk")
check.equal(store.queued_count(KID.key), 0, "and the queue is empty")
check.equal([r['outcome'] for r in store.import_rows(started['importId'])],
            ['asked'] * 4, 'released rows also stop saying waiting on their import report')
check.equal(wants.allowance(KID, media.MUSIC), 3,
            "none of it spent the allowance for searching")

# A queue whose account Jellyfin no longer has is dropped rather than kept for
# ever, and the other accounts' rows are untouched.
store.queue_rows("user-gone", media.MUSIC, "x", [(2, "Lost", {"unit": "album"})])
store.queue_rows(KID.key, media.MUSIC, "y", [(2, "Record 4 by Band 4", {
    "itemKey": "bk:album:deezer:a4", "unit": "album", "title": "Record 4",
    "artist": "Band 4", "ref": "a4", "source": "deezer"})])
with store.db() as conn:
    conn.execute("UPDATE requests SET requested_at = requested_at - 90000")
check.equal(imports.release_queue(), 1, "the living account's row goes in")
check.equal(store.queue_owners(), [], "and the departed account's queue is gone")

# --- a restart between asking for a row and taking it out of the queue ------
store.queue_rows(KID.key, media.MUSIC, "r", [(2, "Record 9 by Band 9", {
    "itemKey": "bk:album:deezer:a9", "unit": "album", "title": "Record 9",
    "artist": "Band 9", "ref": "a9", "source": "deezer"})])
with store.db() as conn:
    conn.execute("UPDATE requests SET requested_at = requested_at - 90000")
waiting_during_ask: list[int] = []


def watching_add(unit, hit, by, bulk=False):
    waiting_during_ask.append(store.queued_count(KID.key))
    return fake_add(unit, hit, by, bulk)


def container_stops(row_id, *args):
    raise RuntimeError("the container stopped here")


real_unqueue = store.unqueue
store.unqueue = container_stops
buskarr.add = watching_add
asked.clear()
try:
    imports._release_for(KID)
except RuntimeError:
    pass
store.unqueue = real_unqueue
buskarr.add = fake_add
check.equal(waiting_during_ask, [1],
            "a row being asked for is still in the queue, so a list confirmed "
            "meanwhile waits behind it instead of going first")
check.equal(store.queued_count(KID.key), 1,
            "a restart after the ask leaves the row in the queue, not lost")
check.equal(imports._release_for(KID), 1, "the next pass takes it")
check.equal(len(asked), 1,
            "without asking buskarr a second time: the ledger already had it")
check.equal(store.queued_count(KID.key), 0, "and the queue is empty")

# --- stopping it ------------------------------------------------------------
store.queue_rows(KID.key, media.MUSIC, "z", [(2, "One", {"unit": "album"}),
                                              (3, "Two", {"unit": "album"})])
stopped = web.post("/import/queue/clear", data={"confirm": "yes"})
check.that("Stopped. 2 waiting rows" in unquote(stopped.headers["location"]),
           "with the tick, stopping drops both and says how many")
check.equal(store.queued_count(KID.key), 0, "and nothing is left")
store.queue_rows(KID.key, media.MUSIC, "z", [(2, "One", {"unit": "album"})])
check.equal(client.delete("/api/v1/import/queue", headers=as_("kid-token"))
            .json()["removed"], 1, "the API can stop it too")

# --- buskarr's batch state --------------------------------------------------
seen: list[list[str]] = []


def transport(status: int, fail: bool = False):
    def handle(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("down", request=request)
        refs = json.loads(request.content)["references"]
        seen.append(refs)
        return httpx.Response(status, json={"states": {
            ref: {"state": "have"} for ref in refs if ref.endswith("1")}})
    return httpx.MockTransport(handle)


def with_transport(mock):
    return lambda: httpx.Client(base_url="http://buskarr.invalid/api/v1",
                                transport=mock)


buskarr._client = with_transport(transport(200))
refs = [f"want:{n}" for n in range(2500)]
answer = REAL_STATES(refs)
check.equal([len(page) for page in seen], [1000, 1000, 500],
            "two thousand five hundred references go in three calls")
check.equal(len(answer), 250, "and the answers from all three come back")
buskarr._client = with_transport(transport(404))
check.equal(REAL_STATES(["want:1"]), None,
            "a buskarr from before the endpoint answers None, to fall back on")
buskarr._client = with_transport(transport(200, fail=True))
check.equal(REAL_STATES(["want:1"]), {},
            "an unreachable one answers nothing known, rather than a probe "
            "per row that would each wait out the same timeout")

# If queue removal fails, its source row must still say waiting. Retrying can
# then complete both changes together instead of leaving contradictory history.
store.put_import('atomic', KID.key, 'music', 'done', 1, {'unit': 'track'})
store.put_import_row('atomic', {'line': 2, 'state': 'matched', 'title': 'Song'}, 'waiting')
store.queue_rows(KID.key, 'music', 'atomic', [(2, 'Song', {'itemKey': 'atomic-key'})])
queued_id = next(r['id'] for r in store.queued(KID.key) if r['import_id'] == 'atomic')
with store.db() as conn:
    conn.execute("CREATE TRIGGER fail_unqueue BEFORE DELETE ON import_queue "
                 "WHEN OLD.import_id='atomic' BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
check.raises(sqlite3.IntegrityError, lambda: store.unqueue(queued_id, imports.ASKED),
             'an interrupted queue completion fails atomically')
check.equal(store.import_rows('atomic')[0]['outcome'], 'waiting', 'the report change is rolled back too')
check.that(store.is_queued(queued_id), 'the interrupted entry remains available for retry')
with store.db() as conn:
    conn.execute('DROP TRIGGER fail_unqueue')
store.unqueue(queued_id, imports.ASKED)
check.equal((store.is_queued(queued_id), store.import_rows('atomic')[0]['outcome']),
            (False, imports.ASKED), 'retry completes the queue and report together')

harness.cleanup()
raise SystemExit(check.report())
