"""The bbc-ui client turns every failure into BbcUnavailable or BbcAmbiguous.

httpx is stubbed at the module level so nothing leaves the process: each test
arms the next get/post with a crafted Response or an exception, then checks what
the client makes of it. The distinction that matters is a write whose outcome
could have landed (BbcAmbiguous) versus one that certainly did not
(BbcUnavailable), because the wants chunk refunds on one and reconciles on the
other.
"""
import httpx

import harness

harness.setup(BBC_UI_URL="http://bbc-ui.invalid:8080", BBC_UI_TOKEN="s3cret")

from app import store  # noqa: E402
from app.bbc import client, contract  # noqa: E402

store.init()  # so config's settings-store fallback has a database to read

check = harness.Check("bbc client")

_last: dict = {}
_queue: list = []
_calls = [0]


def _request(method: str) -> httpx.Request:
    return httpx.Request(method, "http://bbc-ui.invalid:8080/x")


def ok(status: int, *, json=None, text: str = "") -> httpx.Response:
    if json is not None:
        return httpx.Response(status, json=json, request=_request("GET"))
    return httpx.Response(status, text=text, request=_request("GET"))


def _fake(url, **kw):
    _calls[0] += 1
    _last.clear()
    _last["url"] = url
    _last["params"] = kw.get("params")
    _last["headers"] = kw.get("headers") or {}
    _last["json"] = kw.get("json")
    if not _queue:
        raise AssertionError("the client called bbc-ui when it should not have")
    outcome = _queue.pop(0)
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def arm(*outcomes) -> None:
    """Arm the next call(s); each client call consumes one outcome, in order."""
    _queue[:] = outcomes
    _calls[0] = 0
    _last.clear()


def never_called() -> None:
    """Expect no call at all: an empty queue turns any call into a loud failure."""
    _queue.clear()
    _calls[0] = 0
    _last.clear()


httpx.get = _fake
httpx.post = _fake

PROG = {"pid": "b006m8z1", "title": "Casualty", "synopsis": "", "image_url": "",
        "category": "Drama", "episode_count": 3, "described": "available"}
BAD_PROG = {"pid": "", "title": "x", "episode_count": 1, "described": "available"}
STATUS = {"pid": "m002xv0m", "attempt_key": "k", "state": "downloading",
          "unavailable": None, "message": ""}
RESULT = {"pid": "m002xv0m", "outcome": "accepted", "attempt_key": "m002xv0m:k",
          "unavailable": None, "message": ""}


def reply(**rows) -> dict:
    return {"schema_version": contract.SCHEMA_VERSION, **rows}


check.that(issubclass(client.BbcAmbiguous, client.BbcUnavailable),
           "BbcAmbiguous is a BbcUnavailable, so the safe default covers it")

# --- health: the one unauthenticated call -----------------------------------

arm(ok(200, json=reply(ok=True, tunnel_ok=False)))
h = client.health()
check.that(h.ok and not h.tunnel_ok, "health parses")
check.that(client.TOKEN_HEADER not in _last["headers"],
           "health sends no shared secret")

arm(ok(200, json={"schema_version": 999, "ok": True, "tunnel_ok": True}))
check.raises(client.BbcUnavailable, client.health, "a skewed health reply is unhealthy")
arm(ok(200, text="<html>not json</html>"))
check.raises(client.BbcUnavailable, client.health, "non-JSON health is unhealthy")
arm(ok(503, text="down"))
check.raises(client.BbcUnavailable, client.health, "a 503 health is unhealthy")
arm(httpx.ConnectError("refused"))
check.raises(client.BbcUnavailable, client.health, "an unreachable health is unhealthy")

# --- catalogue: a bad row is skipped, an all-bad reply is an outage ----------

arm(ok(200, json=reply(programmes=[PROG, BAD_PROG])))
progs = client.programmes("new")
check.equal(len(progs), 1, "a bad programme row is skipped, the good one kept")
check.equal(_last["headers"].get(client.TOKEN_HEADER), "s3cret",
            "catalogue sends the shared secret")
check.equal(_last["params"], {"view": "new"}, "catalogue params carry the view")

arm(ok(200, json=reply(programmes=[BAD_PROG])))
check.raises(client.BbcUnavailable, lambda: client.programmes("new"),
             "a reply whose every row is bad is an outage, not an empty shelf")
arm(ok(200, json=reply(programmes=[])))
check.equal(client.programmes("new"), [], "a genuinely empty shelf is empty")
arm(ok(200, json=reply(episodes=[])))
check.equal(client.episodes("b006m8z1"), [], "episodes of a programme, empty here")

# --- status: a strict partition, pids passed through ------------------------

arm(ok(200, json=reply(statuses=[STATUS], not_found=["m2"])))
part = client.status(["m002xv0m", "m2", "m3"])
check.equal(part.unanswered, frozenset({"m3"}), "a pid in neither list is unanswered")
check.equal(_last["params"], {"pids": "m002xv0m,m2,m3"}, "status asks about the pids")

# --- request: ambiguity is classified by how it failed ----------------------

arm(ok(200, json=reply(results=[RESULT])))
results = client.request([("m002xv0m", "m002xv0m:k", contract.Reason.USER_REQUEST)])
check.equal(len(results), 1, "a request reply parses")
check.equal(_last["json"]["requests"][0]["attempt_key"], "m002xv0m:k",
            "the request body carries a per-pid attempt key")
