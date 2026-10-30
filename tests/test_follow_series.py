"""A book series asked for whole is followed, and its new books asked for.

Matt, 2026-10-30: "If I ask for an entire series, nothing realizes if another
book has come out and it should be looked for." Asking for a whole series now
follows it, and a daily look asks for any book that came out since, on the
follower's behalf and charged like any ask of theirs. Books that were out
already are never asked for by it: those are the ask's business. A no from a
keyholder, or taking the rest of the series back, ends the follow.
"""
import threading

import harness

harness.setup(BOOK_DAILY_CAP="3", SERIES_WANT_LIMIT="10", APPROVALS="on",
              APPROVAL_NOTIFY_URL="http://notify.invalid/approvals",
              JELLYFIN_USER="", LISTENARR_URL="http://listenarr.invalid",
              LISTENARR_API_KEY="k")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, approvals, compat_nextread, config, held,  # noqa: E402
                 jellyfin, listenarr, main, media, sessions, store)
from app.books import adapter, follow, series, shelves  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

check = harness.Check("followed book series")
store.init()

MATT = jellyfin.User(id="u-matt", name="Matt", is_admin=True)
READER = jellyfin.User(id="u-reader", name="Reader", is_admin=False)
OTHER = jellyfin.User(id="u-other", name="Other", is_admin=False)
ACCOUNTS = [MATT, READER, OTHER]
TOKENS = {"matt-token": MATT, "reader-token": READER, "other-token": OTHER}
jellyfin_down = {"now": False}


def fake_accounts():
    if jellyfin_down["now"]:
        raise jellyfin.JellyfinUnavailable("Jellyfin is not answering")
    return list(ACCOUNTS)


jellyfin.accounts = fake_accounts
jellyfin.account = lambda reference: next(
    a for a in ACCOUNTS if a.id == reference or a.name == reference)
jellyfin.user_from_token = lambda token: TOKENS[token]
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: ["lib-books"] if medium == "book" else []
jellyfin.books = lambda user_id: []

BOOKS = {media.BOOK: media.Medium(media.BOOK, "Books", ("book", "series"), 3,
                                  ("lib-books",))}
media.available = lambda: dict(BOOKS)
media.get = lambda medium: BOOKS.get(medium)

shelves.owned_index = lambda user: (set(), {})
shelves.forget_asin = lambda asin, user_key=None: None
added: list[str] = []
listenarr_refuses: set[str] = set()


def fake_add(asin, monitored=True, metadata=None):
    if asin in listenarr_refuses:
        return listenarr.AddResult(False, "Listenarr would not take it.", None)
    added.append(asin)
    return listenarr.AddResult(True, "Sent to Listenarr", 40 + len(added),
                               title=f"Book {asin}", authors=("An Author",))


listenarr.add = fake_add
listenarr.enqueue_search = lambda audiobook_id: True
listenarr.metadata_from_search_row = lambda row, region=None: {"asin": row["asin"]}
notices: list[str] = []
held._post = lambda url, body, headers: notices.append(body)

#: The catalogue: per series, the books Audible lists, in order. `out` False is
#: announced but not published yet.
CATALOGUE: dict[str, list[dict]] = {}
#: Raised by the next plan of a series, to stand for the catalogue failing.
plan_fails: dict[str, Exception] = {}


def book(prefix, n, out=True):
    return {"asin": f"{prefix}{n:02d}", "title": f"{prefix} {n}", "key": f"#{n}",
            "position": str(n), "out": out}


def fake_plan(user, name, anchor_item_id=None, library=None):
    if name in plan_fails:
        raise plan_fails[name]
    asked = set(added)
    listed = CATALOGUE[name]
    have = [b for b in listed if b.get("owned")]
    not_out = [b for b in listed if not b["out"] and not b.get("owned")]
    on_order = [b for b in listed if b["out"] and b["asin"] in asked]
    missing = [b for b in listed if b["out"] and not b.get("owned")
               and b["asin"] not in asked]
    return {"series": name, "catalogueName": f"The {name}", "seriesAsin": name.upper(),
            "region": "ca", "have": have, "onOrder": on_order, "leftOut": [],
            "notOut": not_out, "missing": missing, "otherVersion": [],
            "otherVersionReason": series.OTHER_VERSION_REASON,
            "rows": {b["asin"]: {"asin": b["asin"]} for b in missing}}


