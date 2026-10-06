"""Books past the daily allowance: one book, and the rest of a series tap.

Books keep their own request path and their own allowance arithmetic, so the
rule has to hold there separately: up to the limit a book goes through, past it
the book waits for a keyholder, and the rest of a series tap waits as one ask
rather than being left for tomorrow.
"""
import threading

import harness

harness.setup(BOOK_DAILY_CAP="2", SERIES_WANT_LIMIT="10", APPROVALS="on",
              APPROVAL_NOTIFY_URL="http://notify.invalid/approvals")

from app import approvals, config, held, jellyfin, listenarr, media, store, wants  # noqa: E402
from app.books import adapter, series, shelves  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

check = harness.Check("approvals for books")
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
added: list[tuple] = []


def fake_add(asin, monitored=True, metadata=None):
    added.append((asin, metadata))
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


# --- one book ----------------------------------------------------------------

for asin in ("B01", "B02"):
    check.equal(book_wants.want(READER, asin, f"Title {asin}", over_limit=True)[0],
                book_wants.ON_ITS_WAY, f"book {asin} is within the allowance")
check.raises(book_wants.AllowanceExhausted,
             lambda: book_wants.want(READER, "B03", "Title B03"),
             "a third book without saying so is refused, as before")
state, message = book_wants.want(READER, "B03", "Title B03",
                                 metadata={"asin": "B03", "region": "us"},
                                 over_limit=True)
settle_notices()
check.equal(state, held.WAITING_FOR_APPROVAL, "past the limit a book waits")
check.that("past today's limit for books" in message and "Matt" in message,
           f"and says who it went to: {message!r}")
check.equal([asin for asin, _ in added], ["B01", "B02"],
            "and nothing more reached Listenarr")
check.equal(len(notices), 1, "whoever approves is told")

answer = approvals.approve(MATT, media.BOOK, "B03")
check.equal(answer["approvedFor"], ["Reader"], "approving it is for the reader")
check.equal(added[-1], ("B03", {"asin": "B03", "region": "us"}),
            "and hands Listenarr the book with the listing it was asked from")
row = store.get(READER.key, media.BOOK, "B03")
check.equal((row["cost"], row["allowance"]), (0, store.APPROVED_POOL),
            "recorded free, under its own pool")
check.equal(book_wants.allowance(READER), 0,
            "the reader's allowance is what it was, not less")

book_wants.want(READER, "B04", "Title B04", over_limit=True)
removed, _ = book_wants.cancel(READER, "B04")
check.that(removed and store.held(READER.key, media.BOOK, "B04") is None,
           "a waiting book can be withdrawn")
check.equal(added[-1][0], "B03", "without anything reaching Listenarr")

# --- the rest of a series tap ------------------------------------------------

store.reset_allowance(READER.key, media.BOOK)
PLANNED = [{"asin": f"S{n:02d}", "title": f"Saga {n}", "key": f"saga-{n}",
            "position": str(n)} for n in range(1, 9)]


def fake_plan(user, name, anchor_item_id=None):
    asked = {a for a, _ in added}
    missing = [c for c in PLANNED if c["asin"] not in asked]
    return {"series": name, "catalogueName": "The Saga", "seriesAsin": "SAGA",
            "region": "us", "have": [], "onOrder": [], "leftOut": [],
            "notOut": [], "missing": missing, "otherVersion": [],
            "otherVersionReason": series.OTHER_VERSION_REASON,
            "rows": {c["asin"]: {"asin": c["asin"]} for c in missing}}


series.plan = fake_plan
before = len(added)
outcome = series.want_series(READER, "Saga", "anchor-1", over_limit=True)
settle_notices()
check.equal([c["asin"] for c in outcome["requested"]], ["S01", "S02"],
            "the books that fit in the allowance are asked for")
check.equal(outcome["waitingForApprovalCount"], 6,
            "the rest of the tap waits for a keyholder")
check.that("the other 6 books have gone to Matt to approve" in outcome["message"],
           f"and the sentence says so: {outcome['message']!r}")
check.that("tomorrow" not in outcome["message"],
           "rather than telling them to wait for tomorrow")
waiting = store.held(READER.key, media.BOOK, "Saga")
check.that(waiting is not None and waiting["unit"] == "series",
           "as one ask for the series, not one per book")
check.equal(len(added) - before, 2, "and only the two reached Listenarr")
announced = len(notices)
check.equal(adapter.want(READER, "Saga", "series", {}, over_limit=True)[0],
            held.WAITING_FOR_APPROVAL,
            "asked again with nothing left today, the series is still waiting")
settle_notices()
check.equal(len(notices), announced, "and is not announced a second time")

entry = approvals.waiting()[0]
check.equal((entry["unit"], entry.get("bookCount")), ("series", 6),
            "the keyholder sees six books from a series")
check.equal(entry.get("bookTitles"),
            ["Saga 3", "Saga 4", "Saga 5", "Saga 6", "Saga 7", "Saga 8"],
            "named")

answer = approvals.approve(MATT, media.BOOK, "Saga")
check.equal([asin for asin, _ in added[-6:]],
            ["S03", "S04", "S05", "S06", "S07", "S08"],
            "approving asks for exactly the books that were waiting")
check.equal(store.held(READER.key, media.BOOK, "Saga"), None,
            "and the series ask is answered")
check.equal(store.get(READER.key, media.BOOK, "S05")["cost"], 0,
            "each of them free")

# A tap past the per-tap limit leaves the rest for another tap, not for a
# keyholder: approving is only ever about the allowance.
PLANNED[:] = [{"asin": f"T{n:02d}", "title": f"Tome {n}", "key": f"tome-{n}",
               "position": str(n)} for n in range(1, 15)]
store.reset_allowance(READER.key, media.BOOK)
outcome = series.want_series(READER, "Tomes", over_limit=True)
check.equal((len(outcome["requested"]), outcome["waitingForApprovalCount"]),
            (2, 8), "two fit, eight wait: the tap's limit of ten")
check.that("4 more books not asked for yet" in outcome["message"],
           f"and the four past the tap are left for the next one: {outcome['message']!r}")
before = len(added)
approvals.approve(MATT, media.BOOK, "Tomes")
check.equal([asin for asin, _ in added[before:]],
            [f"T{n:02d}" for n in range(3, 11)],
            "approving asks for the eight that waited, not the tap's ten")

config.APPROVALS = False
store.reset_allowance(READER.key, media.BOOK)
PLANNED[:] = [{"asin": f"U{n:02d}", "title": f"Volume {n}", "key": f"vol-{n}",
               "position": str(n)} for n in range(1, 5)]
outcome = series.want_series(READER, "Volumes", over_limit=True)
check.equal(outcome["waitingForApprovalCount"], 0,
            "a server that does not hold asks leaves the rest for tomorrow")
check.that("tomorrow" in outcome["message"], "and says so, as before")
config.APPROVALS = True

harness.cleanup()
raise SystemExit(check.report())
