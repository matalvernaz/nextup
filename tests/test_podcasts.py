"""Podcasts as a medium: search, the ask and its choice, arrival, cancelling, deleting."""
import time

import harness

harness.setup(PODCAST_DAILY_CAP="2",
              RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
              RADARR_QUALITY_PROFILE_ID="6")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, arr, backends, episodes, gone, itunes, jellyfin,  # noqa: E402
                 media, podcastfeed, podcasts, podfetch, store, wants)

check = harness.Check("podcasts")
store.init()

MATT = jellyfin.User(id="u-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="kid", is_admin=False)
CURRENT = {"user": KID}
jellyfin.user_from_token = lambda token: CURRENT["user"]

backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media.jellyfin.library_ids = lambda medium: {"podcast": ["lib-pods"],
                                             "movie": ["lib-films"]}.get(medium, [])
jellyfin.library_ids = media.jellyfin.library_ids
media.forget()
media._owned._value = jellyfin.Owned()
media._owned._built_at = time.monotonic()

# --- what Jellyfin holds, and what the fork's fetcher does, are both stated ------
MAGNUS_FEED = "https://feeds.acast.com/public/shows/magnus"
LIBRARY: list[dict] = []          # Jellyfin's Podcast items
COUNTS: dict[str, int] = {}       # episodes per Jellyfin podcast id
jellyfin.podcasts_owned = lambda: list(LIBRARY)
jellyfin.podcast_episode_count = lambda item_id: COUNTS.get(item_id)
# The fork is present: its fetch task was "seen", so nothing probes the task list.
podfetch._presence = (True, time.monotonic())

added: list[tuple] = []
podfetch.add = lambda feed, title="", choice=None: (
    added.append((feed, title, choice)) or arr.AddResult(
        True, "Subscribed.", "pg-1", title or "Untitled", image_url="", overview=""))
PG_STATE = {"pg-1": {"downloaded": 0, "downloading": 0, "pending": 250, "total": 250,
                     "in_feed": 250, "paused": False, "title": "The Magnus Archives",
                     "url": MAGNUS_FEED, "error": ""}}
podfetch.state = lambda backend_id: PG_STATE.get(backend_id)
cancelled: list[str] = []
podfetch.cancel = lambda backend_id: cancelled.append(backend_id) or True

MAGNUS_HIT = {"itemKey": itunes.item_key(MAGNUS_FEED), "medium": "podcast", "unit": "podcast",
              "title": "The Magnus Archives", "year": "2021", "artist": "Rusty Quill",
              "feedUrl": MAGNUS_FEED, "itunesId": "1131532370", "overview": "",
              "genres": ["Fiction", "Drama"], "episodeCount": 250}
itunes.search = lambda query, limit=itunes.SEARCH_LIMIT: [dict(MAGNUS_HIT)] if "magnus" in query.lower() else []

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-Emby-Token": "a-real-looking-token"}


def media_of(response):
    return sorted(block["medium"] for block in response.json()["media"])


# --- capabilities: podcasts wait for a client that asked for protocol 3 ----------
check.equal(media_of(client.get("/api/v1/capabilities", headers=AUTH)), ["movie"],
            "protocol 1 hears nothing of podcasts")
check.equal(media_of(client.get("/api/v1/capabilities?protocol=2", headers=AUTH)), ["movie"],
            "nor does protocol 2, which shipped with EchoFin's Discover rows")
three = client.get("/api/v1/capabilities?protocol=3", headers=AUTH).json()
check.equal(sorted(b["medium"] for b in three["media"]), ["movie", "podcast"],
            "protocol 3 is told about podcasts")
block = next(b for b in three["media"] if b["medium"] == "podcast")
check.equal(block["units"], ["podcast"], "one unit")
check.equal(block["dailyCap"], 2, "with the podcast cap")
check.equal(block["episodes"], {"choices": ["all", "latest", "new"],
                                "defaultCount": episodes.DEFAULT_LATEST_COUNT,
                                "choice": None, "default": None},
            "and the episodes question, unanswered, with no default: the client's cue to ask")
recs = {r["medium"]: r for r in three["recommendations"]["media"]}
check.equal(recs["podcast"]["surfaces"], ["owned", "catalogue"],
            "podcasts recommend from the library and from the catalogue")
two = client.get("/api/v1/capabilities?protocol=2", headers=AUTH).json()
check.that("podcast" not in {r["medium"] for r in two["recommendations"]["media"]},
           "a protocol-2 client is not told about a podcast shelf either")

# --- search: words go to the catalogue, an address goes to the feed ----------------
hits = client.get("/api/v1/search?medium=podcast&q=magnus", headers=AUTH).json()["results"]
check.equal([h["title"] for h in hits], ["The Magnus Archives"], "words search the catalogue")
check.equal(hits[0]["owned"], False, "not owned yet")
check.equal(hits[0]["requested"], False, "not asked for yet")

preview = podcastfeed.FeedPreview(
    url="https://example.invalid/private.rss", title="Private Show", author="A Friend",
    description="Just for us.", image_url="", episode_count=3, latest=None,
    categories=["Comedy"])
podcastfeed.fetch = lambda url: preview
by_url = client.get("/api/v1/search?medium=podcast&q=https://example.invalid/private.rss",
                    headers=AUTH).json()["results"]
check.equal(len(by_url), 1, "an address is one hit")
check.equal(by_url[0]["title"], "Private Show", "named by the feed")
check.equal(by_url[0]["feedUrl"], "https://example.invalid/private.rss", "carrying the address")
check.equal(by_url[0]["itemKey"], itunes.item_key("https://example.invalid/private.rss"),
            "keyed on the address")
check.equal(by_url[0]["genres"], ["Comedy"], "with the feed's categories")


def unreadable(url):
    raise podcastfeed.Unreadable("That address answered 404, so the feed could not be read.")


podcastfeed.fetch = unreadable
bad = client.get("/api/v1/search?medium=podcast&q=https://example.invalid/nothing",
                 headers=AUTH)
check.equal(bad.status_code, 400, "an address that is not a feed is a 400")
check.that("404" in bad.json()["detail"], "with the sentence saying why")

# --- the ask: refused until the person says how much, then handed to the fork ------
body = {"medium": "podcast", "itemKey": MAGNUS_HIT["itemKey"], "unit": "podcast",
        "title": "The Magnus Archives", "feedUrl": MAGNUS_FEED, "itunesId": "1131532370"}
refused = client.post("/api/v1/want", json=body, headers=AUTH)
check.equal(refused.status_code, 409, "with no choice and no default, the ask is refused")
check.that("Choose how much" in refused.json()["detail"], "and says what to choose")
check.equal(added, [], "the fork was not asked")

accepted = client.post("/api/v1/want", json={**body, "episodes": {"choice": "latest", "count": 3},
                                             "remember": True}, headers=AUTH)
check.equal(accepted.status_code, 200, "with a choice, the ask is accepted")
check.equal(accepted.json()["state"], wants.ON_ITS_WAY, "and is on its way")
check.equal(added[-1][0], MAGNUS_FEED, "the fork was given the feed")
check.equal(added[-1][2], episodes.Episodes("latest", 3), "and the choice")
row = store.get(KID.key, "podcast", MAGNUS_HIT["itemKey"])
check.equal(row["episodes"], "latest:3", "the ledger remembers how much was asked for")
check.equal(row["backend_id"], "pg-1", "and the fork's item id")
check.equal(store.user_setting(KID.key, episodes.SETTING), "latest:3",
            "remember keeps it as this account's usual choice")
caps_after = client.get("/api/v1/capabilities?protocol=3", headers=AUTH).json()
block_after = next(b for b in caps_after["media"] if b["medium"] == "podcast")
check.equal(block_after["episodes"]["choice"], {"choice": "latest", "count": 3},
            "which capabilities now publishes")
check.equal(block_after["remainingToday"], 1, "one of two asks spent")

repeat = client.post("/api/v1/want", json=body, headers=AUTH)
check.equal(repeat.status_code, 200, "asking again is answered")
check.equal(repeat.json()["message"], "Already asked for.", "and free")

# --- the list of requests: the fork's progress ------------------------------------
listed = client.get("/api/v1/requests?medium=podcast", headers=AUTH).json()["requests"]
check.equal(len(listed), 1, "one request")
check.equal(listed[0]["state"], wants.ON_ITS_WAY, "on its way")
check.equal(listed[0]["episodes"], {"choice": "latest", "count": 3}, "saying how much was asked for")
check.that("none of its 250 episodes" in listed[0]["detail"], "and where the fork has got to")
PG_STATE["pg-1"].update({"downloaded": 3, "pending": 247, "downloading": 1})
listed = client.get("/api/v1/requests?medium=podcast", headers=AUTH).json()["requests"]
check.that("3 of 250 episodes fetched so far. Fetching now." in listed[0]["detail"],
           "counted as the fork counts, and saying it is fetching")

# --- arrival: the podcast in Jellyfin, with at least one episode ------------------
LIBRARY.append({"Id": "jf-magnus", "Name": "The Magnus Archives",
                "ProviderIds": {"PodcastFeed": MAGNUS_FEED, "iTunes": "1131532370"}})
podcasts.forget()
listed = client.get("/api/v1/requests?medium=podcast", headers=AUTH).json()["requests"]
check.equal(listed[0]["state"], wants.ON_ITS_WAY,
            "a podcast folder with no episodes yet has not arrived")
COUNTS["jf-magnus"] = 3
listed = client.get("/api/v1/requests?medium=podcast", headers=AUTH).json()["requests"]
check.equal(listed[0]["state"], wants.IN_LIBRARY, "with an episode, it has")
check.that(store.get(KID.key, "podcast", MAGNUS_HIT["itemKey"])["fulfilled_at"] is not None,
           "and the ledger says so")
hits = client.get("/api/v1/search?medium=podcast&q=magnus", headers=AUTH).json()["results"]
check.equal(hits[0]["owned"], True, "a search now marks it owned")
again = client.post("/api/v1/want", json=body, headers=AUTH)
check.equal(again.json()["state"], wants.IN_LIBRARY, "and asking says it is already here")

# Arrival by title alone, for a podcast Jellyfin holds without its feed known.
LIBRARY.append({"Id": "jf-scp", "Name": "SCP Archives", "ProviderIds": {}})
COUNTS["jf-scp"] = 300
podcasts.forget()
scp_feed = "https://feeds.simplecast.com/b7dgNp7A"
scp = {"medium": "podcast", "itemKey": itunes.item_key(scp_feed), "unit": "podcast",
       "title": "SCPArchives", "feedUrl": scp_feed}
check.equal(client.post("/api/v1/want", json=scp, headers=AUTH).json()["state"],
            wants.IN_LIBRARY, "the downloader's spelling of the title still matches the library's")

# --- cancelling stops the fork following it -------------------------------------------
PG_STATE["pg-2"] = {"downloaded": 0, "downloading": 0, "pending": 0, "total": 0, "in_feed": 10,
                    "paused": False, "title": "Private Show",
                    "url": "https://example.invalid/private.rss", "error": ""}
podfetch.add = lambda feed, title="", choice=None: arr.AddResult(True, "Subscribed.", "pg-2", title)
private = {"medium": "podcast", "itemKey": itunes.item_key("https://example.invalid/private.rss"),
           "unit": "podcast", "title": "Private Show",
           "feedUrl": "https://example.invalid/private.rss", "episodes": {"choice": "new"}}
check.equal(client.post("/api/v1/want", json=private, headers=AUTH).status_code, 200,
            "a second podcast is accepted (the cap is two)")
gone_now = client.post("/api/v1/cancel", json={"medium": "podcast", "itemKey": private["itemKey"]},
                       headers=AUTH)
check.equal(gone_now.status_code, 200, "cancelling is answered")
check.equal(cancelled, ["pg-2"], "and the fork stops following it, keeping what it fetched")

# --- the usual choice can be set and cleared on its own ----------------------------------
put = client.put("/api/v1/episodes", json={"choice": "all"}, headers=AUTH)
check.equal(put.status_code, 200, "the usual choice can be set")
check.equal(put.json()["choice"], {"choice": "all"}, "and is echoed back")
check.equal(client.put("/api/v1/episodes", json={"choice": None}, headers=AUTH).json()["choice"], None,
            "and cleared")
check.equal(client.put("/api/v1/episodes", json={"choice": "everything"}, headers=AUTH).status_code, 400,
            "an unknown choice is refused")

# --- the catalogue shelf ----------------------------------------------------------------
jellyfin.recommendation_items_for_user = lambda medium, uid, libraries: [
    {"Id": "jf-magnus", "Name": "The Magnus Archives", "Genres": ["Fiction", "Drama"],
     "Studios": [{"Name": "Rusty Quill"}], "UserData": {"PlayedPercentage": 40}},
]
suggested: list[tuple] = []
itunes.suggestions = lambda genres, studios, exclude_keys, exclude_titles, limit: (
    suggested.append((genres, studios, set(exclude_keys), set(exclude_titles))) or [
        {**MAGNUS_HIT, "itemKey": "feed:new", "title": "Something Similar",
         "score": 3.0, "reason": ["shares the Fiction genre with podcasts you listen to"]}])
shelf = client.get("/api/v1/recommendations?medium=podcast&surface=catalogue", headers=AUTH)
check.equal(shelf.status_code, 200, "the catalogue shelf is answered")
check.equal([r["title"] for r in shelf.json()["recommendations"]], ["Something Similar"], "with rows")
check.equal(suggested[-1][0], ["Drama", "Fiction"], "from the listener's genres")
check.equal(suggested[-1][1], ["Rusty Quill"], "and makers")
check.that(MAGNUS_HIT["itemKey"] in suggested[-1][2], "excluding what the library holds")
check.that("SCP Archives" in suggested[-1][3], "by title as well as feed")
check.equal(client.get("/api/v1/recommendations?medium=movie&surface=catalogue", headers=AUTH).status_code,
            404, "films have no catalogue shelf")

# --- deleting a podcast from the library clears the ledger, or refuses while it is still there ---
CURRENT["user"] = MATT
KEYHOLDER = {"X-Emby-Token": "a-keyholders-token"}
still = client.post("/api/v1/deleted", json={
    "itemId": "jf-magnus", "type": "Podcast", "name": "The Magnus Archives",
    "providerIds": {"PodcastFeed": MAGNUS_FEED}}, headers=KEYHOLDER)
check.equal(still.status_code, 409, "a podcast still in the library is left alone")
LIBRARY[:] = [row for row in LIBRARY if row["Id"] != "jf-magnus"]
podcasts.forget()
cleared = client.post("/api/v1/deleted", json={
    "itemId": "jf-magnus", "type": "Podcast", "name": "The Magnus Archives",
    "providerIds": {"PodcastFeed": MAGNUS_FEED}}, headers=KEYHOLDER)
check.equal(cleared.status_code, 200, "once it is gone, the deletion is acted on")
check.equal(cleared.json()["cleared"], False,
            "nothing had to be stopped: the fork's fetch task only visits podcasts the library holds")

harness.cleanup()
raise SystemExit(check.report())