series.plan = fake_plan


def settle_notices():
    for thread in threading.enumerate():
        if thread.name == "approval-notice":
            thread.join(5)


def a_day_passes():
    """Every follow due a look again, and every charge out of today's allowance."""
    with store.db() as conn:
        conn.execute("UPDATE followed_series SET checked_at = checked_at - 90000")
        conn.execute("UPDATE requests SET requested_at = requested_at - 90000")


def follow_of(user, name):
    return next((row for row in store.followed(user.key) if row["series"] == name), None)


def look():
    """One pass, with anything it raises recorded as a failure, not a crash."""
    try:
        return follow.check_due()
    except Exception as exc:  # noqa: BLE001 - raising is the defect here.
        check.that(False, f"the pass raised {exc!r} instead of carrying on")
        return None


# --- asking for a whole series follows it --------------------------------------

CATALOGUE["Saga"] = [book("SA", 1), book("SA", 2), book("SA", 3), book("SA", 4, out=False)]
outcome = series.want_series(READER, "Saga", "anchor-1", over_limit=True, follow=True)
check.equal([b["asin"] for b in outcome["requested"]], ["SA01", "SA02", "SA03"],
            "the three books that are out are asked for")
check.equal(outcome["following"], True, "and the series is followed")
check.that(outcome["message"].endswith(series.FOLLOWING_SENTENCE),
           f"which the answer says: {outcome['message']!r}")
row = follow_of(READER, "Saga")
check.equal(row["known"], ["#1", "#2", "#3"],
            "what was out is known; the announced fourth book is not")
check.equal((row["anchor_item_id"], row["title"]), ("anchor-1", "The Saga"),
            "with the book it was asked from and the series' own name")

checked = follow_of(READER, "Saga")["checked_at"]
CATALOGUE["Saga"][3]["out"] = True
check.equal(look(), [], "looked at again the same day, nothing is due")
check.equal(follow_of(READER, "Saga")["checked_at"], checked,
            "so it is not looked at at all, even with a book newly out")
CATALOGUE["Saga"][3]["out"] = False

a_day_passes()
check.equal(look(), [], "a day later, with the fourth book still not out, nothing is asked")
check.equal(follow_of(READER, "Saga")["known"], ["#1", "#2", "#3"],
            "and the fourth stays unknown, to be noticed when it is out")

# --- a book comes out --------------------------------------------------------

CATALOGUE["Saga"][3]["out"] = True
a_day_passes()
asked = look()
check.equal([(a["asin"], a["state"]) for a in asked or []], [("SA04", "on_its_way")],
            "the day the fourth book is out, it is asked for")
check.equal(added[-1], "SA04", "handed to Listenarr")
request = store.get(READER.key, media.BOOK, "SA04")
check.equal((request["cost"], request["allowance"]), (1, ""),
            "charged to the reader's allowance like any ask")
check.equal(follow_of(READER, "Saga")["known"], ["#1", "#2", "#3", "#4"],
            "and is known from then on")
a_day_passes()
check.equal(look(), [], "the next day nothing more is asked for")

CATALOGUE["Saga"].append(book("SA", 5))
a_day_passes()
check.equal([a["asin"] for a in look() or []], ["SA05"],
            "a book the catalogue did not list before is asked for too")

# --- the books that were out already are left alone -----------------------------

CATALOGUE["Long"] = [book("LO", n) for n in range(1, 15)]
a_day_passes()
outcome = series.want_series(READER, "Long", over_limit=True, follow=True)
check.equal((len(outcome["requested"]), outcome["waitingForApprovalCount"]), (3, 7),
            "a fourteen-book series asks for three and holds seven")
before = list(added)
a_day_passes()
check.equal([a["asin"] for a in look() or [] if a["series"] == "Long"], [],
            "following it never asks for the books that were out at the ask")
check.equal(added, before, "so nothing reaches Listenarr from it")

# --- a new book already on its way for somebody else -----------------------------

