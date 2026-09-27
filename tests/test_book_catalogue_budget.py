"""A failed catalogue is attempted once per shelf build, with prompt recovery."""
import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid", AUDIBLE_REGIONS="ca,us",
              JELLYFIN_USER="", LOG_LEVEL="CRITICAL")
harness.no_book_ratings()

import httpx

from app import jellyfin, listenarr, store
from app.books import audible, engine, hardcover_shelf

check = harness.Check("book catalogue outage budget")
store.init()
calls = []
failed = True


def transport(request):
    if request.url.path.endswith("/library"):
        return httpx.Response(200, json=[])
    calls.append(request.url.params["region"])
    if failed:
        raise httpx.ReadTimeout("simulated outage", request=request)
    return httpx.Response(200, json={"results": [{
        "asin": "MATCH", "title": "Volume 0", "authors": [{"name": "A Writer"}]}]})


listenarr._client = lambda: httpx.Client(
    base_url="http://listenarr.invalid", transport=httpx.MockTransport(transport))
audible.sims = lambda asin: []
library = [
    {"Id": str(i), "Name": f"Volume {i}", "AlbumArtist": "A Writer",
     "SeriesName": f"Saga {i}", "IndexNumber": 1, "UserData": {"Played": True}}
    for i in range(20)
]
library.append({"Id": "next", "Name": "Next Volume", "AlbumArtist": "A Writer",
                "SeriesName": "Saga 0", "IndexNumber": 2, "UserData": {"Played": False}})
jellyfin.books = lambda uid: library
engine.keyword_queries = lambda profile: ["adventure", "exploration"]
hardcover_shelf.wanted = lambda shelf: [{"title": "Wanted", "authors": ["A Writer"]}]
user = jellyfin.User(id="review", name="Review")
result = engine.run(user, update_playlist=False)
check.equal(calls, ["ca", "us"], "twenty seeds, keywords and wants share one failed attempt")
check.equal([row["id"] for row in result["own"]], ["next"], "local ranking still works")
check.that(all(store.get_audible_alias(f"item:{i}") is None for i in range(20)),
           "skipped lookups do not create negative aliases")

# A new build may recover immediately; failure is neither global nor timed.
failed = False
before = len(calls)
engine.run(user, update_playlist=False)
check.that(len(calls) > before, "the next build checks the recovered catalogue")
check.equal(store.get_audible_alias("item:0"), "MATCH", "a recovered edition is resolved")

harness.cleanup()
raise SystemExit(check.report())
