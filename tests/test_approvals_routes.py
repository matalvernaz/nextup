"""Who may approve, and what clients and pages are told about waiting asks.

The two halves of the contract: an older client never sees anything new (no
held asks in its list, a refusal at the limit as before), and a client that
says it can show a waiting request gets one. Approving and declining are for
keyholders only, on the API and on the pages alike.
"""
import re

import harness

harness.setup(RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
              RADARR_QUALITY_PROFILE_ID="4", MOVIE_DAILY_CAP="1",
              APPROVALS="on", JELLYFIN_USER="")

from fastapi import FastAPI  # noqa: E402  (after harness.setup)
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, arr, compat_nextread, held, jellyfin, main, media,  # noqa: E402
                 radarr, sessions, store)

check = harness.Check("approval routes")
store.init()

MATT = jellyfin.User(id="u-matt", name="Matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="Kid", is_admin=False)
ACCOUNTS = [MATT, KID]
TOKENS = {"matt-token": MATT, "kid-token": KID}
jellyfin.accounts = lambda: list(ACCOUNTS)
jellyfin.account = lambda reference: next(
    (a for a in ACCOUNTS if a.id == reference or a.name == reference), None) or (
    _ for _ in ()).throw(LookupError(reference))
jellyfin.user_from_token = lambda token: TOKENS[token]
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: ["lib-books"] if medium == "book" else []

FILMS = {media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 1,
                                   ("lib-movies",))}
media.available = lambda: dict(FILMS)
media.get = lambda medium: FILMS.get(medium)
media.owned = lambda force=False: jellyfin.Owned()
added: list[str] = []
radarr.add = lambda tmdb, title="", year="", monitored=True: (
    added.append(tmdb) or arr.AddResult(True, "Sent to Radarr.", f"r{tmdb}", title))
radarr.arrived = lambda keys, owned: set()
held._post = lambda url, body, headers: None

app = FastAPI()
app.include_router(api.router)
app.include_router(compat_nextread.router)
client = TestClient(app, raise_server_exceptions=False)


def as_(token):
    return {"X-Emby-Token": token}


def want(token, n, **extra):
    return client.post("/api/v1/want", headers=as_(token), json={
        "medium": "movie", "itemKey": f"tmdb:{n}", "title": f"Film {n}", **extra})


# --- capabilities ------------------------------------------------------------

caps = client.get("/api/v1/capabilities", headers=as_("kid-token")).json()
check.equal(caps["approvals"]["supported"], True, "a member is told asks are held")
check.equal(caps["approvals"]["approvers"], ["Matt"], "and who approves them")
check.equal("waiting" in caps["approvals"], False,
            "but not how many are waiting, which is a keyholder's")
check.equal(caps["states"], ["on_its_way", "still_looking", "in_library"],
            "the pinned three states are unchanged for older clients")

# --- an ask past the limit ---------------------------------------------------

check.equal(want("kid-token", 1).status_code, 200, "the first film goes through")
refused = want("kid-token", 2)
check.equal(refused.status_code, 409,
            "a client that does not say it can show a waiting ask is refused")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:2"), None,
            "and nothing waits for it")
waiting = want("kid-token", 2, overLimit="request")
check.equal(waiting.status_code, 200, "a client that says so is answered")
check.equal(waiting.json()["state"], "waiting_for_approval",
            "with the ask waiting for approval")
check.equal(added, ["1"], "and nothing more handed to Radarr")

plain = client.get("/api/v1/requests", headers=as_("kid-token")).json()
check.equal([r["itemKey"] for r in plain["requests"]], ["tmdb:1"],
            "an older client's list leaves the waiting ask out")
listed = client.get("/api/v1/requests?held=true", headers=as_("kid-token")).json()
check.equal({r["itemKey"]: r["state"] for r in listed["requests"]},
            {"tmdb:1": "on_its_way", "tmdb:2": "waiting_for_approval"},
            "and a client that asks for held asks sees it waiting")

caps = client.get("/api/v1/capabilities", headers=as_("matt-token")).json()
check.equal(caps["approvals"]["waiting"], 1, "a keyholder is told one is waiting")
compat = client.get("/nextread/api/v1/capabilities", headers=as_("kid-token"))
check.equal(compat.json().get("approvals", {}).get("supported"), True,
            "and the book routes say asks are held too")

# --- who may answer ----------------------------------------------------------

check.equal(client.get("/api/v1/approvals", headers=as_("kid-token")).status_code,
            403, "a member may not read the waiting list")
