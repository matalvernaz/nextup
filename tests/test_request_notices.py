"""People are told on their phones what happened to what they asked for.

Matt, 2026-10-30, on whether people can be notified when their requests are
acknowledged. A keyholder's yes or no, an ask going through by itself, an ask
somebody else's settled, and a new book from a followed series are each said
to the person, through Jellyfin, which keeps their phones. Only to somebody
who turned it on, never twice for one thing, and never in front of the
request itself.
"""
import threading

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4", MOVIE_DAILY_CAP="2", BOOK_DAILY_CAP="3",
    APPROVALS="on", REQUEST_NOTICES="on", JELLYFIN_USER="",
    LISTENARR_URL="http://listenarr.invalid", LISTENARR_API_KEY="k",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, approvals, arr, config, held, jellyfin,  # noqa: E402
                 listenarr, media, notices, radarr, store, wants)
from app.books import follow, series, shelves  # noqa: E402

check = harness.Check("request notices")
store.init()

MATT = jellyfin.User(id="u-matt", name="Matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="Kid", is_admin=False)
OTHER = jellyfin.User(id="u-other", name="Other", is_admin=False)
THIRD = jellyfin.User(id="u-third", name="Third", is_admin=False)
ACCOUNTS = [MATT, KID, OTHER, THIRD]
TOKENS = {"kid-token": KID, "other-token": OTHER}
jellyfin.accounts = lambda: list(ACCOUNTS)
jellyfin.account = lambda reference: next(
    a for a in ACCOUNTS if a.id == reference or a.name == reference)
jellyfin.user_from_token = lambda token: TOKENS[token]
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: []
jellyfin.books = lambda user_id: []

REGISTRY = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 2, ("lib-movies",)),
    media.BOOK: media.Medium(media.BOOK, "Books", ("book", "series"), 3, ("lib-books",)),
}
media.available = lambda: dict(REGISTRY)
media.get = lambda medium: REGISTRY.get(medium)
media.owned = lambda force=False: jellyfin.Owned()
radarr.add = lambda tmdb, title="", year="", monitored=True: arr.AddResult(
    True, "Sent to Radarr.", f"r{tmdb}", title, year)
held._post = lambda url, body, headers: None
shelves.owned_index = lambda user: (set(), {})
shelves.forget_asin = lambda asin, user_key=None: None
listenarr.add = lambda asin, monitored=True, metadata=None: listenarr.AddResult(
    True, "Sent to Listenarr", 41, title=f"Book {asin}", authors=("An Author",))
listenarr.enqueue_search = lambda audiobook_id: True
listenarr.metadata_from_search_row = lambda row, region=None: {"asin": row["asin"]}

#: Every notice Jellyfin was asked to deliver, as (account, payload).
delivered: list[tuple[str, dict]] = []
#: What Jellyfin does with the next one: deliver, or refuse in one of its ways.
jellyfin_answer = {"raise": None}


def fake_notify(user_id, payload):
    if jellyfin_answer["raise"] is not None:
        raise jellyfin_answer["raise"]
    delivered.append((user_id, payload))
    return {"Sent": 1, "Failed": 0, "Dropped": 0}


real_notify_user = jellyfin.notify_user
jellyfin.notify_user = fake_notify


def settle():
    for thread in threading.enumerate():
        if thread.name == "request-notice":
            thread.join(5)


def bodies(user):
    settle()
    return [payload["Body"] for user_id, payload in delivered if user_id == user.key]


def film(n):
    return {"title": f"Film {n}", "year": "2001"}


def ask(user, n):
    return wants.want(user, media.MOVIE, f"tmdb:{n}", "movie", film(n), over_limit=True)


def a_day_passes():
    with store.db() as conn:
        conn.execute("UPDATE requests SET requested_at = requested_at - 90000")
        conn.execute("UPDATE followed_series SET checked_at = checked_at - 90000")


# --- nobody is told anything until they turn it on ---------------------------------

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)
as_kid = {"X-Emby-Token": "kid-token"}
caps = client.get("/api/v1/capabilities", headers=as_kid).json()
check.equal(caps.get("requestNotices"), {"supported": True, "on": False},
            "a server that can send says so, and an account starts with it off")

ask(KID, 1)
ask(KID, 2)
ask(KID, 3)
approvals.approve(MATT, media.MOVIE, "tmdb:3")
check.equal(bodies(KID), [], "with it off, a yes tells them nothing")

answer = client.put("/api/v1/request-notices", headers=as_kid, json={"on": True})
check.equal((answer.status_code, answer.json()), (200, {"supported": True, "on": True}),
            "turning it on answers with the new state")
