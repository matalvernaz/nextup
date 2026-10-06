"""New podcasts sorted into folders: the ask's choice, the fork's preview, the page and the API."""
import time

import harness

harness.setup(PODCAST_DAILY_CAP="5", JELLYFIN_USER="")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, arr, backends, episodes, itunes, jellyfin, main,  # noqa: E402
                 media, podcastfeed, podfetch, sessions, setup, store)

check = harness.Check("podcast folders")
store.init()

MATT = jellyfin.User(id="u-matt", name="matt", is_admin=True)
TOKENS = {"matt-token": MATT}
jellyfin.user_from_token = lambda token: TOKENS.get(token, MATT)
jellyfin.credential_rejected = lambda force=True: False
setup.needs_setup = lambda: False
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media.jellyfin.library_ids = lambda medium: {"podcast": ["lib-pods"]}.get(medium, [])
jellyfin.library_ids = media.jellyfin.library_ids
media.forget()
media._owned._value = jellyfin.Owned()
media._owned._built_at = time.monotonic()
jellyfin.podcasts_owned = lambda: []
jellyfin.podcast_episode_count = lambda item_id: None
podfetch._presence = (True, time.monotonic())

# Every address in these tests is public as far as the check knows; the one
# that is not says so.
podcastfeed.public_address_problem = lambda url: (
    "That address is on this network, so it cannot be fetched." if "192.168." in url else "")

FEED = "https://feeds.example/scp"


class Response:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self.body = body
        self.text = text

    def json(self):
        if self.body is None:
            raise ValueError("no body")
        return self.body


class Fork:
    """The fork as nextup sees it: records every call, answers as told."""

    def __init__(self):
        self.calls = []
        self.answer = Response(200, {})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return self.answer

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


fork = Fork()
jellyfin._client = lambda: fork

PREVIEW = {
    "Name": "SCP Archives", "Episodes": 322, "Skipped": 4, "BySeason": True,
    "Series": ["Class of '76", "Serapis"],
    "Folders": [
        {"Folder": "Class of '76", "Episodes": 10, "First": "Class of '76 - Part One",
         "Last": "Class of '76 - Part Ten", "Examples": ["Class of '76 - Part One"]},
        {"Folder": "Season 1", "Episodes": 34, "First": "SCP-087: \"The Stairwell\"",
         "Last": "SCP-2521 Pt. 2", "Examples": ["01 - SCP-087 - 'The Stairwell'"]},
        {"Folder": "", "Episodes": 1, "First": "April 1", "Last": "April 1", "Examples": []},
    ],
    "Routes": [{"Type": "trailer", "Skip": True}],
}

# --- the choice itself ---------------------------------------------------------------
check.equal([podfetch.organize_of(v) for v in (None, "", True, False, "yes", "no", "on", "0")],
            [True, True, True, False, True, False, True, False],
            "absent is yes, a ticked box or true is yes, anything else said is no")

# --- the order carries it to the fork ------------------------------------------------
fork.answer = Response(200, {"ItemId": "p1", "Created": True, "Name": "SCP Archives", "Organized": True})
done = podfetch.add(FEED, "SCP Archives", episodes.Episodes(episodes.ALL))
check.equal(fork.calls[-1], ("POST", "/Podcasts", {"feedUrl": FEED, "backfill": "all", "organize": True,
                                                   "title": "SCP Archives"}),
            "an ask sorts into folders unless told not to")
check.that("sorted into folders by season and series" in done.message,
           f"and says so when the fork sorted it: {done.message!r}")
fork.answer = Response(200, {"ItemId": "p1", "Created": False, "Name": "SCP Archives"})
kept = podfetch.add(FEED, "SCP Archives", None, organize=False)
check.equal(fork.calls[-1][2]["organize"], False, "told not to, it asks the fork not to")
check.that("folders" not in kept.message, "and does not claim a sorting the fork did not do")

# --- the preview ---------------------------------------------------------------------
fork.calls.clear()
preview, why, kind = podfetch.folders("http://192.168.1.181/feed.xml")
check.equal((preview, kind), (None, "refused"), "an address on this network is refused")
check.equal(fork.calls, [], "before the fork is asked to fetch it")

fork.answer = Response(404, None, "")
check.equal(podfetch.folders(FEED)[2], "unsupported", "a fork from before folders says so")
fork.answer = httpx.ConnectError("down")
check.equal(podfetch.folders(FEED)[2], "unreachable", "a fork not answering is not a refusal")
fork.answer = Response(400, {"detail": "That address is not a podcast feed with episodes in it."})
preview, why, kind = podfetch.folders(FEED)
check.equal((kind, why), ("refused", "That address is not a podcast feed with episodes in it."),
            "the fork's own reason is passed on")

