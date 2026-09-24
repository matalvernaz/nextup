"""A requested book's own year and ASIN, onto the item that fulfilled it.

Death Has Joined the Party was asked for on 2026-09-24 and arrived tagged
`date=2000` beside a (P)2026 copyright; the library showed the year 2000. This
service had the edition in hand the whole time. These pin what it now does with
it, and the things it must not do: stamp a locked item, stamp another edition,
or stamp a book that only shares a series prefix with the one asked for.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("books-stamp")

from app import jellyfin
from app.books import audible, stamp, store

store.init()

check = harness.Check("book stamp")

MINUTES = 600_000_000

PRODUCTS = {
    "B0H37CTBWV": {"asin": "B0H37CTBWV", "release_date": "2026-05-28",
                   "runtime_length_min": 1440},
    "B0MOANA": {"asin": "B0MOANA", "release_date": "2026-07-23",
                "runtime_length_min": 127},
    "B0ANNOUNCED": {"asin": "B0ANNOUNCED", "release_date": "2200-01-01",
                    "runtime_length_min": 185},
}
audible.product = lambda asin: PRODUCTS.get(asin)

LIBRARY: dict[str, dict] = {}
UPDATES: list[dict] = []


def shelve(item_id, name, authors, minutes, year=2000, ids=None, locked=False):
    LIBRARY[item_id] = {
        "Id": item_id, "Name": name, "RunTimeTicks": minutes * MINUTES,
        "People": [{"Name": a, "Type": "Author"} for a in authors],
        "ProductionYear": year, "PremiereDate": f"{year}-01-01T00:00:00.0000000Z",
        "ProviderIds": dict(ids or {}), "LockData": locked,
        "Overview": "kept as it was",
    }


def books_named(title):
    words = title.lower().split()[:3]
    return [dict(item) for item in LIBRARY.values()
            if all(w in item["Name"].lower() for w in words)]


jellyfin.books_named = books_named
jellyfin.item_record = lambda item_id: (
    dict(LIBRARY[item_id]) if item_id in LIBRARY else None)
jellyfin.update_item = lambda record: UPDATES.append(record)

# --- the case it exists for -------------------------------------------------
shelve("dhjtp", "Death Has Joined the Party: A LitRPG Dungeon Crawl "
       "(Mana Runners, Book 1) (Unabridged)", ["Rachel Aaron, Travis Bach"], 1439)
outcome = stamp.stamp_one("B0H37CTBWV", "Death Has Joined the Party",
                          ["Rachel Aaron", "Travis Bach"])
check.equal(outcome, "stamped", "a placeholder year is replaced")
check.equal(len(UPDATES), 1, "with one write")
written = UPDATES[-1]
check.equal(written["ProductionYear"], 2026, "the edition's year")
check.equal(written["PremiereDate"][:10], "2026-05-28", "and its release date")
check.equal(written["ProviderIds"], {},
            "and no Audible id, which would let the fork's provider rewrite "
            "the series on every refresh")
check.equal(written["Overview"], "kept as it was",
            "the rest of the record goes back as it came")
check.equal(written["LockData"], False, "and the item is not locked")
check.equal(store.get_stamp("B0H37CTBWV")["outcome"], "stamped", "and recorded")

# --- what it leaves alone ---------------------------------------------------
UPDATES.clear()
LIBRARY["dhjtp"].update(written)
check.equal(stamp.stamp_one("B0H37CTBWV", "Death Has Joined the Party",
                            ["Rachel Aaron", "Travis Bach"]),
            "already-right", "a right year with a date needs nothing")
check.equal(UPDATES, [], "and nothing is written")

LIBRARY.clear()
shelve("locked", "Death Has Joined the Party", ["Rachel Aaron"], 1439, locked=True)
check.equal(stamp.stamp_one("B0H37CTBWV", "Death Has Joined the Party",
                            ["Rachel Aaron"]),
            "locked", "a locked item is somebody's decision")
check.equal(UPDATES, [], "and is not written")

LIBRARY.clear()
shelve("drama", "Death Has Joined the Party", ["Rachel Aaron"], 190)
check.equal(stamp.stamp_one("B0H37CTBWV", "Death Has Joined the Party",
                            ["Rachel Aaron"]),
            "other-edition",
            "hours apart is another edition, and its year is not this one's")
check.equal(UPDATES, [], "and is not written")

LIBRARY.clear()
shelve("tagged", "Death Has Joined the Party", ["Rachel Aaron"], 1439,
       ids={"Audible": "B0OTHERSTORE"})
check.equal(stamp.stamp_one("B0H37CTBWV", "Death Has Joined the Party",
                            ["Rachel Aaron"]), "stamped", "the year still comes")
check.equal(UPDATES[-1]["ProviderIds"], {"Audible": "B0OTHERSTORE"},
            "an Audible id already there is left as it is")

# Jellyfin writes an unset date as the year 1; that is no date.
UPDATES.clear()
LIBRARY.clear()
shelve("unset", "Death Has Joined the Party", ["Rachel Aaron"], 1439, year=2026)
LIBRARY["unset"]["PremiereDate"] = "0001-01-01T00:00:00.0000000Z"
check.equal(stamp.stamp_one("B0H37CTBWV", "Death Has Joined the Party",
                            ["Rachel Aaron"]), "stamped", "an unset date is filled")
check.equal(UPDATES[-1]["PremiereDate"][:10], "2026-05-28", "with the release date")

# A year or two out is the print edition against the audio release, not an
# error, and is not churned.
UPDATES.clear()
LIBRARY.clear()
shelve("close", "Death Has Joined the Party", ["Rachel Aaron"], 1439, year=2025)
check.equal(stamp.stamp_one("B0H37CTBWV", "Death Has Joined the Party",
                            ["Rachel Aaron"]), "already-right",
            "a year within two of the edition's is left alone")
check.equal(UPDATES, [], "and nothing is written")

# --- a shared series prefix is not the book ---------------------------------
UPDATES.clear()
LIBRARY.clear()
shelve("belle", "Disney Princess: Belle and the Rose Riddle",
       ["Disney Press", "Juliana Schiavo"], 156, year=2024)
check.equal(stamp.stamp_one("B0MOANA", "Disney Princess: Moana and Tales from Motunui",
                            ["Disney Press", "Suzanne Francis"]),
            "not-found", "Belle is not the Moana book that was asked for")
check.equal(UPDATES, [], "and is not given Moana's year")

# --- an announced volume has no year to give ------------------------------
check.equal(stamp.stamp_one("B0ANNOUNCED", "Something Announced", ["Anyone"]),
            "no-date", "Audible's placeholder date is not a year")

# --- the sweep, which is also the backfill ---------------------------------
UPDATES.clear()
LIBRARY.clear()
jellyfin.all_users = lambda: {}
with store.db() as conn:
    conn.execute("DELETE FROM book_stamps")
    for asin, title, authors in (
            ("B0H37CTBWV", "Death Has Joined the Party", '["Rachel Aaron"]'),
            ("B0MOANA", "Disney Princess: Moana and Tales from Motunui",
             '["Disney Press"]')):
        conn.execute(
            "INSERT INTO requests(user_key,medium,item_key,unit,title,authors,"
            "requested_at,fulfilled_at) VALUES(?,?,?,?,?,?,?,?)",
            ("matt", "book", asin, "book", title, authors, time.time(), time.time()))
shelve("dhjtp", "Death Has Joined the Party", ["Rachel Aaron"], 1439)
check.equal(stamp.sweep(), 1, "the sweep stamps what arrived before stamping existed")
check.equal(store.get_stamp("B0MOANA")["outcome"], "not-found",
            "and records the one it could not find")
check.equal(stamp.sweep(), 0, "a stamped book is not stamped again")
check.equal(store.get_stamp("B0H37CTBWV")["attempts"], 1, "nor even looked at")
check.equal(store.get_stamp("B0MOANA")["attempts"], 2,
            "an unfinished one is tried again")
for _ in range(stamp.MAX_ATTEMPTS + 2):
    stamp.sweep()
check.equal(store.get_stamp("B0MOANA")["attempts"], stamp.MAX_ATTEMPTS,
            "until it has had its tries")

# --- arrival in a test process starts nothing -------------------------------
started = []
real_thread = stamp.Thread
stamp.Thread = lambda *a, **k: started.append(1) or real_thread(target=lambda: None)
stamp.soon([{"asin": "B0H37CTBWV", "title": "x", "authors": []}])
check.equal(started, [], "only the running service stamps on arrival")
stamp.Thread = real_thread

harness.discard(DB_PATH)
raise SystemExit(check.report())