check.equal(client.get("/api/v1/capabilities", headers=as_kid).json()["requestNotices"],
            {"supported": True, "on": True}, "and capabilities carry it")

# --- a keyholder's yes -------------------------------------------------------------

ask(KID, 4)
approvals.approve(MATT, media.MOVIE, "tmdb:4")
check.equal(bodies(KID), ["Matt approved Film 4. It is on its way."],
            "a yes is said, with what it means")
user_id, payload = delivered[-1]
check.equal((payload["Title"], payload["Kind"], payload["Data"]),
            ("Request approved", "requests", {"medium": "movie", "itemKey": "tmdb:4"}),
            "titled, marked as about requests, and naming what it is about")

# --- a keyholder's no --------------------------------------------------------------

delivered.clear()
ask(KID, 5)
approvals.decline(MATT, media.MOVIE, "tmdb:5", "We have it on disc")
check.equal(bodies(KID), ["Matt said no to Film 5. We have it on disc."],
            "a no is said, with the reason given")
ask(KID, 6)
approvals.decline(MATT, media.MOVIE, "tmdb:6")
check.equal(bodies(KID)[-1], "Matt said no to Film 6.", "and without one when none was")

# --- it went through by itself ------------------------------------------------------

delivered.clear()
ask(KID, 7)
a_day_passes()
approvals.release_when_room()
check.equal(bodies(KID), ["Film 7 went through by itself, now that your allowance "
                          "has room. It is on its way."],
            "an ask going through by itself is said")

# --- somebody else's ask settles it ---------------------------------------------------

delivered.clear()
for n in (8, 9):
    ask(KID, n)
ask(KID, 10)
check.equal(ask(THIRD, 10)[0], wants.ON_ITS_WAY, "Third asks for the same film within their allowance")
check.equal(bodies(KID), ["Film 10 is on its way. Somebody else asked for it too, so "
                          "it no longer needs approving."],
            "the one who was waiting is told it is coming")

delivered.clear()
notices.set_wanted(OTHER.key, True)
store.set_cap(OTHER.key, media.MOVIE, 0)
ask(OTHER, 11)
ask(KID, 11)
approvals.approve(MATT, media.MOVIE, "tmdb:11")
check.equal(sorted(bodies(KID) + bodies(OTHER)),
            ["Matt approved Film 11. It is on its way.",
             "Matt approved Film 11. It is on its way."],
            "one yes for two askers tells each of them once, and only that")

# --- only those who turned it on -------------------------------------------------------

delivered.clear()
store.set_cap(THIRD.key, media.MOVIE, 0)
ask(THIRD, 12)
approvals.approve(MATT, media.MOVIE, "tmdb:12")
check.equal(bodies(THIRD), [], "somebody who never turned it on is told nothing")
client.put("/api/v1/request-notices", headers=as_kid, json={"on": False})
ask(KID, 13)
approvals.approve(MATT, media.MOVIE, "tmdb:13")
check.equal(bodies(KID), [], "nor somebody who turned it off again")
notices.set_wanted(KID.key, True)

# --- a new book in a followed series -------------------------------------------------------

delivered.clear()
CATALOGUE = [{"asin": "SA01", "title": "Saga 1", "key": "#1", "position": "1"}]


def fake_plan(user, name, anchor_item_id=None, library=None):
    asked = {row["item_key"] for row in store.active(user.key, media.BOOK)}
    missing = [b for b in CATALOGUE if b["asin"] not in asked]
    on_order = [b for b in CATALOGUE if b["asin"] in asked]
    return {"series": name, "catalogueName": "The Saga", "seriesAsin": "SAGA",
            "region": "ca", "have": [], "onOrder": on_order, "leftOut": [],
            "notOut": [], "missing": missing, "otherVersion": [],
            "otherVersionReason": series.OTHER_VERSION_REASON,
            "rows": {b["asin"]: {"asin": b["asin"]} for b in missing}}


series.plan = fake_plan
series.want_series(KID, "Saga", follow=True)
CATALOGUE.append({"asin": "SA02", "title": "Saga 2", "key": "#2", "position": "2"})
a_day_passes()
follow.check_due()
settle()
check.equal([(p["Title"], p["Body"]) for u, p in delivered if u == KID.key],
            [("New in The Saga", "Saga 2 is out, so it has been asked for.")],
            "a new book asked for from a followed series is said")

# --- when Jellyfin cannot deliver ---------------------------------------------------------

