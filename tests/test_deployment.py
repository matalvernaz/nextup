"""The ways a deployment goes wrong quietly.

Each of these is a misconfiguration that leaves a container reporting healthy
while the thing it exists to do does not work. None of them announce
themselves, which is the whole reason they are worth a test.
"""
import harness

harness.setup()

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, jellyfin, main, selfcheck  # noqa: E402

check = harness.Check("deployment")


# --- a number that is not a number ------------------------------------------
# Reading RADARR_QUALITY_PROFILE_ID=six as the default of 0 disqualifies the
# backend outright, because Arr.configured wants a profile above zero. Films
# then stop being offered at all, on a container that starts, reports healthy
# and logs only that a backend is not configured -- so the medium vanishes from
# every client with nothing anywhere to say why.
check.equal(config._int("NOT_SET_ANYWHERE", 7), 7, "an unset variable takes the default")

import os  # noqa: E402

os.environ["A_TEST_NUMBER"] = ""
check.equal(config._int("A_TEST_NUMBER", 7), 7, "an empty variable takes the default")
os.environ["A_TEST_NUMBER"] = "12"
check.equal(config._int("A_TEST_NUMBER", 7), 12, "a real number is read")
os.environ["A_TEST_NUMBER"] = "six"
check.raises(RuntimeError, lambda: config._int("A_TEST_NUMBER", 7),
             "a value that will not parse raises rather than defaulting")
del os.environ["A_TEST_NUMBER"]


# --- a credential Jellyfin has stopped accepting ----------------------------
# Two failures pull in opposite directions here.
#
# The first ran for days on the service this one is modelled on: every route
# failing because the API key had been retired, with the container reporting
# healthy throughout, because nothing the health check touched had stopped
# working. That is why the state is reported at all.
#
# The second is what reporting it through `/healthz` cost. Traefik's Docker
# provider drops an unhealthy container -- verified against the live proxy on
# 2026-09-05: same container, same labels, healthcheck `exit 0` answers 200
# through it and `exit 1` answers 404, with nothing in any log. The compose
# healthcheck hits `/healthz`, so a retired key deleted every router including
# `/setup`, which is the page that issues a new key. The fault made itself
# unrepairable from outside.
client = TestClient(main.app, raise_server_exceptions=False)

jellyfin.credential_rejected = lambda force=True: True
check.equal(client.get("/healthz").status_code, 200,
            "liveness stays 200 while the process answers, whatever Jellyfin "
            "thinks of this service's key")

ready = client.get("/readyz")
check.equal(ready.status_code, 503, "readiness says so instead")
check.equal(ready.json()["reason"], "jellyfin_credential_rejected",
            "naming the state a person has to act on")
check.equal(ready.json()["repair"], "/setup",
            "and where they act on it, because a reason with no address is a "
            "puzzle rather than an instruction")

# And the pages say it, which is the only channel somebody sees without asking:
# a refused key leaves them rendering with an empty library behind them, which
# reads as "there is nothing here" rather than as a fault.
page = client.get("/signin")
check.that("refusing this service" in page.text,
           "the pages carry the fault where a person will meet it")
check.that('role="alert"' in page.text,
           "as an alert rather than in the polite region, because it "
           "displaces whatever else was being announced")

jellyfin.credential_rejected = lambda force=True: False
check.equal(client.get("/readyz").status_code, 200,
            "and a working key is ready")
check.that("refusing this service" not in client.get("/signin").text,
           "with no banner when there is nothing wrong")


# --- the route clients actually look for ------------------------------------
BASE = "https://jellyfin.example.com/nextup"
WANTED = f"{BASE}/api/v1/info"


def answering(handler):
    """Point the check at a fake origin for the length of one call."""
    real = httpx.get

    def fake(url, **kwargs):
        check.equal(url, WANTED, "the check asks the address clients use")
        return handler(httpx.Request("GET", url))

    httpx.get = fake
    try:
        return selfcheck.check(BASE)
    finally:
        httpx.get = real


check.that(
    answering(lambda req: httpx.Response(
        200, json={"service": "nextup", "protocol": 1}, request=req)) is None,
    "a route that answers is no news")

missing = answering(lambda req: httpx.Response(404, text="Not Found", request=req))
check.that(missing is not None and "404" in missing and "Jellyfin origin" in missing,
           "a missing proxy rule is named, not merely noticed")

# A 200 from somebody else is what a catch-all router or a captive portal looks
# like, and it must not read as a working route.
imposter = answering(lambda req: httpx.Response(
    200, json={"service": "jellyfin"}, request=req))
check.that(imposter is not None and "'jellyfin'" in imposter
           and "'nextup'" in imposter,
           "somebody else answering 200 is not this service answering, and "
           "both names are given because with two routes watched the expected "
           "one differs per route")

challenged = answering(lambda req: httpx.Response(401, text="", request=req))
check.that(challenged is not None and "401" in challenged,
           "any other status is reported with its number")


def refuse(req):
    raise httpx.ConnectError("nothing listening", request=req)


unreachable = answering(refuse)
check.that(unreachable is not None and "could not be reached" in unreachable,
           "an address nothing listens on is reported")

# Unset is the ordinary case for an install serving only the browser pages,
# and it must cost that install nothing at all.
import threading  # noqa: E402

config.PUBLIC_URLS = []
selfcheck.watch()
check.that(not any(t.name == "same-origin-check" for t in threading.enumerate()),
           "no PUBLIC_URL means no thread and no requests")

# --- more than one route, because this deployment answers the old prefix too --
#
# The second route is the one whose failure has no symptom at all: the books
# row is simply absent on every client, with nothing in any log.
check.equal(selfcheck.expected_service("https://jf.example.com/nextup"),
            "nextup", "the ordinary prefix expects this service's name")
check.equal(selfcheck.expected_service("https://jf.example.com/nextread/"),
            "nextread",
            "and the old audiobook prefix expects the old name, which is what "
            "it really answers with -- comparing it against this service's "
            "name would report a working route as somebody else's")
check.equal(selfcheck.expected_service("https://nextup.example.com"),
            "nextup", "a bare host is this service")

harness.cleanup()
raise SystemExit(check.report())
