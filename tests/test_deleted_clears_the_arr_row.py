"""Deleting something from the library clears what was still acquiring it.

The failure this covers is invisible from the client: Jellyfin removes a film,
Radarr goes on holding a monitored row for it, and the next sweep downloads it
again. To whoever deleted it, the film simply came back.

Two properties matter more than the happy path and most of this file is about
them:

* **resolution goes through the acquisition tool, not the ledger.** Nextup has
  only existed since 2026-08-30, and on the live server it has a ledger row for
  a handful of the 44 films Radarr holds. A ledger-first implementation passes
  its own tests and clears nothing in practice, so the case with no ledger row
  at all is tested first and deliberately.
* **nothing is cleared on the caller's word.** The library is re-read for the
  provider id, not the item id; a row with no file is left alone; and an
  account still waiting stops the whole thing.
"""
import harness

harness.setup(
    RADARR_URL="http://radarr.invalid:7878", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="1", RADARR_ROOT_FOLDER="/media/movies",
    SONARR_URL="http://sonarr.invalid:8989", SONARR_API_KEY="k",
    SONARR_QUALITY_PROFILE_ID="1", SONARR_ROOT_FOLDER="/media/tv",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import gone, jellyfin, media, store  # noqa: E402
from app.books import store as book_store  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

check = harness.Check("deleted")

store.init()

USER = jellyfin.User(id="u1", name="matt", is_admin=True)
OTHER = jellyfin.User(id="u2", name="alex", is_admin=False)

MOVIE = {"itemId": "jf-1", "type": "Movie", "name": "The 5th Wave",
         "providerIds": {"Tmdb": "299687", "Imdb": "tt2304933"}}
SERIES = {"itemId": "jf-2", "type": "Series", "name": "Game of Thrones",
          "providerIds": {"Tvdb": "121361"}}


class Tool:
    """A stand-in for one configured *arr, recording what it was asked to do."""

    def __init__(self, row=None, configured=True):
        self.row = row
        self.configured = configured
        self.deleted: list[str] = []
        self.asked: list[str] = []

    def existing(self, provider_id):
        self.asked.append(provider_id)
        return self.row

    def delete(self, backend_id):
        self.deleted.append(str(backend_id))
        return True


def library(movies=(), series=()):
    """Stub the owned index: what the library still holds after the delete.

    Stubbed at `jellyfin.owned_index`, which is what the guard calls. Stubbing
    `media.owned` instead would prove nothing about the thing that matters:
    the cache in front of it hands back a stale index during an outage, and
    the whole point is that this caller does not accept one.
    """
    jellyfin.owned_index = lambda: jellyfin.Owned(
        movie_tmdb=frozenset(movies), series_tvdb=frozenset(series))


def use(movie_tool=None, series_tool=None):
    from app import radarr, sonarr
    radarr.backend = lambda: movie_tool or Tool(row=None)
    sonarr.backend = lambda: series_tool or Tool(row=None)


def reset_ledger():
    with store.db() as conn:
        conn.execute("DELETE FROM requests")


# --- The case the whole feature lives or dies on -----------------------------

print("=== a film Radarr holds but nobody asked for here is still cleared ===")
reset_ledger()
library(movies=[], series=[])
radarr_tool = Tool(row={"id": 44, "title": "The 5th Wave", "hasFile": True})
use(movie_tool=radarr_tool)

report = gone.clear(MOVIE)
check.equal(radarr_tool.asked, ["299687"],
            "Radarr is asked by TMDB id, which is how a row with no ledger "
            "entry is found at all")
check.equal(radarr_tool.deleted, ["44"], "and its row is removed")
check.equal(report["cleared"], True, "the report says something was cleared")
check.equal(report["stopped"], True, "and that it was stopped")
check.equal(report["ledgerRows"], 0, "with no ledger row involved")

print("=== a settled ledger row goes with it, for every account ===")
reset_ledger()
store.record(USER.key, "movie", "tmdb:299687", "movie", "The 5th Wave", "2016", 1, "44")
store.record(OTHER.key, "movie", "tmdb:299687", "movie", "The 5th Wave", "2016", 1, "44")
store.mark_arrived(USER.key, "movie", {"tmdb:299687"})
store.mark_arrived(OTHER.key, "movie", {"tmdb:299687"})
radarr_tool = Tool(row={"id": 44, "title": "The 5th Wave", "hasFile": True})
use(movie_tool=radarr_tool)

report = gone.clear(MOVIE)
check.equal(report["ledgerRows"], 2,
            "both accounts' settled rows go: the file is gone for the house, "
            "so one surviving row would point at nothing")
check.equal(store.get(USER.key, "movie", "tmdb:299687"), None,
            "and the row really is gone")

# --- Nothing is cleared on the caller's word ---------------------------------

print("=== a film still in the library is refused ===")
reset_ledger()
library(movies=["299687"])
radarr_tool = Tool(row={"id": 44, "title": "The 5th Wave", "hasFile": True})
use(movie_tool=radarr_tool)

check.raises(gone.StillHere, lambda: gone.clear(MOVIE),
             "a TMDB id the library still holds refuses")
check.equal(radarr_tool.deleted, [],
            "and Radarr is not touched. The client's claim is never the "
            "evidence -- an item id proves some item went, not this one")

print("=== Jellyfin unreachable changes nothing ===")
reset_ledger()


def unavailable():
    raise jellyfin.JellyfinUnavailable("library listing timed out")


jellyfin.owned_index = unavailable
radarr_tool = Tool(row={"id": 44, "title": "The 5th Wave", "hasFile": True})
use(movie_tool=radarr_tool)

check.raises(gone.Unsettled, lambda: gone.clear(MOVIE),
             "an outage is its own answer, not a deletion")
check.equal(radarr_tool.deleted, [],
            "and nothing is cleared on an unverified claim")

# The cache in front of the index answers an outage with the LAST index it
# built, which is right for "what does the library hold" and wrong here twice:
# the outage stops being reported, and a film added since that index was built
# is missing from it, which reads exactly like a film that has gone.
media.owned = lambda force=False: jellyfin.Owned(
    movie_tmdb=frozenset(["299687"]))
check.raises(gone.Unsettled, lambda: gone.clear(MOVIE),
             "and a stale index is not accepted as the answer either")

print("=== a row with no file is work in flight, and is left alone ===")
reset_ledger()
library(movies=[])
radarr_tool = Tool(row={"id": 44, "title": "The 5th Wave", "hasFile": False})
use(movie_tool=radarr_tool)

check.raises(gone.StillHere, lambda: gone.clear(MOVIE),
             "hasFile false means nothing has landed yet")
check.equal(radarr_tool.deleted, [], "so the acquisition keeps running")

print("=== an account still waiting stops it ===")
reset_ledger()
library(movies=[])
store.record(OTHER.key, "movie", "tmdb:299687", "movie", "The 5th Wave", "2016", 1, "44")
radarr_tool = Tool(row={"id": 44, "title": "The 5th Wave", "hasFile": True})
use(movie_tool=radarr_tool)

check.raises(gone.StillHere, lambda: gone.clear(MOVIE),
             "an unfulfilled row anywhere in the house stops the clear")
check.equal(radarr_tool.deleted, [], "and Radarr keeps looking")
check.that(store.get(OTHER.key, "movie", "tmdb:299687") is not None,
           "the outstanding row survives, with the allowance it spent")

# --- Series ------------------------------------------------------------------

print("=== a series is counted in episode files, not hasFile ===")
reset_ledger()
library(series=[])
sonarr_tool = Tool(row={"id": 67, "title": "Game of Thrones",
                        "statistics": {"episodeFileCount": 73}})
use(series_tool=sonarr_tool)

report = gone.clear(SERIES)
check.equal(sonarr_tool.asked, ["121361"], "Sonarr is asked by TVDB id")
check.equal(sonarr_tool.deleted, ["67"], "and its row goes")
check.equal(report["medium"], "series", "reported as a series")

print("=== a series Sonarr has downloaded nothing for is left alone ===")
reset_ledger()
library(series=[])
sonarr_tool = Tool(row={"id": 67, "title": "Game of Thrones",
                        "statistics": {"episodeFileCount": 0}})
use(series_tool=sonarr_tool)
check.raises(gone.StillHere, lambda: gone.clear(SERIES),
             "no episode file means the acquisition has not delivered yet")
check.equal(sonarr_tool.deleted, [], "so it keeps running")

# --- The ordinary answer -----------------------------------------------------

print("=== most deletions clear nothing, and that is not a failure ===")
reset_ledger()
library()
radarr_tool = Tool(row=None)
use(movie_tool=radarr_tool)
report = gone.clear(MOVIE)
check.equal(report["cleared"], False, "nothing was acquiring it")
check.equal(report["message"], "Nothing was still looking for that.",
            "and the sentence says so plainly")

print("=== a film with no TMDB id asks nothing ===")
radarr_tool = Tool(row={"id": 44, "hasFile": True})
use(movie_tool=radarr_tool)
report = gone.clear({"itemId": "x", "type": "Movie", "name": "Home video",
                     "providerIds": {}})
check.equal(radarr_tool.asked, [],
            "no id means no row can exist under one, so Radarr is not asked")
check.equal(report["cleared"], False, "and nothing is claimed")

print("=== an episode does not unmonitor its series ===")
report = gone.clear({"itemId": "e1", "type": "Episode", "name": "Winter Is "
                     "Coming", "providerIds": {"Tvdb": "121361"}})
check.equal(report["medium"], None,
            "a ledger key and a Sonarr row are both series-level, so the only "
            "thing an episode could do here is too much")
check.equal(report["cleared"], False, "and it does nothing")

print("=== music is out, deliberately ===")
report = gone.clear({"itemId": "a1", "type": "MusicAlbum", "name": "Rumours",
                     "providerIds": {"MusicBrainzAlbum": "abc"}})
check.equal(report["cleared"], False,
            "a buskarr key is a catalogue reference Jellyfin does not hold")

# --- Books -------------------------------------------------------------------

print("=== a deleted book is matched on title and author, not on ASIN ===")
reset_ledger()
book_store.record_request(USER.key, "B0FMS8SNXH", "Splinter Angel: Book 1",
                          ["R. C. Joshua"])
store.mark_arrived(USER.key, "book", {"B0FMS8SNXH"})
jellyfin.books_named = lambda _title: []
stopped: list[str] = []
book_wants._stop_acquiring = lambda asin: stopped.append(asin) or True

report = gone.clear({
    "itemId": "b1", "type": "AudioBook", "name": "Splinter Angel: Book 1",
    "providerIds": {}, "authors": ["RC Joshua"]})
check.equal(stopped, ["B0FMS8SNXH"],
            "the ASIN it was asked for is not the one it arrived under, so "
            "the title with an author to agree is the only thing that joins "
            "the two -- and the initials spelling must not break it")
check.equal(report["ledgerRows"], 1, "and the settled row goes")

print("=== one failure does not skip the other rows ===")
reset_ledger()
# The same book under both marketplaces' ASINs, which is the ordinary shape
# here: the id it was asked for is not the id it arrives under.
book_store.record_request(USER.key, "B0AAAA", "Splinter Angel: Book 1",
                          ["R. C. Joshua"])
book_store.record_request(OTHER.key, "B0BBBB", "Splinter Angel: Book 1",
                          ["R. C. Joshua"])
store.mark_arrived(USER.key, "book", {"B0AAAA"})
store.mark_arrived(OTHER.key, "book", {"B0BBBB"})
jellyfin.books_named = lambda _title: []
tried: list[str] = []


def stubborn(asin):
    tried.append(asin)
    return asin != "B0AAAA"


book_wants._stop_acquiring = stubborn
report = gone.clear({"itemId": "b3", "type": "AudioBook",
                     "name": "Splinter Angel: Book 1", "providerIds": {},
                     "authors": ["R. C. Joshua"]})
check.equal(tried, ["B0AAAA", "B0BBBB"],
            "the second row is still tried after the first fails -- short "
            "circuiting here leaves a Listenarr row nobody would notice")
check.equal(report["stopped"], False, "and the report does not claim success")
check.equal(report["ledgerRows"], 2, "while both settled rows still go")

print("=== a book still on the shelf is refused ===")
reset_ledger()
book_store.record_request(USER.key, "B0FMS8SNXH", "Splinter Angel: Book 1",
                          ["R. C. Joshua"])
store.mark_arrived(USER.key, "book", {"B0FMS8SNXH"})
jellyfin.books_named = lambda _title: [
    {"Name": "Splinter Angel: Book 1", "AlbumArtist": "R. C. Joshua",
     "ProviderIds": {}}]
stopped = []
book_wants._stop_acquiring = lambda asin: stopped.append(asin) or True

check.raises(gone.StillHere, lambda: gone.clear({
    "itemId": "b1", "type": "AudioBook", "name": "Splinter Angel: Book 1",
    "providerIds": {}, "authors": ["R. C. Joshua"]}),
    "a copy still in the library refuses")
check.equal(stopped, [], "and Listenarr keeps its row")

print("=== a different book of the same name is not the one that went ===")
reset_ledger()
book_store.record_request(USER.key, "B0AAA", "Master Class", ["Ada Palmer"])
store.mark_arrived(USER.key, "book", {"B0AAA"})
jellyfin.books_named = lambda _title: []
stopped = []
book_wants._stop_acquiring = lambda asin: stopped.append(asin) or True

report = gone.clear({"itemId": "b2", "type": "AudioBook",
                     "name": "Master Class", "providerIds": {},
                     "authors": ["Someone Else"]})
check.equal(stopped, [],
            "the title agrees and the author does not, which is the whole "
            "point of requiring both")
check.equal(report["cleared"], False, "so nothing is claimed")

# --- The capability ----------------------------------------------------------

print("=== the capability is published, and is false with no tool at all ===")
from app import api  # noqa: E402

jellyfin.user_from_token = lambda _token: USER
app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)