fork.answer = Response(200, PREVIEW)
preview, why, kind = podfetch.folders(FEED)
check.equal(fork.calls[-1], ("GET", "/Podcasts/Organize", {"feedUrl": FEED}), "the fork is asked for this feed")
check.equal([(f["folder"], f["episodes"]) for f in preview["folders"]],
            [("Class of '76", 10), ("Season 1", 34), ("", 1)], "every folder, in the fork's order")
check.equal((preview["skipped"], preview["bySeason"], preview["series"]),
            (4, True, ["Class of '76", "Serapis"]), "with what is left out and the series found")
check.equal(podfetch.folders_sentence(preview),
            "Class of '76, 10 episodes; Season 1, 34 episodes; the podcast's own folder, 1 episode. "
            "4 trailers or announcements left out.",
            "said in one sentence")

# --- the app's API ---------------------------------------------------------------------
api_app = FastAPI()
api_app.include_router(api.router)
client = TestClient(api_app, raise_server_exceptions=False)
AUTH = {"X-Emby-Token": "matt-token"}

answer = client.get("/api/v1/podcast/folders", params={"feedUrl": FEED}, headers=AUTH)
check.equal(answer.status_code, 200, "the app can ask for the folders")
check.that(answer.json()["sentence"].startswith("Class of '76, 10 episodes"), "and gets the sentence too")
check.equal(client.get("/api/v1/podcast/folders", params={"feedUrl": "http://192.168.1.2/x"},
                       headers=AUTH).status_code, 400, "a refused address is a 400")
fork.answer = Response(404, None, "")
check.equal(client.get("/api/v1/podcast/folders", params={"feedUrl": FEED}, headers=AUTH).status_code,
            501, "a fork that does not sort is a 501")

three = client.get("/api/v1/capabilities?protocol=3", headers=AUTH).json()
block = next(b for b in three["media"] if b["medium"] == "podcast")
check.equal(block["episodes"]["folders"], True, "the capabilities say new podcasts are sorted")

# --- the ask passes the choice on --------------------------------------------------------
asked: list[bool] = []
podfetch.add = lambda feed, title="", choice=None, organize=True: (
    asked.append(organize) or arr.AddResult(True, "Subscribed.", "p-" + str(len(asked)), title))


def ask(feed, **extra):
    body = {"medium": "podcast", "itemKey": itunes.item_key(feed), "unit": "podcast",
            "title": feed.rsplit("/", 1)[-1], "feedUrl": feed, "episodes": {"choice": "new"}}
    body.update(extra)
    return client.post("/api/v1/want", json=body, headers=AUTH)


check.equal(ask("https://feeds.example/one").status_code, 200, "an ask with nothing said")
check.equal(asked[-1], True, "is sorted")
check.equal(ask("https://feeds.example/two", organize=False).status_code, 200, "an ask that says no")
check.equal(asked[-1], False, "is not")

# --- the page ------------------------------------------------------------------------------
pages = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
pages.cookies.set(sessions.COOKIE_NAME, sessions.issue("matt-token", MATT.id))
page = pages.post("/want", data={"medium": "podcast", "item_key": itunes.item_key("https://feeds.example/three"),
                                 "unit": "podcast", "title": "Three", "feedUrl": "https://feeds.example/three",
                                 "episodes": "new", "organize_offered": "yes"})
check.equal(page.status_code, 303, "the page's ask answers with the list")
check.equal(asked[-1], False, "a box offered and left unticked is a no")
pages.post("/want", data={"medium": "podcast", "item_key": itunes.item_key("https://feeds.example/four"),
                          "unit": "podcast", "title": "Four", "feedUrl": "https://feeds.example/four",
                          "episodes": "new", "organize_offered": "yes", "organize": "yes"})
check.equal(asked[-1], True, "ticked, it is a yes")

fork.answer = Response(200, PREVIEW)
shown = pages.get("/podcast/folders", params={"feedUrl": FEED, "title": "SCP Archives"})
check.equal(shown.status_code, 200, "the folders page opens")
text = shown.text
check.that("<h2>How SCP Archives would be sorted</h2>" in text, "headed with the podcast's name")
check.that("<h3>Class of &#39;76</h3>" in text or "<h3>Class of '76</h3>" in text, "each folder a heading")
check.that("from Class of &#39;76 - Part One to Class of &#39;76 - Part Ten" in text
           or "from Class of '76 - Part One to Class of '76 - Part Ten" in text,
           "with its first and last episode")
check.that("<h3>The podcast&#39;s own folder</h3>" in text or "<h3>The podcast's own folder</h3>" in text,
           "the podcast's own folder named, not blank")
refused = pages.get("/podcast/folders", params={"feedUrl": "http://192.168.1.3/x"})
check.equal(refused.status_code, 400, "a refused address says why on the page")
check.that("on this network" in refused.text, "in the page's own words")

harness.cleanup()
raise SystemExit(check.report())