CATALOGUE["Shared"] = [book("SH", 1)]
a_day_passes()
series.want_series(OTHER, "Shared", follow=True)
CATALOGUE["Shared"].append(book("SH", 2))
book_wants.want(MATT, "SH02", "SH 2")
a_day_passes()
before = list(added)
asked = [a for a in look() or [] if a["series"] == "Shared"]
check.equal(asked, [], "a new book already on order for the household is not asked for again")
check.equal(added, before, "nothing more reaches Listenarr")
check.that("#2" in follow_of(OTHER, "Shared")["known"], "and it is known from then on")

# --- past the day's allowance it waits for a keyholder ---------------------------

for n in (1, 2, 3):
    book_wants.want(READER, f"X{n}", f"Something else {n}")
check.equal(book_wants.allowance(READER), 0, "the reader has used the day")
CATALOGUE["Saga"].append(book("SA", 6))
with store.db() as conn:
    conn.execute("UPDATE followed_series SET checked_at = checked_at - 90000")
notices.clear()
asked = [a for a in look() or [] if a["series"] == "Saga"]
settle_notices()
check.equal([(a["asin"], a["state"]) for a in asked], [("SA06", "waiting_for_approval")],
            "a new book past the allowance waits for a keyholder")
check.that(any(follow.HELD_DETAIL in body for body in notices),
           f"whose notice says it came from a followed series: {notices!r}")
check.that("#6" in follow_of(READER, "Saga")["known"],
           "and it is known, so it is not asked for twice")

# --- too many new books at once is a catalogue change, not news ------------------

a_day_passes()
CATALOGUE["Saga"] += [book("SA", n) for n in range(7, 11)]
before = list(added)
check.equal([a for a in look() or [] if a["series"] == "Saga"], [],
            "four new books at once are not asked for")
check.equal(added, before, "nothing reaches Listenarr")
check.that("#10" in follow_of(READER, "Saga")["known"],
           "they are taken as known, so it is said once")

# --- a refusal is tried again on the next look -----------------------------------

a_day_passes()
CATALOGUE["Saga"] += [book("SA", 11), book("SA", 12)]
listenarr_refuses.add("SA11")
check.equal([a["asin"] for a in look() or [] if a["series"] == "Saga"], ["SA12"],
            "a book Listenarr refuses is not asked for, and the one beside it still is")
known = follow_of(READER, "Saga")["known"]
check.that("#11" not in known and "#12" in known,
           f"the refused one stays unknown, the other is known: {known}")
listenarr_refuses.clear()
a_day_passes()
check.equal([a["asin"] for a in look() or [] if a["series"] == "Saga"], ["SA11"],
            "the next look asks for it again, and it goes")

# --- a catalogue that cannot answer, or cannot place the series ------------------

a_day_passes()
checked = follow_of(READER, "Saga")["checked_at"]
plan_fails["Saga"] = series.Unavailable("Listenarr did not answer.")
look()
check.equal(follow_of(READER, "Saga")["checked_at"], checked,
            "Listenarr not answering leaves the series due, to be looked at next pass")
plan_fails["Saga"] = series.Unresolvable("Could not tell which Audible series Saga is.")
look()
check.that(follow_of(READER, "Saga")["checked_at"] > checked,
           "a series that cannot be placed today is looked at again tomorrow")
check.that(follow_of(READER, "Saga") is not None, "and is still followed")
del plan_fails["Saga"]

a_day_passes()
plan_fails["Long"] = RuntimeError("something nobody foresaw")
CATALOGUE["Saga"].append(book("SA", 13))
asked = look()
check.equal([a["asin"] for a in asked or [] if a["series"] == "Saga"], ["SA13"],
            "one series failing in a way nobody foresaw does not stop the next")
del plan_fails["Long"]

a_day_passes()
jellyfin_down["now"] = True
check.equal(look(), [], "with Jellyfin not answering nothing is looked at")
jellyfin_down["now"] = False

# --- a no, or taking it back, ends the follow --------------------------------------

CATALOGUE["Refused"] = [book("RF", n) for n in range(1, 6)]
for n in (1, 2, 3):
    book_wants.want(OTHER, f"Y{n}", f"Another {n}")
series.want_series(OTHER, "Refused", over_limit=True, follow=True)
check.that(follow_of(OTHER, "Refused") is not None, "a series asked for past the limit is followed")
approvals.decline(MATT, media.BOOK, "Refused", "Not this one.")
check.equal(follow_of(OTHER, "Refused"), None, "a keyholder's no to it ends the follow")