caps = client.get("/api/v1/capabilities",
                  headers={"X-Emby-Token": "t"}).json()
check.equal(caps["deleted"]["supported"], True,
            "a deployment with Radarr configured can clear something")

real_supported = gone.supported
gone.supported = lambda: False
caps = client.get("/api/v1/capabilities",
                  headers={"X-Emby-Token": "t"}).json()
check.equal(caps["deleted"]["supported"], False,
            "and one with no acquisition tool says so, so a client offers "
            "nothing rather than a no-op")
gone.supported = real_supported

print("=== the endpoint's statuses are the ones a client can act on ===")
reset_ledger()
library(movies=[])
radarr_tool = Tool(row={"id": 44, "title": "The 5th Wave", "hasFile": True})
use(movie_tool=radarr_tool)
resp = client.post("/api/v1/deleted", headers={"X-Emby-Token": "t"},
                   json={"itemId": "jf-1", "type": "Movie",
                         "name": "The 5th Wave",
                         "providerIds": {"Tmdb": "299687"}})
check.equal(resp.status_code, 200, "an ordinary clear is a 200")
check.equal(resp.json()["stopped"], True, "and says what it did")

library(movies=["299687"])
resp = client.post("/api/v1/deleted", headers={"X-Emby-Token": "t"},
                   json={"itemId": "jf-1", "type": "Movie",
                         "name": "The 5th Wave",
                         "providerIds": {"Tmdb": "299687"}})
check.equal(resp.status_code, 409,
            "still in the library is a 409: the request was fine, the world "
            "is not what it described")

jellyfin.owned_index = unavailable
resp = client.post("/api/v1/deleted", headers={"X-Emby-Token": "t"},
                   json={"itemId": "jf-1", "type": "Movie",
                         "name": "The 5th Wave",
                         "providerIds": {"Tmdb": "299687"}})
check.equal(resp.status_code, 503, "and an outage is a 503, worth retrying")

resp = client.post("/api/v1/deleted", json={"itemId": "x", "type": "Movie"})
check.equal(resp.status_code, 401, "no token, no answer")

harness.cleanup()
raise SystemExit(check.report())