# From here every film Kid asks for waits, whatever day it is.
store.set_cap(KID.key, media.MOVIE, 0)
delivered.clear()
jellyfin_answer["raise"] = jellyfin.PushUnavailable("This server does not send notifications.")
ask(KID, 14)
ask(KID, 15)
approvals.approve(MATT, media.MOVIE, "tmdb:14")
settle()
jellyfin_answer["raise"] = None
approvals.approve(MATT, media.MOVIE, "tmdb:15")
check.equal(bodies(KID), [],
            "a Jellyfin that cannot send is not asked again for an hour")
notices._unavailable_until = 0.0
ask(KID, 16)
approvals.approve(MATT, media.MOVIE, "tmdb:16")
check.equal(bodies(KID), ["Matt approved Film 16. It is on its way."],
            "and is asked again after it")

delivered.clear()
jellyfin_answer["raise"] = jellyfin.JellyfinUnavailable("Jellyfin did not answer")
ask(KID, 17)
answer = approvals.approve(MATT, media.MOVIE, "tmdb:17")
settle()
check.equal(answer["state"], wants.ON_ITS_WAY, "a notice that cannot be delivered costs the yes nothing")
jellyfin_answer["raise"] = None
ask(KID, 18)
approvals.approve(MATT, media.MOVIE, "tmdb:18")
check.equal(bodies(KID), ["Matt approved Film 18. It is on its way."],
            "and does not stop the next one, the way a server that cannot send does")

# --- long text is cut to what a phone takes ------------------------------------------------

delivered.clear()
notices.declined([KID.key], "Matt", "movie", "tmdb:19", "A" * 300, "B" * 900)
settle()
user_id, payload = delivered[-1]
check.that(len(payload["Title"]) <= 120 and len(payload["Body"]) <= 600,
           "title and body are cut to the lengths Jellyfin takes")

# --- a server that cannot send ------------------------------------------------------------

config.REQUEST_NOTICES = False
check.equal(client.get("/api/v1/capabilities", headers=as_kid).json()["requestNotices"],
            {"supported": False, "on": False},
            "a server that cannot send says so, whatever the account chose")
check.equal(client.put("/api/v1/request-notices", headers=as_kid,
                       json={"on": True}).status_code, 409,
            "and refuses to have it turned on")
delivered.clear()
ask(KID, 20)
approvals.approve(MATT, media.MOVIE, "tmdb:20")
check.equal(bodies(KID), [], "and never asks Jellyfin")
config.REQUEST_NOTICES = True

# --- the call to Jellyfin itself ----------------------------------------------------------

import httpx  # noqa: E402

seen: list[httpx.Request] = []
answers = {"next": httpx.Response(200, json={"Sent": 2, "Failed": 0, "Dropped": 1})}


def transport(request):
    seen.append(request)
    answer = answers["next"]
    if isinstance(answer, Exception):
        raise answer
    return answer


jellyfin._client = lambda: httpx.Client(base_url="http://jellyfin.invalid",
                                        headers=jellyfin._headers(),
                                        transport=httpx.MockTransport(transport))
result = real_notify_user("u-kid", {"Title": "T", "Body": "B", "Kind": "requests"})
check.equal(result, {"Sent": 2, "Failed": 0, "Dropped": 1}, "Jellyfin's counts come back")
check.equal(seen[-1].url.path, "/Podcasts/Push/Users/u-kid/Notify",
            "posted to the account's notify address")
check.that(seen[-1].headers.get("Authorization", "").startswith("MediaBrowser Token="),
           "with this service's own credential")
answers["next"] = httpx.Response(404, text="This server does not send notifications.")
check.raises(jellyfin.PushUnavailable, lambda: real_notify_user("u-kid", {}),
             "a fork that cannot send is told apart")
answers["next"] = httpx.Response(404)
check.raises(jellyfin.PushUnavailable, lambda: real_notify_user("u-kid", {}),
             "and so is a stock Jellyfin with no such address")
answers["next"] = httpx.Response(404, json={"title": "Not Found", "detail": "No such user."})
check.raises(LookupError, lambda: real_notify_user("u-gone", {}),
             "an account Jellyfin does not have is a different answer")
answers["next"] = httpx.Response(500, text="boom")
check.raises(jellyfin.JellyfinUnavailable, lambda: real_notify_user("u-kid", {}),
             "a server error is Jellyfin not answering")
answers["next"] = httpx.ConnectError("refused")
check.raises(jellyfin.JellyfinUnavailable, lambda: real_notify_user("u-kid", {}),
             "and so is no connection at all")

harness.cleanup()
raise SystemExit(check.report())
