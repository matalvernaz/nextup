"""The one endpoint that takes a password, and what stops it being farmed.

`POST /signin` is unauthenticated by necessity and, in the shipped deployment,
a published port with nothing in front of it. Unlimited, it is somewhere to
test stolen credentials against a Jellyfin that answers honestly, and a way to
run a real account into Jellyfin's own lockout policy from outside.
"""
import harness

harness.setup()

from fastapi.testclient import TestClient  # noqa: E402

from app import jellyfin, main, throttle  # noqa: E402

check = harness.Check("signin throttle")
# Redirects are not followed: a successful sign-in redirects to the ordinary
# page, which resolves its viewer through a Jellyfin that is not there in a
# test. The 303 is the thing being checked.
client = TestClient(main.app, raise_server_exceptions=False,
                    follow_redirects=False)

jellyfin.credential_rejected = lambda force=True: False


def refuse_everything(username, password, device=""):
    raise jellyfin.TokenRejected("Jellyfin did not accept that password.")


def accept(username, password, device=""):
    return "a-token", jellyfin.User(id="u1", name=username)


jellyfin.authenticate = refuse_everything


def attempt(username="someone", password="wrong", address="203.0.113.7"):
    return client.post("/signin", data={"username": username,
                                        "password": password},
                       headers={"X-Forwarded-For": address})


# --- the limit bites, and says how long ------------------------------------
throttle.forget_everything()
codes = [attempt().status_code for _ in range(throttle.MAX_ATTEMPTS)]
check.equal(set(codes), {401}, "every wrong password inside the limit is a 401")

refused = attempt()
check.equal(refused.status_code, 429, "and the next one is refused outright")
check.that(refused.headers.get("retry-after", "").isdigit(),
           "with Retry-After in seconds, for anything reading the response")
check.that("Try again in about" in refused.text,
           "and in words on the page, because the person reading it is not "
           "reading headers")

# --- one household mistyping must not lock out the household ----------------
#
# Counted per address AND per username, so the refusal above should not have
# touched a different person from a different place.
check.equal(attempt(username="somebody-else", address="203.0.113.9").status_code,
            401, "a different account from a different address is unaffected")

# --- either key alone is enough ---------------------------------------------
# Per address only would miss a slow attempt on one account from many places;
# per username only would miss one address spraying many accounts.
throttle.forget_everything()
for i in range(throttle.MAX_ATTEMPTS):
    attempt(username="target", address=f"198.51.100.{i}")
check.equal(attempt(username="target", address="198.51.100.200").status_code,
            429, "one account attacked from many addresses is still stopped")

throttle.forget_everything()
for i in range(throttle.MAX_ATTEMPTS):
    attempt(username=f"person{i}", address="198.51.100.5")
check.equal(attempt(username="someone-new", address="198.51.100.5").status_code,
            429, "and one address spraying many accounts likewise")

# --- getting it right clears it --------------------------------------------
throttle.forget_everything()
for _ in range(throttle.MAX_ATTEMPTS - 1):
    attempt()
jellyfin.authenticate = accept
ok = attempt(password="right")
check.equal(ok.status_code, 303, "the right password signs in")
jellyfin.authenticate = refuse_everything
check.equal(attempt().status_code, 401,
            "and clears what was counted, so somebody who mistypes four times "
            "and then succeeds is carrying nothing afterwards")

# --- an unreachable Jellyfin is not a failed attempt ------------------------
#
# A server that is down says nothing about whether the password was right, and
# counting it would lock the household out of a service that is about to work
# again on its own.
def unavailable(username, password, device=""):
    raise jellyfin.JellyfinUnavailable("connection refused")


throttle.forget_everything()
jellyfin.authenticate = unavailable
for _ in range(throttle.MAX_ATTEMPTS + 2):
    check.equal(attempt().status_code, 503, "an outage is reported as an outage")
    break
for _ in range(throttle.MAX_ATTEMPTS + 2):
    attempt()
jellyfin.authenticate = refuse_everything
check.equal(attempt().status_code, 401,
            "and none of it counted against anybody")

harness.cleanup()
raise SystemExit(check.report())
