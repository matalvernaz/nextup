"""Podcasts here are recommendations only: both shelves stay, asking goes.

A show is added in EchoFin, through the Jellyfin fork (Matt, 2026-10-27), so
this service neither searches for one to ask for nor takes an ask, and its
web page says where a suggested show is added instead of offering a button.
"""
import time

import harness

# Both are settings of the request path that went. Still set on a server that
# had them, they are ignored rather than refused.
harness.setup(PODCAST_DAILY_CAP="2", PODCAST_EPISODES_DEFAULT="everything",
              RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
              RADARR_QUALITY_PROFILE_ID="6", JELLYFIN_USER="")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, backends, buskarr, itunes, jellyfin, main,  # noqa: E402
                 media, podcasts, sessions, store)

check = harness.Check("podcasts")
store.init()

MATT = jellyfin.User(id="u-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="kid", is_admin=False)
CURRENT = {"user": KID}
jellyfin.user_from_token = lambda token: CURRENT["user"]
jellyfin.credential_rejected = lambda force=True: False

backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media.jellyfin.library_ids = lambda medium: {"podcast": ["lib-pods"],
                                             "movie": ["lib-films"]}.get(medium, [])
jellyfin.library_ids = media.jellyfin.library_ids
media.forget()
media._owned._value = jellyfin.Owned()
media._owned._built_at = time.monotonic()

# --- what Jellyfin holds is stated ---------------------------------------------------
MAGNUS_FEED = "https://feeds.acast.com/public/shows/magnus"
SIMILAR_FEED = "https://feeds.example/something-similar"
LIBRARY: list[dict] = [
    {"Id": "jf-magnus", "Name": "The Magnus Archives",
     "ProviderIds": {"PodcastFeed": MAGNUS_FEED, "iTunes": "1131532370"}},
    # Held without its feed known, so it can only be recognised by title.
    {"Id": "jf-scp", "Name": "SCP Archives", "ProviderIds": {}},
]
jellyfin.podcasts_owned = lambda: list(LIBRARY)
podcasts.forget()

MAGNUS_KEY = itunes.item_key(MAGNUS_FEED)
SIMILAR_KEY = itunes.item_key(SIMILAR_FEED)

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-Emby-Token": "a-real-looking-token"}


def media_of(response):
    return sorted(block["medium"] for block in response.json()["media"])


# --- capabilities: no podcast requests at any protocol, both shelves at 3 -------------
for query, said in (("", "protocol 1"), ("?protocol=2", "protocol 2"),
                    ("?protocol=3", "protocol 3")):
    check.equal(media_of(client.get(f"/api/v1/capabilities{query}", headers=AUTH)),
                ["movie"], f"{said} is offered nothing to ask for in podcasts")
three = client.get("/api/v1/capabilities?protocol=3", headers=AUTH).json()
check.that(all("episodes" not in block for block in three["media"]),
           "and no question of how many episodes")
recs = {r["medium"]: r for r in three["recommendations"]["media"]}
check.equal(recs["podcast"]["surfaces"], ["owned", "catalogue"],
            "podcasts still recommend from the library and from Apple's directory")
two = client.get("/api/v1/capabilities?protocol=2", headers=AUTH).json()
check.that("podcast" not in {r["medium"] for r in two["recommendations"]["media"]},
           "a protocol-2 client is not told about a podcast shelf")

# --- asking: answered as for any medium the server does not offer ----------------------
searched = client.get("/api/v1/search?medium=podcast&q=magnus", headers=AUTH)
check.equal(searched.status_code, 404, "a podcast search is not served")
body = {"medium": "podcast", "itemKey": SIMILAR_KEY, "unit": "podcast",
        "title": "Something Similar"}
refused = client.post("/api/v1/want", json=body, headers=AUTH)
check.equal(refused.status_code, 409, "an ask for a podcast is refused")
check.equal(refused.json()["detail"],
            "That kind of thing cannot be asked for on this server.",
            "as any medium the server does not offer is")
older = client.post("/api/v1/want", json={
    **body, "feedUrl": SIMILAR_FEED, "itunesId": "1",
    "episodes": {"choice": "latest", "count": 3}, "remember": True}, headers=AUTH)
check.equal(older.status_code, 409,
            "so is one from an older client, carrying a feed and a choice of episodes")
check.equal(store.get(KID.key, "podcast", SIMILAR_KEY), None, "nothing is written down")
check.equal(store.user_setting(KID.key, "PODCAST_EPISODES"), None,
            "and no choice of episodes is kept")
check.equal(client.put("/api/v1/episodes", json={"choice": "all"}, headers=AUTH).status_code,
            404, "there is no usual choice of episodes to set")
check.equal(client.get(f"/api/v1/podcast/folders?feedUrl={SIMILAR_FEED}",
                       headers=AUTH).status_code,
            404, "nor folders to preview")

# --- the catalogue shelf --------------------------------------------------------------
jellyfin.recommendation_items_for_user = lambda medium, uid, libraries: [
    {"Id": "jf-magnus", "Name": "The Magnus Archives", "Genres": ["Fiction", "Drama"],
     "Studios": [{"Name": "Rusty Quill"}], "UserData": {"PlayedPercentage": 40}},
]
suggested: list[tuple] = []
itunes.suggestions = lambda genres, studios, exclude_keys, exclude_titles, limit: (
    suggested.append((genres, studios, set(exclude_keys), set(exclude_titles))) or [
        {"itemKey": SIMILAR_KEY, "medium": "podcast", "unit": "podcast",
         "title": "Something Similar", "artist": "Rusty Quill", "feedUrl": SIMILAR_FEED,
         "itunesId": "1", "overview": "", "genres": ["Fiction"], "episodeCount": 40,
         "score": 3.0, "reason": ["shares the Fiction genre with podcasts you listen to"]}])
# A podcast request left in the ledger by the old release. The shelf no longer
# reads the ledger, so it neither leaves this show out nor marks it.
store.record(KID.key, "podcast", SIMILAR_KEY, "podcast", "Something Similar", "", 1, "pg-1")

shelf = client.get("/api/v1/recommendations?medium=podcast&surface=catalogue", headers=AUTH)
check.equal(shelf.status_code, 200, "the catalogue shelf is answered")
rows = shelf.json()["recommendations"]
check.equal([r["title"] for r in rows], ["Something Similar"], "with rows")
check.equal(suggested[-1][0], ["Drama", "Fiction"], "from the listener's genres")
check.equal(suggested[-1][1], ["Rusty Quill"], "and makers")
check.equal(suggested[-1][2], {MAGNUS_KEY},
            "leaving out what the library holds, and nothing anybody asked for")
check.that("SCP Archives" in suggested[-1][3], "by title as well as feed")
check.equal((rows[0]["owned"], "requested" in rows[0]), (False, False),
            "a row says whether the library has it, and nothing about asking")

# Read again, from the cache: the same rule, and the owned flag read afresh.
LIBRARY.append({"Id": "jf-similar", "Name": "Something Similar",
                "ProviderIds": {"PodcastFeed": SIMILAR_FEED}})
podcasts.forget()
cached = client.get("/api/v1/recommendations?medium=podcast&surface=catalogue",
                    headers=AUTH).json()["recommendations"]
check.equal(len(suggested), 1, "the second read is the cached shelf")
check.equal((cached[0]["owned"], "requested" in cached[0]), (True, False),
            "which marks a show added since as owned, and says nothing about asking")
LIBRARY.pop()
podcasts.forget()
check.equal(client.get("/api/v1/recommendations?medium=movie&surface=catalogue",
                       headers=AUTH).status_code,
            404, "films have no catalogue shelf")

# --- the web page: no button, and where to add a show instead ---------------------------
web = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
web.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", KID.id))
page = web.get("/discover?medium=podcast")
check.equal(page.status_code, 200, "the podcast shelves render")
check.that("Something Similar" in page.text, "with the suggestion on them")
check.that('action="/want"' not in page.text, "and nothing to ask for it with")
check.that("Add one in EchoFin: Library, Podcasts, Add a Show." in page.text,
           "but where to add one")
check.that("You can ask for" not in page.text
           and "Requests for podcasts are not enabled" not in page.text,
           "and no allowance to spend")

# --- an ask the start-up migration declined can be taken off the list ------------------
# It stays on the asker's list a while, with a button to remove it. That goes
# through the held ask alone: nothing was handed to anything, and a podcast has
# no acquisition tool here to call off.
store.hold(KID.key, "podcast", SIMILAR_KEY, "podcast", "Something Similar", "", "", "",
           {"title": "Something Similar"}, "")
store.decline_held("podcast", SIMILAR_KEY, store.PODCASTS_ADDED_ELSEWHERE, "")
store.release(KID.key, "podcast", SIMILAR_KEY)  # the old request, as the migration leaves it
stopped: list[str] = []
buskarr.cancel = lambda backend_id: stopped.append(backend_id) or True
removed = client.post("/api/v1/cancel", json={"medium": "podcast", "itemKey": SIMILAR_KEY},
                      headers=AUTH)
check.equal((removed.status_code, removed.json()["message"]), (200, "Taken off your list."),
            "a declined podcast ask can be taken off the list")
check.equal((store.held(KID.key, "podcast", SIMILAR_KEY), stopped), (None, []),
            "and only the held ask goes: nothing is asked to stop")

# --- deleting a podcast from the library is nothing this service tracks ----------------
CURRENT["user"] = MATT
deleted = client.post("/api/v1/deleted", json={
    "itemId": "jf-magnus", "type": "Podcast", "name": "The Magnus Archives",
    "providerIds": {"PodcastFeed": MAGNUS_FEED}}, headers={"X-Emby-Token": "a-keyholders-token"})
check.equal(deleted.status_code, 200, "a deleted podcast is answered")
check.equal((deleted.json()["medium"], deleted.json()["cleared"]), (None, False),
            "as a type this service does not track, with nothing cleared")

harness.cleanup()
raise SystemExit(check.report())
