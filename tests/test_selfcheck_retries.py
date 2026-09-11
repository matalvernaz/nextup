"""A route that is merely late must not be reported as a route that is missing.

`selfcheck` writes the one error line in this service that means "a feature is
silently absent": no client will ever find the same-origin route, and nothing
else anywhere says so. It probed once, sixty seconds after start.

Sixty seconds is not always enough. Measured 2026-09-10: a deploy logged both
routes as 404 at exactly the first delay and both answered minutes later, so
every slow proxy registration wrote that line. An alarm that fires on healthy
deploys is one nobody reads on the day it is right.
"""
import harness

harness.setup()

from app import selfcheck  # noqa: E402

check = harness.Check("selfcheck retries")

URL = "https://jellyfin.example.com/nextup"
MISSING = f"{URL}/api/v1/info answers 404. The proxy rule is missing."


def answers(*sequence):
    """Stub `check` with one verdict per call, and count the calls."""
    calls = []

    def stub(base_url):
        calls.append(base_url)
        return sequence[min(len(calls) - 1, len(sequence) - 1)]

    selfcheck.check = stub
    selfcheck.time.sleep = lambda _seconds: None
    return calls


print("=== a route late to register is not an error ===")
calls = answers(MISSING, MISSING, None)
problem, taken = selfcheck.settled(URL, attempts=3)
check.equal(problem, None, "two 404s then an answer settles as no problem")
check.equal(taken, 3, "and reports how many probes it took")
check.equal(len(calls), 3, "without probing more than it was allowed")

print("\n=== a route that answers first time costs one probe ===")
calls = answers(None)
problem, taken = selfcheck.settled(URL, attempts=3)
check.equal((problem, taken), (None, 1), "no retry when there is nothing wrong")
check.equal(len(calls), 1, "and no extra request")

print("\n=== a genuinely missing rule is still reported ===")
calls = answers(MISSING)
problem, taken = selfcheck.settled(URL, attempts=3)
check.equal(problem, MISSING, "every attempt failing is a real problem")
check.equal(len(calls), 3, "after trying the whole allowance")

print("\n=== the default is one probe, so later passes do not wait ===")
calls = answers(MISSING, None)
problem, _ = selfcheck.settled(URL)
check.equal(problem, MISSING, "one attempt by default")
check.equal(len(calls), 1, "a settled deployment is not re-probed for nothing")

print("\n=== and the loop only spends its retries on the first pass ===")
logged = []
selfcheck.log.error = lambda fmt, *a: logged.append(("error", fmt % a))
selfcheck.log.info = lambda fmt, *a: logged.append(("info", fmt % a))
attempted = []


def counting(base_url, attempts=1, gap=0):
    attempted.append(attempts)
    return (None, 1)


selfcheck.settled = counting
selfcheck.INTERVAL_SECONDS = 0


def stop_after_two(_seconds):
    if len(attempted) >= 2:
        raise KeyboardInterrupt


selfcheck.time.sleep = stop_after_two
try:
    selfcheck._loop([URL])
except KeyboardInterrupt:
    pass
check.equal(attempted, [selfcheck.FIRST_PASS_ATTEMPTS, 1],
            "the first pass retries, the second does not")
check.that(not any(kind == "error" for kind, _ in logged),
           f"and a route that answered logged no error: {logged}")

harness.cleanup()
raise SystemExit(check.report())
