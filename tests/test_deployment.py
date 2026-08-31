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
# The failure this covers ran for days on the service this one is modelled on:
# every route failing because the API key had been retired, with the container
# reporting healthy throughout, because nothing the health check touched had
# stopped working.
client = TestClient(main.app, raise_server_exceptions=False)

jellyfin.credential_rejected = lambda: False
check.equal(client.get("/healthz").status_code, 200, "healthy when the key works")

jellyfin.credential_rejected = lambda: True
check.equal(client.get("/healthz").status_code, 503,
            "unhealthy when Jellyfin refuses this service's key")
jellyfin.credential_rejected = lambda: False


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
check.that(imposter is not None and "not this service" in imposter,
           "somebody else answering 200 is not this service answering")

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

config.PUBLIC_URL = ""
selfcheck.watch()
check.that(not any(t.name == "same-origin-check" for t in threading.enumerate()),
           "no PUBLIC_URL means no thread and no requests")

harness.cleanup()
raise SystemExit(check.report())