check.equal(_last["headers"].get(client.TOKEN_HEADER), "s3cret",
            "request sends the shared secret")


def _request_one(outcome):
    arm(outcome)
    return lambda: client.request(
        [("m002xv0m", "m002xv0m:k", contract.Reason.USER_REQUEST)])


check.raises(client.BbcAmbiguous,
             _request_one(ok(200, json={"schema_version": 999, "results": []})),
             "a skewed 2xx reply to a POST may have landed")
check.raises(client.BbcAmbiguous,
             _request_one(ok(200, text="<html>")),
             "a non-JSON 2xx reply to a POST may have landed")
check.raises(client.BbcAmbiguous, _request_one(ok(502, text="bad gateway")),
             "a 5xx to a POST may have landed")
check.raises(client.BbcAmbiguous, _request_one(httpx.ReadTimeout("slow")),
             "a read timeout on a POST may have landed")
check.raises(client.BbcAmbiguous, _request_one(httpx.RemoteProtocolError("drop")),
             "a dropped connection on a POST may have landed")

# These did NOT land: a clean refusal, or nothing sent.
check.raises(client.BbcUnavailable, _request_one(ok(409, text="nope")),
             "a 4xx to a POST is a clean refusal")
# 409 must be the plain outage, never the ambiguous subclass.
arm(ok(409, text="nope"))
try:
    client.request([("m002xv0m", "m002xv0m:k", contract.Reason.USER_REQUEST)])
except client.BbcAmbiguous:
    check.that(False, "a 409 was wrongly treated as ambiguous")
except client.BbcUnavailable:
    check.that(True, "a 409 is a plain outage, not ambiguous")
check.raises(client.BbcUnavailable, _request_one(ok(401, text="no token")),
             "a 401 to a POST is a clean refusal")
check.raises(client.BbcUnavailable, _request_one(httpx.ConnectError("refused")),
             "a connect error sent nothing")
check.raises(client.BbcUnavailable, _request_one(httpx.ConnectTimeout("slow")),
             "a connect timeout sent nothing")

# --- a request reply must cover exactly the batch, or it reconciles ----------

check.raises(client.BbcAmbiguous, _request_one(ok(200, json=reply(results=[]))),
             "a request reply that drops the pid is ambiguous")
check.raises(client.BbcAmbiguous,
             _request_one(ok(200, json=reply(results=[RESULT, RESULT]))),
             "a duplicate result is ambiguous")
check.raises(client.BbcAmbiguous,
             _request_one(ok(200, json=reply(
                 results=[{**RESULT, "pid": "elsewhere"}]))),
             "an unsolicited result is ambiguous")

# --- episodes: a bad row is skipped, an all-bad reply is an outage -----------

_GOOD_EP = {"pid": "e1", "programme_pid": "b", "title": "Ep", "synopsis": "",
            "image_url": "", "category": "", "series_number": None,
            "episode_number": None, "described": "available", "available": True,
            "unavailable": None}
_BAD_EP = {"pid": "", "programme_pid": "b", "title": "x",
           "described": "available", "available": True, "unavailable": None}
arm(ok(200, json=reply(episodes=[_GOOD_EP, _BAD_EP])))
check.equal(len(client.episodes("b")), 1, "episodes skips a bad row, keeps the good")
arm(ok(200, json=reply(episodes=[_BAD_EP])))
check.raises(client.BbcUnavailable, lambda: client.episodes("b"),
             "episodes all-bad is an outage, not an empty list")

# --- status: an empty ask makes no call; a long list is chunked -------------

never_called()
empty = client.status([])
check.that(not empty.statuses and not empty.not_found and not empty.unanswered,
           "status of nothing makes no call and is empty")
many = [f"p{i}" for i in range(client._STATUS_BATCH + 1)]
arm(ok(200, json=reply(statuses=[], not_found=[])),
    ok(200, json=reply(statuses=[], not_found=[])))
part = client.status(many)
check.equal(_calls[0], 2, "a 51-pid status asks in two batches")
check.equal(len(part.unanswered), len(many), "every pid here comes back unanswered")

# --- cancel -----------------------------------------------------------------

arm(ok(200, json=reply(cancelled=["m1", "m2"])))
cancelled = client.cancel(["m1", "m2"])
check.equal(cancelled, frozenset({"m1", "m2"}), "cancel returns the dropped pids")
check.equal(_last["json"]["pids"], ["m1", "m2"], "cancel names the pids")
check.equal(_last["headers"].get(client.TOKEN_HEADER), "s3cret",
            "cancel sends the shared secret")
arm(ok(200, json=reply()))  # no 'cancelled' list = an ack we cannot read
check.raises(client.BbcAmbiguous, lambda: client.cancel(["m1"]),
             "an unreadable cancel ack is ambiguous, not success")

# --- fail closed: no token means no call, as an outage ----------------------

import os  # noqa: E402

os.environ.pop("BBC_UI_TOKEN", None)
for name, call in (
    ("request", lambda: client.request(
        [("m1", "m1:k", contract.Reason.USER_REQUEST)])),
    ("status", lambda: client.status(["m1"])),
    ("catalogue", lambda: client.programmes("new")),
    ("cancel", lambda: client.cancel(["m1"])),
):
    never_called()
    check.raises(client.BbcUnavailable, call,
                 f"{name} with no shared secret is a fail-closed outage")

raise SystemExit(check.report())