body = {"medium": "movie", "itemKey": "tmdb:2"}
check.equal(client.post("/api/v1/approvals/approve", headers=as_("kid-token"),
                        json=body).status_code, 403, "or approve their own ask")
check.equal(client.post("/api/v1/approvals/decline", headers=as_("kid-token"),
                        json=body).status_code, 403, "or decline one")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:2")["state"], "waiting",
            "and the refused calls changed nothing")

listing = client.get("/api/v1/approvals", headers=as_("matt-token"))
check.equal(listing.status_code, 200, "a keyholder reads the waiting list")
entries = listing.json()["approvals"]
check.equal([(e["itemKey"], [w["name"] for w in e["askedBy"]]) for e in entries],
            [("tmdb:2", ["Kid"])], "naming who asked for what")

approved = client.post("/api/v1/approvals/approve", headers=as_("matt-token"),
                       json=body)
check.equal(approved.status_code, 200, "a keyholder approves it")
check.equal(approved.json()["approvedFor"], ["Kid"], "for Kid")
check.equal(added, ["1", "2"], "and it reaches Radarr")
check.equal(client.post("/api/v1/approvals/approve", headers=as_("matt-token"),
                        json=body).status_code, 404,
            "approving it again finds nothing waiting")

want("kid-token", 3, overLimit="request")
declined = client.post("/api/v1/approvals/decline", headers=as_("matt-token"),
                       json={"medium": "movie", "itemKey": "tmdb:3",
                             "reason": "Not this one."})
check.equal(declined.status_code, 200, "a keyholder declines one")
listed = client.get("/api/v1/requests?held=true", headers=as_("kid-token")).json()
row = next(r for r in listed["requests"] if r["itemKey"] == "tmdb:3")
check.equal((row["state"], row.get("reason"), row.get("decidedBy")),
            ("declined", "Not this one.", "Matt"),
            "and the asker sees the no, why, and from whom")
withdrawn = client.post("/api/v1/cancel", headers=as_("kid-token"),
                        json={"medium": "movie", "itemKey": "tmdb:3"})
check.equal(withdrawn.status_code, 200, "the asker can clear the no away")

# --- the pages ---------------------------------------------------------------

pages = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
pages.cookies.set(sessions.COOKIE_NAME, sessions.issue("kid-token", KID.id))
page = pages.post("/want", data={"medium": "movie", "item_key": "tmdb:4",
                                 "title": "Film 4"})
check.equal(page.status_code, 303, "asking on the page answers with the list")
check.that("Matt" in page.headers["location"].replace("%20", " "),
           "and says who the ask went to")
index = pages.get("/").text
check.that("so it is waiting for Matt to approve it" in index,
           "the page lists the waiting ask, and who it waits for")
check.that("Withdraw your request for Film 4" in index,
           "with a button named for withdrawing it")
check.that("Past a limit, what you ask for waits for Matt to approve it" in index,
           "and says what happens past a limit")
check.equal(pages.get("/approvals").status_code, 403,
            "a member is told the approvals page is not theirs")
check.equal(pages.post("/approvals/approve", data={
    "medium": "movie", "item_key": "tmdb:4"}).status_code, 303,
            "a member's approval is turned away")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:4")["state"], "waiting",
            "and changes nothing")

keyholder = TestClient(main.app, raise_server_exceptions=False,
                       follow_redirects=False)
keyholder.cookies.set(sessions.COOKIE_NAME, sessions.issue("matt-token", MATT.id))
listing = keyholder.get("/approvals")
check.equal(listing.status_code, 200, "a keyholder opens the approvals page")
text = listing.text
check.that("Requests to approve, 1 waiting" in text,
           "the link to it carries the count")
check.that("Approve Film 4" in text and "Decline Film 4" in text,
           "each ask has buttons named for it")
check.that(re.search(r"Asked for by\s+Kid today at \d\d:\d\d\.", text) is not None,
           "and says who asked, and when")
done = keyholder.post("/approvals/decline", data={
    "medium": "movie", "item_key": "tmdb:4", "reason": "Too long."})
check.equal(done.status_code, 303, "declining on the page goes back to it")
check.that("Declined%20for%20Kid" in done.headers["location"],
           "saying who it was declined for")
check.that("Nothing is waiting for approval." in keyholder.get("/approvals").text,
           "and the page is empty again")

harness.cleanup()
raise SystemExit(check.report())
