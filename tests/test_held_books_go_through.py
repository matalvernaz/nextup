"""Books waiting for a keyholder go through by themselves once they fit.

Books keep their own request path and allowance arithmetic, and the rest of a
series tap waits as one ask. So the rule has to hold there separately: a
waiting book goes when the day has room, and a waiting series goes a few
books at a time, never past the books the tap that started it would have
asked for.
"""
import threading

import harness

harness.setup(BOOK_DAILY_CAP="2", SERIES_WANT_LIMIT="10", APPROVALS="on",
              APPROVAL_NOTIFY_URL="http://notify.invalid/approvals")

from app import approvals, held, jellyfin, listenarr, media, store  # noqa: E402
from app.books import series, shelves  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

check = harness.Check("held books go through by themselves")
store.init()

MATT = jellyfin.User(id="u-matt", name="Matt", is_admin=True)
READER = jellyfin.User(id="u-reader", name="Reader", is_admin=False)
ACCOUNTS = [MATT, READER]
jellyfin.accounts = lambda: list(ACCOUNTS)
jellyfin.account = lambda reference: next(
    account for account in ACCOUNTS if account.id == reference)

BOOKS = {media.BOOK: media.Medium(media.BOOK, "Books", ("book", "series"), 2,
                                  ("lib-books",))}
media.available = lambda: dict(BOOKS)
media.get = lambda medium: BOOKS.get(medium)

shelves.owned_index = lambda user: (set(), {})
shelves.forget_asin = lambda asin, user_key=None: None
added: list[str] = []


def fake_add(asin, monitored=True, metadata=None):
    added.append(asin)
    return listenarr.AddResult(True, "Sent to Listenarr", 40 + len(added),
                               title=f"Book {asin}", authors=("An Author",))


listenarr.add = fake_add
listenarr.enqueue_search = lambda audiobook_id: True
listenarr.metadata_from_search_row = lambda row, region=None: {"asin": row["asin"]}
notices: list[str] = []
held._post = lambda url, body, headers: notices.append(body)


def settle_notices():
    for thread in threading.enumerate():
        if thread.name == "approval-notice":
            thread.join(5)


def a_day_passes():
    with store.db() as conn:
        conn.execute("UPDATE requests SET requested_at = requested_at - 90000")


# --- one book ----------------------------------------------------------------

for asin in ("B01", "B02"):
    book_wants.want(READER, asin, f"Title {asin}", over_limit=True)
state, message = book_wants.want(READER, "B03", "Title B03",
                                 metadata={"asin": "B03"}, over_limit=True)
check.equal(state, held.WAITING_FOR_APPROVAL, "a third book waits")
check.that("goes through by itself" in message,
           f"and the reader is told it goes through by itself later: {message!r}")
check.equal(approvals.release_when_room(), [], "the same day it stays waiting")
a_day_passes()
went = approvals.release_when_room()
check.equal([w["itemKey"] for w in went], ["B03"], "a day later it goes through")
check.equal(added[-1], "B03", "handed to Listenarr")
row = store.get(READER.key, media.BOOK, "B03")
check.equal((row["cost"], row["allowance"]), (1, ""),
            "charged to the reader's ordinary allowance")
check.equal(book_wants.allowance(READER), 1, "one of the day's two")

# --- the rest of a series, a few books at a time ----------------------------

PLANNED = [{"asin": f"T{n:02d}", "title": f"Tome {n}", "key": f"tome-{n}",
            "position": str(n)} for n in range(1, 15)]


def fake_plan(user, name, anchor_item_id=None):
    asked = set(added)
    missing = [c for c in PLANNED if c["asin"] not in asked]
    return {"series": name, "catalogueName": "The Tomes", "seriesAsin": "TOMES",
            "region": "us", "have": [], "onOrder": [], "leftOut": [],
            "notOut": [], "missing": missing, "otherVersion": [],
            "otherVersionReason": series.OTHER_VERSION_REASON,
            "rows": {c["asin"]: {"asin": c["asin"]} for c in missing}}


series.plan = fake_plan
a_day_passes()
outcome = series.want_series(READER, "Tomes", over_limit=True)
settle_notices()
check.equal((len(outcome["requested"]), outcome["waitingForApprovalCount"]),
            (2, 8), "a tap of the fourteen-book series asks for two and holds eight")
check.that("they go through by themselves" in outcome["message"],
           f"and says they go through by themselves later: {outcome['message']!r}")
asked_at = store.held(READER.key, media.BOOK, "Tomes")["asked_at"]

released: list[str] = []
for day in range(1, 6):
    a_day_passes()
    before = len(added)
    went = approvals.release_when_room()
    released += added[before:]
    row = store.held(READER.key, media.BOOK, "Tomes")
    if row is None:
        break
    hit = held.hit_of(row)
    check.equal(hit["count"], 8 - len(released),
                f"day {day}: the series ask now waits for the {8 - len(released)} left")
    check.equal(hit["titles"][:1], [f"Tome {3 + len(released)}"],
                f"day {day}: starting from the next book")
    check.equal(row["asked_at"], asked_at, f"day {day}: and keeps its place")

check.equal(released, [f"T{n:02d}" for n in range(3, 11)],
            "two a day, the eight that waited, and not one book past the tap")
check.equal(day, 4, "over four days")
check.equal(store.held(READER.key, media.BOOK, "Tomes"), None,
            "and then nothing waits")
check.equal(store.get(READER.key, media.BOOK, "T07")["cost"], 1,
            "each book charged like any ask")
a_day_passes()
before = len(added)
approvals.release_when_room()
check.equal(added[before:], [], "the next day nothing more is asked for")

# --- a declined series stays declined ----------------------------------------

PLANNED[:] = [{"asin": f"V{n:02d}", "title": f"Volume {n}", "key": f"vol-{n}",
               "position": str(n)} for n in range(1, 6)]
a_day_passes()
series.want_series(READER, "Volumes", over_limit=True)
approvals.decline(MATT, media.BOOK, "Volumes", "Finish the Tomes first.")
a_day_passes()
before = len(added)
approvals.release_when_room()
check.equal(added[before:], [], "a declined series is never asked for by itself")

harness.cleanup()
raise SystemExit(check.report())
