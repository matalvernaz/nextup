"""An unavailable marketplace cannot establish that a book has no neighbours."""
import harness

harness.setup(AUDIBLE_REGIONS="ca,us")

import httpx

from app.books import audible, store

check = harness.Check("book similarity outages")
store.init()
client_type = httpx.Client
replies = {"ca.invalid": 503, "us.invalid": {"similar_products": []}}
calls = []
audible._base = lambda region: f"https://{region}.invalid/1.0/catalog"


def transport(request):
    calls.append(request.url.host)
    reply = replies[request.url.host]
    if isinstance(reply, int):
        return httpx.Response(reply)
    return httpx.Response(200, json=reply)


audible.httpx.Client = lambda **kwargs: client_type(
    **kwargs, transport=httpx.MockTransport(transport))

check.equal(audible.sims("RECOVER"), [], "a partial outage leaves this pass empty")
check.equal(store.get_sims("RECOVER", audible.AXIS_RAW), None,
            "an empty answer from one store cannot hide the failed store for a week")
replies["ca.invalid"] = {"similar_products": [{"asin": "NEXT", "title": "Next Book"}]}
check.equal([row["asin"] for row in audible.sims("RECOVER")], ["NEXT"],
            "the same book gains neighbours immediately after recovery")
before = len(calls)
audible.sims("RECOVER")
check.equal(len(calls), before, "positive neighbours are still cached")

replies["ca.invalid"] = {"similar_products": []}
check.equal(audible.sims("EMPTY"), [], "both stores can confirm an empty answer")
check.equal(store.get_sims("EMPTY", audible.AXIS_RAW), [],
            "a complete negative answer is still cached")

for index, malformed in enumerate(([], {}, {"similar_products": ["wrong row"]})):
    replies["ca.invalid"] = malformed
    check.equal(audible.sims(f"BAD{index}"), [], "malformed replies do not break ranking")
    check.equal(store.get_sims(f"BAD{index}", audible.AXIS_RAW), None,
                "malformed replies cannot produce a negative cache entry")

audible.httpx.Client = client_type
harness.cleanup()
raise SystemExit(check.report())
