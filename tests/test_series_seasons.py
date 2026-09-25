"""Which seasons of a series are asked for, from the choice to the Sonarr body.

Until 2026-09-25 every series was added monitoring everything and searching for
every missing episode, so asking for Heartland fetched nineteen seasons. Now a
person chooses: every season, the latest one, a stretch of them, or only what
airs from now on. The Sonarr facts these bodies rely on were verified against
the live server that day (see `sonarr._monitor_only` and `_monitor_latest`).
"""
import harness

harness.setup(
    SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k",
    SONARR_QUALITY_PROFILE_ID="6",
    SERIES_DAILY_CAP="1",
)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, arr, config, jellyfin, main, media, seasons,  # noqa: E402
                 sonarr, store, wants)

check = harness.Check("series seasons")
store.init()

# --- the choice itself --------------------------------------------------------

for text, expected in [
        ("all", seasons.Seasons(seasons.ALL)),
        ("latest", seasons.Seasons(seasons.LATEST)),
        ("latest:37", seasons.Seasons(seasons.LATEST, 0, 37)),
        ("new", seasons.Seasons(seasons.NEW)),
        ("range:2-4", seasons.Seasons(seasons.RANGE, 2, 4))]:
    check.equal(seasons.decode(text), expected, f"{text} reads back")
    check.equal(expected.encode(), text, f"{text} is written the same way")
for text in ("", None, "range:4-2", "range:0-3", "range:x-3", "everything",
             "range:1-500", "latest:soon"):
    check.equal(seasons.decode(text), None, f"{text!r} is not a choice")

check.equal(seasons.from_body({"choice": "range", "from": 3, "to": 5}),
            seasons.Seasons(seasons.RANGE, 3, 5), "a stretch from a body")
check.equal(seasons.from_body({"choice": "latest"}),
            seasons.Seasons(seasons.LATEST), "the latest season from a body")
for body in ({"choice": "range", "from": 5, "to": 3},
             {"choice": "range", "from": 3},
             {"choice": "range", "from": True, "to": 3},
             {"choice": "some"}, "latest", None):
    check.raises(ValueError, lambda body=body: seasons.from_body(body),
                 f"{body!r} is refused with a sentence")

check.equal(seasons.Seasons(seasons.RANGE, 2, 2).phrase(), "season 2",
            "a stretch of one season is named as one")
check.equal(seasons.Seasons(seasons.LATEST, 0, 37).as_json(),
            {"choice": "latest", "season": 37},
            "the latest season says which one it turned out to be")

# --- the Sonarr body for each choice -------------------------------------------

SHOW = {"title": "Blackadder", "year": 1983, "tvdbId": 76736,
        "status": "ended",
        "seasons": [{"seasonNumber": n, "monitored": True} for n in range(0, 5)]}


class Response:
    def __init__(self, body=None, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "refused", request=httpx.Request("GET", "http://sonarr.invalid"),
                response=httpx.Response(self.status_code))
        return self

    def json(self):
        return self.body


