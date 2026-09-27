"""A catalogue outage is not a missing book or a missing edition."""
import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid:4545",
              AUDIBLE_REGIONS="ca,us", JELLYFIN_USER="")
harness.no_book_ratings()

import httpx
from fastapi.testclient import TestClient

from app import arr, backends, jellyfin, listenarr, main, media, store
from app.books import audible, engine

check = harness.Check("book catalogue outages")
store.init()
user = jellyfin.User(id="reader", name="Reader")
jellyfin.user_from_token = lambda token: user
jellyfin.library_ids = lambda medium: ["books"] if medium == "book" else []
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)

replies = {"ca": 503, "us": 503}
lookups = []
edition = {"asin": "EDITION", "title": "First Volume",
           "authors": [{"name": "A Writer"}]}
neighbours = [{"asin": "NEIGHBOUR", "title": "Another Book",
               "authors": ["A Writer"]}]

def transport(request):
    if request.url.path == "/api/v1/library":
        return httpx.Response(200, json=[])
    region = request.url.params["region"]
    lookups.append((region, request.url.params["query"]))
    reply = replies[region]
    if isinstance(reply, int):
        return httpx.Response(reply)
    return httpx.Response(200, json=reply)

listenarr._client = lambda: httpx.Client(
    base_url="http://listenarr.invalid:4545", transport=httpx.MockTransport(transport))
audible.sims = lambda asin: neighbours if asin == "EDITION" else []

def seed(ident):
    return {"Id": ident, "Name": "First Volume", "AlbumArtist": "A Writer",
            "SeriesName": "Saga", "IndexNumber": 1, "UserData": {"Played": True}}

# Search failures need the same honest 503 on both current API families.
media.forget()
client = TestClient(main.app, raise_server_exceptions=False)
headers = {"X-Emby-Token": "reader-token"}
for route in ("/api/v1/search?medium=book&q=title",
              "/nextread/api/v1/search?q=title"):
    response = client.get(route, headers=headers)
    check.equal(response.status_code, 503, f"{route} reports a catalogue outage")
    check.that("book catalogue" in response.json()["detail"],
               "the error identifies the failing dependency")

check.equal(engine._seed_sims(seed("failed")), [], "a failed seed does not fail the shelf")
check.equal(store.get_audible_alias("item:failed"), None,
            "an outage is not cached as a seven-day missing edition")

# Partial success cannot prove absence in a different marketplace.
replies.update(ca=503, us={"results": []})
check.raises(arr.Unavailable, lambda: listenarr.audible_search("title"),
             "one empty store and one failed store is not 'nothing found'")
engine._seed_sims(seed("partial"))
check.equal(store.get_audible_alias("item:partial"), None,
            "partial failure cannot populate the negative alias cache")
replies["us"] = {"results": [edition]}
check.equal(listenarr.audible_search("title"), [edition],
            "interactive search can still offer positive hits from a working store")
check.raises(arr.Unavailable,
             lambda: listenarr.audible_search("title", require_complete=True),
             "a caller deciding absence requires every marketplace to answer")

# Recovery is immediate at the resolver; no cache reset or week-long wait.
replies["ca"] = {"results": [edition]}
check.equal(engine._seed_sims(seed("failed")), neighbours,
            "the same failed seed resolves as soon as the catalogue recovers")
check.equal(store.get_audible_alias("item:failed"), "EDITION",
            "a successful match records its actual edition")

audible.sims = lambda asin: []
engine._seed_sims(seed("neighbour-outage"))
check.equal(store.get_audible_alias("item:neighbour-outage"), "EDITION",
            "a similarity outage does not erase a confirmed edition")
before = len(lookups)
audible.sims = lambda asin: neighbours if asin == "EDITION" else []
check.equal(engine._seed_sims(seed("neighbour-outage")), neighbours,
            "similarity recovery uses the confirmed edition")
check.equal(len(lookups), before, "it does not repeat the catalogue resolution")

replies.update(ca={"results": []}, us={"results": []})
audible.sims = lambda asin: []
engine._seed_sims(seed("neighbour-outage"))
check.equal(store.get_audible_alias("item:neighbour-outage"), "EDITION",
            "empty later searches cannot erase an already confirmed edition")
engine._seed_sims(seed("missing"))
check.equal(store.get_audible_alias("item:missing"), "",
            "an actual missing edition is still cached")

replies.update(ca=["wrong envelope"], us={"results": ["wrong row"]})
check.raises(arr.Unavailable, lambda: listenarr.audible_search("title"),
             "malformed catalogue data is an unavailable answer")
replies.update(ca={}, us={"results": None})
check.raises(arr.Unavailable, lambda: listenarr.audible_search("title"),
             "a missing result collection cannot prove that no books matched")

replies.update(ca=503, us=503)
before = len(lookups)
check.equal(engine._want_candidates([
    {"title": "First", "authors": ["A Writer"]},
    {"title": "Second", "authors": ["A Writer"]},
], lambda row: False, set()), {}, "a failed want-to-read lookup remains optional")
check.equal(len(lookups) - before, 2,
            "a catalogue outage stops repeated lookups for this want-to-read pass")

# Local owned ranking is useful even when every catalogue lookup fails.
replies.update(ca=503, us=503)
jellyfin.books = lambda uid: [seed("local"), {
    "Id": "next", "Name": "Second Volume", "AlbumArtist": "A Writer",
    "SeriesName": "Saga", "IndexNumber": 2, "UserData": {"Played": False}}]
result = engine.run(user, update_playlist=False)
check.equal([r["id"] for r in result["own"]], ["next"],
            "a catalogue outage leaves the owned reading recommendation usable")

harness.cleanup()
raise SystemExit(check.report())