CATALOGUE["Taken"] = [book("TK", n) for n in range(1, 4)]
series.want_series(OTHER, "Taken", over_limit=True, follow=True)
removed = book_wants.cancel(OTHER, "Taken")
check.equal(follow_of(OTHER, "Taken"), None,
            "taking back the rest of a series ends the follow too")

# --- an approval or a release finishing an ask is not a new ask -------------------

CATALOGUE["Later"] = [book("LA", n) for n in range(1, 4)]
series.want_series(OTHER, "Later", over_limit=True, follow=True)
known_before = follow_of(OTHER, "Later")["known"]
CATALOGUE["Later"].append(book("LA", 4))
approvals.approve(MATT, media.BOOK, "Later")
check.equal(follow_of(OTHER, "Later")["known"], known_before,
            "approving the rest does not take a later book as already known")
a_day_passes()
check.equal([a["asin"] for a in look() or [] if a["series"] == "Later"], ["LA04"],
            "so the follow still asks for it")

# --- asking again keeps what was known -------------------------------------------

store.follow_series(OTHER.key, "Again", "", "Again", ["#1", "#2"])
store.follow_series(OTHER.key, "Again", "", "Again", ["#1", "#3"])
check.equal(follow_of(OTHER, "Again")["known"], ["#1", "#2", "#3"],
            "asking for a followed series again adds to what was known, never forgets")
store.unfollow_series(OTHER.key, "Again")

# --- asking through the request path follows too -----------------------------------

CATALOGUE["Path"] = [book("PA", 1)]
adapter.want(OTHER, "Path", "series", {}, over_limit=True)
check.that(follow_of(OTHER, "Path") is not None,
           "asking for a series through the shared request path follows it")

# --- switched off -------------------------------------------------------------------

config.FOLLOW_SERIES = False
CATALOGUE["Off"] = [book("OF", 1)]
outcome = series.want_series(OTHER, "Off", follow=True)
check.equal(outcome["following"], False, "a server that does not follow says so")
check.that(series.FOLLOWING_SENTENCE not in outcome["message"], "and promises nothing")
check.equal(follow_of(OTHER, "Off"), None, "and records nothing")
a_day_passes()
check.equal(look(), [], "and looks at nothing")
config.FOLLOW_SERIES = True

# --- the API and the page ------------------------------------------------------------

app = FastAPI()
app.include_router(api.router)
app.include_router(compat_nextread.router)
client = TestClient(app, raise_server_exceptions=False)
as_other = {"X-Emby-Token": "other-token"}
caps = client.get("/api/v1/capabilities", headers=as_other).json()
check.equal(caps.get("seriesFollow"), {"supported": True}, "capabilities say series are followed")
legacy = client.get("/nextread/api/v1/capabilities", headers=as_other).json()
check.equal(legacy.get("seriesFollow"), {"supported": True}, "and so do the book capabilities")
listed = client.get("/api/v1/series/follows", headers=as_other).json()["follows"]
check.equal(sorted(f["series"] for f in listed), ["Later", "Path", "Shared"],
            "the follows list is the account's own")
check.equal(client.post("/api/v1/series/unfollow", headers=as_other,
                        json={"series": "Path"}).status_code, 200, "one can be stopped")
check.equal(follow_of(OTHER, "Path"), None, "and is gone")
check.equal(client.post("/api/v1/series/unfollow", headers=as_other,
                        json={"series": "Path"}).status_code, 404,
            "stopping one not followed says so")
answer = client.post("/nextread/api/v1/series/want", headers=as_other,
                     json={"series": "Path"}).json()
check.equal(answer.get("following"), True, "asking for a series on the book route follows it")

pages = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
pages.cookies.set(sessions.COOKIE_NAME, sessions.issue("other-token", OTHER.id))
index = pages.get("/").text
check.that("Series you follow" in index and "Stop following The Shared" in index,
           "the page lists followed series with a button to stop each")
stopped = pages.post("/series/unfollow", data={"series": "Shared"})
check.equal(stopped.status_code, 303, "stopping from the page goes back to the list")
check.equal(follow_of(OTHER, "Shared"), None, "and stops it")

harness.cleanup()
raise SystemExit(check.report())