class Sonarr:
    """A Sonarr that remembers what it was told."""

    configured = True
    quality_profile_id = 6

    def __init__(self, show=SHOW, episodes=None, put_status=200,
                 existing=None):
        self.show = show
        self.episodes = episodes if episodes is not None else []
        self.put_status = put_status
        self.existing_row = existing
        self.bodies, self.puts, self.commands, self.deleted = [], [], [], []

    def existing(self, provider_id):
        return self.existing_row

    def root_folder_path(self):
        return "/tv"

    def lookup(self, term, limit):
        return [self.show]

    def add(self, body):
        self.bodies.append(body)
        return arr.AddResult(True, "Sent to Sonarr.", "42", body["title"],
                             str(body["year"]))

    def delete(self, backend_id, preserve_downloaded=False):
        self.deleted.append(backend_id)
        return True

    def client(self, timeout=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def get(self, path, params=None):
        if path == "/episode":
            return Response(self.episodes)
        if path == "/series/42":
            return Response({"id": 42, "seasons": [
                {"seasonNumber": s["seasonNumber"], "monitored": False}
                for s in self.show["seasons"]]})
        raise AssertionError(path)

    def put(self, path, json=None):
        self.puts.append(json)
        return Response(json, self.put_status)

    def post(self, path, json=None):
        self.commands.append(json)
        return Response({})


def flags(seasons_list):
    return {s["seasonNumber"]: s["monitored"] for s in seasons_list}


def add_with(tool, choice, **kwargs):
    sonarr.backend = lambda: tool
    return sonarr.add("76736", "Blackadder", choice=choice, **kwargs)


tool = Sonarr()
result = add_with(tool, None)
body = tool.bodies[0]
check.equal(body["addOptions"]["monitor"], config.SONARR_MONITOR,
            "no choice monitors what the server is set to, as before")
check.that(body["addOptions"]["searchForMissingEpisodes"],
           "and searches for what is missing")
check.that("monitorNewItems" not in body,
           "and leaves new seasons as the catalogue record had them")
check.equal((result.message, result.seasons), ("Sent to Sonarr.", ""),
            "and answers as it always did, recording no choice")

tool = Sonarr()
result = add_with(tool, seasons.Seasons(seasons.ALL))
check.equal(tool.bodies[0]["addOptions"]["monitor"], "all",
            "every season monitors everything whatever the server's setting")
check.equal((result.message, result.seasons), ("Asked for every season.", "all"),
            "and says so")

tool = Sonarr()
result = add_with(tool, seasons.Seasons(seasons.RANGE, 2, 3))
body = tool.bodies[0]
check.equal(flags(body["seasons"]), {0: False, 1: False, 2: True, 3: True, 4: False},
            "a stretch monitors exactly those seasons, and never the specials")
check.equal(body["addOptions"]["monitor"], "skip",
            "and tells Sonarr to go by the seasons rather than a mode")
check.that(body["addOptions"]["searchForMissingEpisodes"], "and searches for them")
check.equal(body["monitorNewItems"], "none",
            "a season that starts airing later was not in the stretch")
check.equal((result.message, result.seasons),
            ("Asked for seasons 2 to 3.", "range:2-3"), "and says which")

tool = Sonarr()
result = add_with(tool, seasons.Seasons(seasons.RANGE, 3, 9))
check.equal(flags(tool.bodies[0]["seasons"]),
            {0: False, 1: False, 2: False, 3: True, 4: True},
            "a stretch past the last season stops at the last season")
check.equal((result.message, result.seasons),
            ("Asked for seasons 3 to 4.", "range:3-4"),
            "and names the seasons it could actually ask for")

tool = Sonarr()
result = add_with(tool, seasons.Seasons(seasons.RANGE, 6, 9))
check.that(not result.ok and not tool.bodies,
           "a stretch that misses every season adds nothing")
check.that("has seasons 1 to 4" in result.message,
           "and says which seasons the series has")

continuing = {**SHOW, "title": "Abbott Elementary", "status": "continuing"}
tool = Sonarr(show=continuing)
result = add_with(tool, seasons.Seasons(seasons.NEW))
body = tool.bodies[0]
check.equal(body["addOptions"]["monitor"], "future",
            "new episodes only monitors what has not aired")
check.that(not body["addOptions"]["searchForMissingEpisodes"],
           "and searches for nothing, since nothing it wants exists yet")
check.equal(body["monitorNewItems"], "all", "and follows new seasons")
check.equal((result.message, result.seasons),
            ("Asked for new episodes as they air.", "new"), "and says so")

tool = Sonarr()
result = add_with(tool, seasons.Seasons(seasons.NEW))
check.that(not result.ok and not tool.bodies,
           "an ended series has no new episodes to wait for, so nothing is added")
check.that("has ended" in result.message, "and the refusal says why")

tool = Sonarr(existing={"title": "Blackadder", "year": 1983})
result = add_with(tool, seasons.Seasons(seasons.RANGE, 1, 2))
check.that(result.ok and not result.created and not tool.bodies,
           "a series Sonarr already holds is left as it is")
check.equal(result.seasons, "", "and no choice is recorded against it")

tool = Sonarr()
add_with(tool, seasons.Seasons(seasons.RANGE, 1, 2), monitored=False)
check.equal(tool.bodies[0]["addOptions"]["monitor"], "none",
            "a verification add stays unmonitored whatever the choice")

# --- the latest season, decided once Sonarr has the episodes ------------------

sleeps = []
sonarr._sleep = lambda seconds: sleeps.append(seconds)
clock = [0.0]
sonarr._clock = lambda: clock[0]


def ticking(seconds):
    sleeps.append(seconds)
    clock[0] += seconds


sonarr._sleep = ticking


def episode(season, aired_on):
    return {"seasonNumber": season, "airDateUtc": aired_on}


aired = [episode(0, "2000-01-01T00:00:00Z"), episode(1, "1983-06-15T00:00:00Z"),
         episode(2, "1986-01-09T00:00:00Z"), episode(3, "1987-09-17T00:00:00Z"),
         episode(4, "2999-01-01T00:00:00Z"), episode(4, None)]
announced = {**continuing, "title": "The Simpsons"}
tool = Sonarr(show=announced, episodes=aired)
result = add_with(tool, seasons.Seasons(seasons.LATEST))
body = tool.bodies[0]
check.that(not any(s["monitored"] for s in body["seasons"])
           and body["addOptions"]["monitor"] == "skip"
           and not body["addOptions"]["searchForMissingEpisodes"],
           "the latest season adds with nothing monitored or searched, "
           "because the catalogue cannot say which season has aired")
check.equal(body["monitorNewItems"], "all", "and follows new seasons")
check.equal(flags(tool.puts[0]["seasons"]),
            {0: False, 1: False, 2: False, 3: True, 4: True},
            "then monitors the latest season to have aired, and the announced "
            "one after it, which Sonarr would not treat as new")
check.equal(tool.commands, [{"name": "SeriesSearch", "seriesId": 42}],
            "then searches")
check.equal(result.seasons, "latest:3", "and records the season it was")
check.equal(result.message,
            "Asked for season 3, the latest. New episodes will follow as they air.",
            "and names it")

clock[0] = 0.0
tool = Sonarr(show=announced, episodes=[])
result = add_with(tool, seasons.Seasons(seasons.LATEST))
check.equal(flags(tool.puts[0]["seasons"]),
            {0: False, 1: False, 2: False, 3: False, 4: True},
            "with no episode list inside the wait, the highest listed season")
check.equal(result.seasons, "latest:4", "is what gets recorded")
check.that(clock[0] >= config.SONARR_LATEST_WAIT_SECONDS,
           "after waiting the whole of the allowed time")

clock[0] = 0.0
tool = Sonarr(show=announced, episodes=aired, put_status=500)
result = add_with(tool, seasons.Seasons(seasons.LATEST))
check.that(not result.ok, "a latest season Sonarr could not be told about fails")
check.equal(tool.deleted, ["42"],
            "and the series is taken back out rather than left fetching nothing")
check.that("taken back out" in result.message, "and the answer says so")

# --- which choice an ask gets ---------------------------------------------------

media.available = lambda: {media.SERIES: media.Medium(
    media.SERIES, "Series", ("series",), 1, ("lib-tv",))}
media.get = lambda medium: media.available().get(medium)
media.owned = lambda: jellyfin.Owned()
media.cost = lambda medium, unit: 1
media.episode_counts = lambda provider_ids: {}
sonarr.acquisition_progress = lambda backend_ids: {}

passed = []


def fake_add(tvdb, title="", monitored=True, choice=None):
    passed.append(choice)
    encoded = ""
    if choice is not None:
        encoded = choice.encode()
        if choice.choice == seasons.LATEST:
            encoded = "latest:19"
    return arr.AddResult(True, "Sent.", f"s{tvdb}", title, "", seasons=encoded)


sonarr.add = fake_add

ADMIN = jellyfin.User(id="u-admin", name="matt", is_admin=True)
MEMBER = jellyfin.User(id="u-member", name="alex", is_admin=False)

wants.want(ADMIN, media.SERIES, "tvdb:1", hit={"title": "One"})
check.equal(passed[-1], None,
            "nobody has chosen and there is no default: no choice, as before")

store.put_user_setting(ADMIN.key, seasons.SETTING, "latest")
wants.want(ADMIN, media.SERIES, "tvdb:2", hit={"title": "Two"})
check.equal(passed[-1], seasons.Seasons(seasons.LATEST),
            "an account's own choice applies when the ask names none")

wants.want(ADMIN, media.SERIES, "tvdb:3", hit={"title": "Three"},
           choice=seasons.Seasons(seasons.RANGE, 1, 2))
check.equal(passed[-1], seasons.Seasons(seasons.RANGE, 1, 2),
            "a choice made for one ask beats the usual one")
check.equal(store.user_setting(ADMIN.key, seasons.SETTING), "latest",
            "and does not replace it unless asked to")

config.SERIES_SEASONS_DEFAULT = "range:1-1"
wants.want(MEMBER, media.SERIES, "tvdb:4", hit={"title": "Four"})
check.equal(passed[-1], seasons.Seasons(seasons.RANGE, 1, 1),
            "the server's default applies to somebody who has not chosen")
config.SERIES_SEASONS_DEFAULT = ""

try:
    wants.want(MEMBER, media.SERIES, "tvdb:5", hit={"title": "Five"},
               choice=seasons.Seasons(seasons.NEW), remember=True)
    refused = False
except wants.Denied:
    refused = True
check.that(refused, "a capped account's second series today is refused")
check.equal(store.user_setting(MEMBER.key, seasons.SETTING), "new",
            "but the choice it was asked to remember is kept all the same")

rows = {row["itemKey"]: row for row in wants.states(ADMIN, media.SERIES)}
check.equal(rows["tvdb:2"].get("seasons"), {"choice": "latest", "season": 19},
            "the request list says which season the latest turned out to be")
check.equal(rows["tvdb:3"].get("seasons"), {"choice": "range", "from": 1, "to": 2},
            "and which stretch was asked for")
check.that("seasons" not in rows["tvdb:1"],
           "and nothing for a request made without a choice")

# --- the API -------------------------------------------------------------------

jellyfin.user_from_token = lambda token: MEMBER
app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-Emby-Token": "a-real-looking-token"}


def series_block():
    caps = client.get("/api/v1/capabilities", headers=AUTH).json()
    return next(block for block in caps["media"] if block["medium"] == "series")


block = series_block()
check.equal(block["seasons"]["choices"], ["all", "latest", "range", "new"],
            "capabilities list the choices")
check.equal(block["seasons"]["choice"], {"choice": "new"},
            "and this account's own")
check.equal(block["seasons"]["default"], None, "and that there is no default")

put = client.put("/api/v1/seasons", headers=AUTH,
                 json={"choice": "range", "from": 2, "to": 3})
check.equal(put.status_code, 200, "an account can set its usual choice")
check.equal(put.json()["choice"], {"choice": "range", "from": 2, "to": 3},
            "and is told what it is now")
check.equal(store.user_setting(MEMBER.key, seasons.SETTING), "range:2-3",
            "which is what was stored")
check.equal(client.put("/api/v1/seasons", headers=AUTH,
                       json={"choice": "range", "from": 3, "to": 2}).status_code,
            400, "a stretch that runs backwards is refused")
check.equal(client.put("/api/v1/seasons", headers=AUTH,
                       json={"choice": None}).json()["choice"], None,
            "and a null choice clears it back to not chosen")

store.put_user_setting(MEMBER.key, seasons.SETTING, "")
wanted = client.post("/api/v1/want", headers=AUTH, json={
    "medium": "series", "itemKey": "tvdb:6", "title": "Six",
    "seasons": {"choice": "range", "from": 3, "to": 1}})
check.equal(wanted.status_code, 400,
            "an ask with a choice that does not parse is refused, not guessed")

wants.allowance = lambda user, medium: None
wanted = client.post("/api/v1/want", headers=AUTH, json={
    "medium": "series", "itemKey": "tvdb:7", "title": "Seven",
    "seasons": {"choice": "latest"}, "remember": True})
check.equal(wanted.status_code, 200, "an ask can carry a choice")
check.equal(passed[-1], seasons.Seasons(seasons.LATEST), "which is the one used")
check.equal(store.user_setting(MEMBER.key, seasons.SETTING), "latest",
            "and remembered when asked to be")

# --- what a keyholder sees -----------------------------------------------------

accounts = [{"id": "a", "seasons": ""}, {"id": "b", "seasons": ""},
            {"id": "c", "seasons": ""}, {"id": "d", "seasons": ""}]
chosen = {"a": "latest", "b": "range:1-2", "c": "latest"}
check.equal(main._season_counts(accounts, chosen),
            [("The latest season", 2), ("A stretch of seasons", 1),
             ("Not chosen yet", 1)],
            "the count puts the most chosen first and the undecided last")
check.equal(main._seasons_phrase("range:1-2"), "Seasons 1 to 2",
            "one account's stretch is named in full")
check.equal(main._seasons_phrase(None), "", "and nothing chosen reads as nothing")

harness.cleanup()
raise SystemExit(check.report())
