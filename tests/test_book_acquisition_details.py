"""Optional book explanations never decide arrival or hold request lists behind Listenarr."""
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import harness

database = harness.use("book-acquisition-details")
harness.discard(database)

from app import api, compat_nextread, config, jellyfin, listenarr, media
from app.books import acquisition, adapter, shelves, stamp, store, wants

store.init()
config.LISTENARR_URL = "http://listenarr.invalid"
user = jellyfin.User(id="requester", name="requester")
other = jellyfin.User(id="other", name="other")
store.record_request(user.key, "BLOCKED", "Requested lessons")
store.record_request(user.key, "FUTURE", "Announced book")
store.record_request(user.key, "IMPORTED", "Imported copy, awaiting library scan")
store.record_request(other.key, "PRIVATE", "Another account's request")
stamp.soon = lambda rows: None
listenarr.unmonitor_by_asin = lambda asin: True
shelves.owned_index = lambda account: (set(), {})
calls = []


def respond(request):
    calls.append(request.url.path)
    if request.url.path == "/api/v1/library":
        return httpx.Response(200, json=[
            {"id": 1, "asin": "BLOCKED", "publishedDate": "2010-04-01"},
            {"id": 2, "asin": "FUTURE", "publishedDate": "2200-01-01"},
            {"id": 3, "asin": "PRIVATE", "publishedDate": "2200-01-01"},
            {"id": 4, "asin": "IMPORTED", "fileCount": 5},
        ])
    if request.url.path == "/api/v1/downloads":
        return httpx.Response(200, json=[{
            "audiobookId": 1, "status": "ImportBlocked", "startedAt": "2026-11-19T07:20:40",
            "importBlockReason": "/private/path?token=must-not-leak",
        }, {"audiobookId": 4, "status": "ImportBlocked", "startedAt": "2026-09-01T00:00:00"}])
    return httpx.Response(404)


def fake_client(handler):
    def streamed(request):
        response = handler(request)
        if response.is_stream_consumed:
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=httpx.ByteStream(response.content))
        return response
    return lambda address: httpx.Client(base_url=address, transport=httpx.MockTransport(streamed))


def finish_refresh():
    worker = acquisition.warm()
    if worker:
        worker.join(timeout=2)
        assert not worker.is_alive(), "the fake backend must finish without leaving a refresh behind"


# The core is also used by search and the arrival-stamping sweep. Neither uses details.
listenarr._client = lambda: httpx.Client(
    base_url=config.LISTENARR_URL, transport=httpx.MockTransport(respond))
plain_rows = wants.states(user.key, (set(), {}))
assert not calls, "search and arrival sweeps must not fetch optional acquisition details"
assert all("detail" not in row for row in plain_rows)

acquisition._client = fake_client(respond)
finish_refresh()
rows = {row["asin"]: row for row in acquisition.enrich(plain_rows)}
assert "could not be added" in rows["BLOCKED"].get("detail", ""), rows["BLOCKED"]
assert "release date" in rows["FUTURE"].get("detail", ""), rows["FUTURE"]
assert "appear in the library" in rows["IMPORTED"].get("detail", ""), rows["IMPORTED"]
assert rows["IMPORTED"]["state"] != wants.IN_LIBRARY, "import records cannot prove a Jellyfin arrival"
assert rows["BLOCKED"]["state"] == wants.ON_ITS_WAY, "keep the wire states for older clients"
assert "PRIVATE" not in rows
assert "private/path" not in str(rows) and "must-not-leak" not in str(rows)

# Both actual API boundaries opt in; adapter/search/background reads stay plain.
api.wants.states = lambda *args, **kwargs: adapter.states(user)
media.available = lambda: {}
shared = {row["itemKey"]: row for row in api.get_requests(user=user, held_asks=False)["requests"]}
assert shared["BLOCKED"]["detail"] == rows["BLOCKED"]["detail"]
shelves.result = lambda *args, **kwargs: {"own": [], "discover": []}
shelves.unowned_suggestions = lambda data, owned: data
legacy = {row["asin"]: row for row in compat_nextread.get_shelves(user=user, held_asks=False)["requests"]}
assert legacy["BLOCKED"]["detail"] == rows["BLOCKED"]["detail"]
assert len(calls) == 2, "two bulk reads, reused across response boundaries"

# A blocked import cannot hide an arrival independently confirmed by Jellyfin.
arrived = {row["asin"]: row for row in acquisition.enrich(
    wants.states(user.key, ({"BLOCKED"}, {})))}
assert arrived["BLOCKED"]["state"] == wants.IN_LIBRARY
assert not arrived["BLOCKED"].get("detail"), arrived["BLOCKED"]
assert acquisition.enrich([{"asin": "FUTURE", "state": "waiting_for_approval"}]) == [
    {"asin": "FUTURE", "state": "waiting_for_approval"}]

# A cold cache and an unresponsive backend cannot block even the first list.
entered, release = threading.Event(), threading.Event()


def slow(request):
    entered.set()
    assert release.wait(timeout=2), "test backend was never released"
    return respond(request)


acquisition._cache = None
acquisition._client = fake_client(slow)
try:
    start = time.monotonic()
    cold = api.get_requests(user=user, held_asks=False)["requests"]
    assert time.monotonic() - start < 0.5
    assert cold and all("detail" not in row for row in cold)
    assert entered.wait(timeout=1)
    worker = acquisition._refreshing
    assert worker is not None
    start = time.monotonic()
    another = api.get_requests(user=user, held_asks=False)["requests"]
    assert time.monotonic() - start < 0.5, "a second list waited behind the refresh"
    assert another == cold and acquisition._refreshing is worker
    # Expired details remain readable while a single refresh runs.
    acquisition._cache = (config.LISTENARR_URL, 0, {"FUTURE": "Previously verified explanation"})
    assert acquisition.details({"FUTURE"}) == {"FUTURE": "Previously verified explanation"}
finally:
    release.set()
    worker = acquisition._refreshing
    if worker:
        worker.join(timeout=2)
        assert not worker.is_alive()

# A backend failure changes neither request states nor list availability.
acquisition._cache = None
acquisition._client = fake_client(lambda request: httpx.Response(503))
finish_refresh()
unsettled = acquisition.enrich(wants.states(user.key, None))
assert any(row["asin"] == "FUTURE" for row in unsettled)
assert all(not row.get("detail") for row in unsettled)

# A stream which keeps sending bytes still hits the overall read budget.
class Trickle(httpx.SyncByteStream):
    def __iter__(self):
        for _ in range(50):
            time.sleep(0.01)
            yield b" "


acquisition._cache = None
acquisition._BUDGET_SECONDS = 0.025
acquisition._client = fake_client(lambda request: httpx.Response(200, stream=Trickle()))
started = time.monotonic()
finish_refresh()
assert time.monotonic() - started < 0.3, "inactivity timeouts alone do not stop a trickle"
assert acquisition.details({"FUTURE"}) == {}

# Oversized JSON is rejected before parsing rather than consuming unlimited memory.
acquisition._cache = None
acquisition._BUDGET_SECONDS = 5
acquisition._MAX_BYTES = 8
oversized_reads = []


def oversized(request):
    oversized_reads.append(request.url.path)
    return httpx.Response(200, content=b"[123456789]")


acquisition._client = fake_client(oversized)
finish_refresh()
assert acquisition.details({"FUTURE"}) == {}
assert oversized_reads == ["/api/v1/library"], "stop before reading the second endpoint or parsing an oversized body"

harness.discard(database)
print("book acquisition details: ok")
