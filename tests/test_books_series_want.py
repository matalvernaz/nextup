"""Asking for the rest of a series: what counts as owned, what gets asked for,
and what the sentence says about it."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("books-series-want")

harness.discard(DB_PATH)

from app import config, jellyfin, listenarr
from app.books import audible, series, store, wants

store.init()

matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)
kadija = jellyfin.User(id="user-kadija", name="kadija")

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def book(item_id, name, series_name, index, asin=None, author="Jim Butcher"):
    item = {"Id": item_id, "Name": name, "SeriesName": series_name,
            "IndexNumber": index, "People": [{"Name": author, "Type": "Author"}]}
    if asin:
        item["ProviderIds"] = {"Audible": asin}
    return item


def row(asin, title, position, series_asin, series_name, author="Jim Butcher",
        release="2020-01-01"):
    return {"asin": asin, "title": title, "authors": [{"name": author}],
            "narrators": [{"name": "A Reader"}], "lengthMinutes": 600,
            "releaseDate": release,
            "series": [{"asin": series_asin, "name": series_name, "position": position}]}


HP = "Harry Potter (Full-Cast Editions)"
LIBRARY = [
    book("j1", "Storm Front", "The Dresden Files", 1, asin="B0DF01"),
    # Owned with no Audible id: recognised by title and author.
    book("j2", "Fool Moon", "The Dresden Files", 2),
    book("j4", "Summer Knight", "The Dresden Files", 4, asin="B0DF04"),
    # Owned under a shorter title, with an author tag the household-wide check
    # cannot take for the same author. Within the series, the title decides.
    book("js", "Side Jobs", "The Dresden Files", None,
         author="Jim Butcher (The Dresden Files)"),
    book("h1", "Harry Potter and the Philosopher's Stone (Full-Cast Edition)", HP, 1,
         asin="B0HP01", author="J.K. Rowling"),
    book("h2", "Harry Potter and the Chamber of Secrets (Full-Cast Edition)", HP, 2,
         asin="B0HP02", author="J.K. Rowling"),
    # The torrented half of a library: a series none of whose books carries an id.
    book("d1", "Harry Potter and the Sorcerer's Stone, Book 1 (Jim Dale)",
         "Harry Potter (Jim Dale)", 1, author="J.K. Rowling"),
    book("l1", "Long Saga 1", "Long Saga", 1, asin="B0LS01", author="Saga Author"),
    book("x1", "Lone Book", None, None),
    # Another torrented set, whose Audible series name carries more words.
    book("d2", "Harry Potter and the Philosopher's Stone, Book 1 (Stephen Fry)",
         "Harry Potter (Stephen Fry)", 1, author="J.K. Rowling"),
    # An Audible id stored in lower case, as Jellyfin was handed it.
    book("c1", "Case Saga 1", "Case Saga", 1, asin="b0cs01", author="Case Author"),
]

PRODUCTS = {
    "B0DF01": {"title": "Storm Front", "_region": "ca",
               "series": [{"title": "The Dresden Files", "sequence": "1", "asin": "SER-DF"}]},
    "B0DF04": {"title": "Summer Knight", "_region": "ca",
               "series": [{"title": "The Dresden Files", "sequence": "4", "asin": "SER-DF"}]},
    # Audible files it under a franchise label too; the numbered one wins.
    "B0HP01": {"title": "Harry Potter and the Philosopher's Stone (Full-Cast Edition)",
               "_region": "ca",
               "series": [{"title": "Wizarding World", "asin": "SER-WW"},
                          {"title": HP, "sequence": "1", "asin": "SER-HPFC"}]},
    "B0HP02": {"title": "Harry Potter and the Chamber of Secrets (Full-Cast Edition)",
               "_region": "ca",
               "series": [{"title": HP, "sequence": "2", "asin": "SER-HPFC"}]},
    "B0LS01": {"title": "Long Saga 1", "_region": "us",
               "series": [{"title": "Long Saga", "sequence": "1", "asin": "SER-LS"}]},
}

DF = "The Dresden Files"
SERIES_BOOKS = {
    "SER-DF": [
        row("B0DF01", "Storm Front", "1", "SER-DF", DF),
        row("B0DF02X", "Fool Moon", "2", "SER-DF", DF),
        # The same book from both marketplaces: one gap, not two.
        row("B0DF03", "Grave Peril", "3", "SER-DF", DF),
        row("B0DF03US", "Grave Peril", "3", "SER-DF", DF),
        row("B0DF04", "Summer Knight", "4", "SER-DF", DF),
        row("B0DF05", "Death Masks", "5", "SER-DF", DF),
        row("B0DF06", "Blood Rites", "6", "SER-DF", DF),
        row("B0DFSS", "Side Jobs: Stories from the Dresden Files", None, "SER-DF", DF),
        # An unnumbered companion listed twice, like every numbered book.
        row("B0DFSSUS", "Side Jobs: Stories from the Dresden Files", None, "SER-DF", DF),
    ],
    "SER-HPFC": [
        row("B0HP01", "Harry Potter and the Philosopher's Stone (Full-Cast Edition)", "1",
            "SER-HPFC", HP, "J.K. Rowling"),
        # The other marketplace's title for the same book, at the same position.
        row("B0HP01US", "Harry Potter and the Sorcerer's Stone (Full-Cast Edition)", "1",
            "SER-HPFC", HP, "J.K. Rowling"),
        row("B0HP02", "Harry Potter and the Chamber of Secrets (Full-Cast Edition)", "2",
            "SER-HPFC", HP, "J.K. Rowling"),
        row("B0HP02US", "Harry Potter and the Chamber of Secrets (Full-Cast Edition)", "2",
            "SER-HPFC", HP, "J.K. Rowling"),
    ],
    "SER-LS": [row(f"B0LS0{n}", f"Long Saga {n}", str(n), "SER-LS", "Long Saga",
                   "Saga Author") for n in range(1, 8)],
    "SER-HP-ONE": [
        row("B0HPUK1", "Harry Potter and the Philosopher's Stone, Book 1", "1",
            "SER-HP-ONE", "Harry Potter", "J.K. Rowling"),
    ],
    "B0D229XM3B": [
        row("B0HPFRY1", "Harry Potter and the Philosopher's Stone, Book 1", "1",
            "B0D229XM3B", "Harry Potter (Narrated by Stephen Fry)", "J.K. Rowling"),
        row("B0HPFRY2", "Harry Potter and the Chamber of Secrets, Book 2", "2",
            "B0D229XM3B", "Harry Potter (Narrated by Stephen Fry)", "J.K. Rowling"),
    ],
    "SER-CS": [
        row("B0CS01", "Case Saga 1", "1", "SER-CS", "Case Saga", "Case Author"),
        row("B0CS02", "Case Saga 2", "2", "SER-CS", "Case Saga", "Case Author"),
        # Rows Audible should never send and Listenarr relays anyway.
        {"asin": 123, "title": None},
        {"asin": "B0CS03", "title": 42, "series": [{"asin": "SER-CS", "position": "nan"}]},
        {"asin": "B0CS04", "title": "Case Saga Untitled Position",
         "series": [{"asin": "SER-CS", "position": "inf"}], "authors": [{"name": 7}]},
        # An unnumbered companion, listed once per marketplace.
        row("B0CSC1", "Case Saga Companion", None, "SER-CS", "Case Saga", "Case Author"),
        row("B0CSC2", "Case Saga Companion", None, "SER-CS", "Case Saga", "Case Author"),
    ],
}
PRODUCTS["B0CS01"] = {"title": "Case Saga 1", "_region": "ca",
                      "series": [{"title": "Case Saga", "sequence": "1", "asin": "SER-CS"}]}
CANDIDATES = {
    "Harry Potter": [
        {"asin": "B0716WMLJB", "name": "Harry Potter", "region": "ca"},
        {"asin": "B0711PTW7B", "name": "Harry Potter", "region": "ca"},
        {"asin": "B0FJMNG7KH", "name": HP, "region": "ca"},
        {"asin": "B0D229XM3B", "name": "Harry Potter (Narrated by Stephen Fry)",
         "region": "ca"},
    ],
}

jellyfin.books = lambda uid: [dict(item) for item in LIBRARY]
audible.product = lambda asin: PRODUCTS.get(asin)
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
listenarr.series_candidates = lambda name, region=None: CANDIDATES.get(name, [])

added = []
metadata_seen = []
searched = []
refused = set()


def fake_add(asin, monitored=True, metadata=None):
    metadata_seen.append(metadata)
    if asin in refused:
        return listenarr.AddResult(False, "Listenarr said no", None)
    added.append(asin)
    return listenarr.AddResult(True, "Sent to Listenarr", len(added),
                               (metadata or {}).get("title") or "", ())


# Kept so the checks at the bottom can exercise the real one.
REAL_ADD = listenarr.add
listenarr.add = fake_add
listenarr.enqueue_search = lambda audiobook_id: searched.append(audiobook_id) or True


# --- the plan: owned by id, by title, by position; one gap per position ------
planned = series.plan(matt, DF, anchor_item_id="j2")
check("series resolved through a member's id", planned["seriesAsin"], "SER-DF")
check("marketplace follows the member", planned["region"], "ca")
check("owned by id, by title and author, by id, and by title within the series",
      sorted(c["asin"] for c in planned["have"]),
      ["B0DF01", "B0DF02X", "B0DF04", "B0DFSS", "B0DFSSUS"])
check("one request per position",
      [c["title"] for c in planned["missing"]],
      ["Grave Peril", "Death Masks", "Blood Rites"])
check("nothing on order yet", planned["onOrder"], [])

# --- asking, uncapped -------------------------------------------------------
outcome = series.want_series(matt, DF, anchor_item_id="j1")
check("every gap asked for", [c["title"] for c in outcome["requested"]],
      ["Grave Peril", "Death Masks", "Blood Rites"])
check("the first edition of a doubled position is the one asked for",
      "B0DF03" in added and "B0DF03US" not in added, True)
check("a book owned under a shorter title is not asked for in either listing",
      "B0DFSS" not in added and "B0DFSSUS" not in added, True)
check("owned count is books, not rows", outcome["ownedCount"], 4)
check("nothing held back", outcome["heldBackCount"], 0)
check("a search is queued per book", len(searched), 3)
check("metadata rides along", all(m is not None for m in metadata_seen), True)
check("metadata names the series", metadata_seen[0]["series"], DF)
check("metadata names the store", metadata_seen[0]["region"], "ca")
check("the sentence names the books",
      outcome["message"],
      "Asked for 3 books from The Dresden Files: Grave Peril, Death Masks, "
      "Blood Rites.")
ledger = {r["asin"] for r in store.requests_for(matt.key)}
check("the ledger has every request", {"B0DF03", "B0DF05", "B0DF06"} <= ledger, True)

# --- asking again: everything is on order, nothing is re-added ---------------
before = len(added)
again = series.want_series(matt, DF)
check("a second tap adds nothing", len(added), before)
check("the gaps read as on order", again["onOrderCount"], 3)
check("the sentence says so", again["message"],
      "You have 4 books of The Dresden Files. The 3 books you do not have are "
      "already being looked for.")

# --- the other marketplace's edition at the same position is owned ----------
full = series.want_series(matt, HP, anchor_item_id="h1")
check("no gap where both editions exist", full["requested"], [])
check("two books, not four rows", full["ownedCount"], 2)
check("the complete sentence", full["message"],
      "You already have every book Audible lists in Harry Potter (Full-Cast "
      "Editions): 2 books.")

# --- the cap, and the per-tap limit -----------------------------------------
capped = series.want_series(kadija, "Long Saga", anchor_item_id="l1")
check("a capped account stops at its allowance",
      len(capped["requested"]), config.BOOK_DAILY_CAP)
check("the rest is counted", capped["heldBackCount"], 6 - config.BOOK_DAILY_CAP)
check("the sentence explains the stop",
      "today's allowance" in capped["message"], True)
check("allowance spent", wants.allowance(kadija), 0)

exhausted = series.want_series(kadija, "Long Saga")
check("nothing asked for on an exhausted allowance", exhausted["requested"], [])
check("the sentence says the allowance is gone and what is missing",
      exhausted["message"],
      "You have used today's requests. 3 books of Long Saga are still missing. "
      "Another 3 books are already being looked for.")

saved_limit = config.SERIES_WANT_LIMIT
config.SERIES_WANT_LIMIT = 2
limited = series.want_series(matt, "Long Saga")
check("the tap limit holds", len(limited["requested"]), 2)
check("what it did not get to is counted", limited["heldBackCount"], 1)
check("and the sentence says to tap again",
      limited["message"],
      "Asked for 2 books from Long Saga: Long Saga 5, Long Saga 6. 1 more book "
      "not asked for yet. Use this again for the next batch. Another 3 books "
      "are already being looked for.")
config.SERIES_WANT_LIMIT = saved_limit

# --- a book Listenarr will not take does not stop the others ----------------
refused.add("B0LS07")
partial = series.want_series(matt, "Long Saga")
check("the refused book is reported", [c["title"] for c in partial["failed"]],
      ["Long Saga 7"])
check("and the sentence says so", partial["message"],
      "Could not ask for Long Saga 7. Another 5 books are already being looked for.")
refused.clear()

# --- resolving by name, only when it is unambiguous -------------------------
try:
    series.plan(matt, "Harry Potter (Jim Dale)")
except series.Unresolvable as exc:
    check("an ambiguous name refuses rather than guesses",
          "Could not tell which Audible series" in str(exc), True)
else:
    failures.append("two Audible series called Harry Potter must not be guessed between")

fry = series.plan(matt, "Harry Potter (Stephen Fry)", anchor_item_id="d2")
check("a name whose every word one series carries resolves to that series",
      fry["seriesAsin"], "B0D229XM3B")
check("and its owned position is recognised", [c["title"][-6:] for c in fry["missing"]],
      ["Book 2"])

CANDIDATES["Harry Potter"] = [
    {"asin": "SER-HP-ONE", "name": "Harry Potter", "region": "ca"},
    {"asin": "B0FJMNG7KH", "name": HP, "region": "ca"},
]
named = series.plan(matt, "Harry Potter (Jim Dale)", anchor_item_id="d1")
check("a unique name resolves once the qualifier is dropped",
      named["seriesAsin"], "SER-HP-ONE")
check("the one book at position 1 is owned by position", named["missing"], [])

# --- what is not a series here ----------------------------------------------
for label, name, anchor in [("unknown name", "Nothing Like This", None),
                            ("anchor from another series", DF, "h1"),
                            ("a book with no series", "Lone Book", "x1")]:
    try:
        series.plan(matt, name, anchor_item_id=anchor)
    except series.NotASeries:
        pass
    else:
        failures.append(f"{label} must be refused as not a series")

# --- Listenarr not answering is an outage, not an answer --------------------
listenarr.series_books = lambda asin, region=None: None
try:
    series.plan(matt, DF)
except series.Unavailable:
    pass
else:
    failures.append("no answer from Listenarr must read as unavailable")
listenarr.series_books = lambda asin, region=None: []
try:
    series.plan(matt, DF)
except series.Unresolvable as exc:
    check("an empty series says so", "lists no books" in str(exc), True)
else:
    failures.append("an empty listing must be refused, not asked for")

# --- a book the listener hid is left out, and said to be -------------------
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
refused.clear()
store.dismiss(matt.key, "B0LS07")
hid = series.want_series(matt, "Long Saga")
check("a hidden book is not asked for", hid["requested"], [])
check("and is counted apart from the ones on order", hid["leftOutCount"], 1)
check("the sentence says so", hid["message"],
      "You have 1 book of Long Saga. Another 5 books are already being looked for. "
      "1 book you hid was left out.")
store.undismiss(matt.key, "B0LS07")

# --- on order means an unfulfilled request, not a fulfilled one -------------
store.record_request(kadija.key, "B0FULFILLED", "Done Book")
store.fulfil_requests(kadija.key, {"B0FULFILLED"})
check("a fulfilled request is no longer on order",
      "B0FULFILLED" in store.ordered_asins(), False)
check("an open one is", "B0LS02" in store.ordered_asins(), True)

# --- ASIN case, and rows that should never arrive ----------------------------
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
store.record_request(kadija.key, "b0cs02", "Case Saga 2")
cased = series.plan(matt, "Case Saga", anchor_item_id="c1")
check("a lower-case library id still counts as owned",
      [c["asin"] for c in cased["have"]], ["B0CS01"])
check("a lower-case ledger id still counts as on order",
      [c["asin"] for c in cased["onOrder"]], ["B0CS02"])
check("a row with no title is skipped; an unnumbered book listed twice is one gap",
      sorted(c["asin"] for c in cased["missing"]), ["B0CS04", "B0CSC1"])
check("a non-numeric position is kept as text",
      cased["missing"][0]["position"], "inf")

# --- the cap can be reached between the check and the attempt ---------------
real_want = wants.want
def raced(*args, **kwargs):
    raise wants.AllowanceExhausted("That is 3 books today.")
wants.want = raced
raced_outcome = series.want_series(matt, "Case Saga")
wants.want = real_want
check("a cap denial ends the batch", raced_outcome["requested"], [])
check("and is not reported as a refusal", raced_outcome["failed"], [])
check("the sentence blames the allowance", raced_outcome["message"],
      "You have used today's requests. 2 books of Case Saga are still missing. "
      "Another 1 book is already being looked for.")

# --- a series announced further than it has been published ------------------
# Audible lists a placeholder product for each volume it has named and not
# released, and a pre-order for one with a date on it. Neither can be
# acquired, and asking for either leaves Listenarr searching forever.
# The plan reads the clock in UTC, so the fixture has to as well: on a box
# ahead of UTC a local "today" is a future date for part of the day, and the
# row meant to be out would read as unreleased.
TODAY = datetime.now(timezone.utc).date()
CS = "Calamity Saga"
LIBRARY.append(book("cb1", "Calamity Saga 1", CS, 1, asin="B0CB01",
                    author="Calamity Author"))
PRODUCTS["B0CB01"] = {"title": "Calamity Saga 1", "_region": "ca",
                      "series": [{"title": CS, "sequence": "1", "asin": "SER-CB"}]}
SERIES_BOOKS["SER-CB"] = [
    row("B0CB01", "Calamity Saga 1", "1", "SER-CB", CS, "Calamity Author"),
    # Out today, which is out.
    row("B0CB02", "Calamity Saga 2", "2", "SER-CB", CS, "Calamity Author",
        release=TODAY.isoformat()),
    # No date relayed at all: asked for rather than withheld on a blank field.
    row("B0CB03", "Calamity Saga 3", "3", "SER-CB", CS, "Calamity Author",
        release=""),
    # A genuine pre-order.
    row("B0CB04", "Calamity Saga 4", "4", "SER-CB", CS, "Calamity Author",
        release=(TODAY + timedelta(days=30)).isoformat()),
    # A placeholder, with every marker Audible actually sends on one.
    {**row("B0CB05", "Calamity Saga 5", "5", "SER-CB", CS, "Calamity Author",
           release="2200-01-01"),
     "narrators": [], "sku": "PL_HLDR_158623CA",
     "publisher": "ZZZ - Series Advisor Placeholder"},
]

gaps = series.plan(matt, CS, anchor_item_id="cb1")
check("an unpublished book is not a gap", [c["title"] for c in gaps["missing"]],
      ["Calamity Saga 2", "Calamity Saga 3"])
check("a pre-order and a placeholder are both held back",
      [c["title"] for c in gaps["notOut"]],
      ["Calamity Saga 4", "Calamity Saga 5"])

asked = series.want_series(matt, CS, anchor_item_id="cb1")
check("only the published gaps are asked for",
      [c["title"] for c in asked["requested"]],
      ["Calamity Saga 2", "Calamity Saga 3"])
check("nothing unpublished reaches Listenarr",
      "B0CB04" not in added and "B0CB05" not in added, True)
check("the unpublished ones are counted", asked["notOutCount"], 2)
check("and the sentence says so", asked["message"],
      "Asked for 2 books from Calamity Saga: Calamity Saga 2, Calamity Saga 3. "
      "2 books are not out yet.")

again_cs = series.want_series(matt, CS)
check("a second tap keeps saying what is unpublished", again_cs["message"],
      "You have 1 book of Calamity Saga. Another 2 books are already being "
      "looked for. 2 books are not out yet.")

# A placeholder somebody asked for before this rule existed reads as
# unpublished, not as a book on its way.
store.record_request(kadija.key, "B0CB05", "Calamity Saga 5")
recorded = series.plan(matt, CS, anchor_item_id="cb1")
check("an unpublished book already in the ledger is still unpublished",
      "B0CB05" in [c["asin"] for c in recorded["notOut"]], True)
check("and is not reported as on order",
      "B0CB05" in [c["asin"] for c in recorded["onOrder"]], False)

check("one unpublished book reads as one",
      series.sentence(CS, owned_count=1, on_order=0, left_out=0, not_out=1,
                      requested=[], failed=[], held_back=0, cap_hit=False,
                      missing=0),
      "You have 1 book of Calamity Saga. 1 book is not out yet.")

check("a book out today is out",
      series._not_out_yet({"releaseDate": TODAY.isoformat()}, TODAY), False)
check("tomorrow's is not",
      series._not_out_yet({"releaseDate": (TODAY + timedelta(days=1)).isoformat()},
                          TODAY), True)
check("a full timestamp is read as its date",
      series._not_out_yet({"releaseDate": "2200-01-01T00:00:00Z"}, TODAY), True)
check("a date no calendar recognises counts as out",
      series._not_out_yet({"releaseDate": "coming soon"}, TODAY), False)
check("no date at all counts as out", series._not_out_yet({}, TODAY), False)

# --- positions compare as numbers -------------------------------------------
check("3 and 3.0 are one position", series._position("3.0"), series._position(3))
check("a half position survives", series._position("3.5"), "3.5")
check("no position is None", series._position(""), None)

# --- and the add boundary holds the same line -------------------------------
# The series planner was the only thing checking this, so a placeholder reached
# through any other door -- an ordinary keyword search, or a client posting an
# ASIN -- was accepted by Listenarr and then searched for forever. There is no
# file to find and nothing downstream ever gives up.
from app import listenarr as _listenarr  # noqa: E402

FUTURE = (TODAY + timedelta(days=30)).isoformat()
# Nothing is in Listenarr's library here, so the by-asin check finds nothing
# and the date is what decides.
_listenarr.find_by_asin = lambda asin: None
refused = REAL_ADD("B0NOTOUT", metadata={
    "asin": "B0NOTOUT", "title": "Book Five", "publishedDate": FUTURE})
check("an unpublished book is refused at the add boundary", refused.ok, False)
check("and the message says why rather than blaming the catalogue",
      "not out yet" in refused.message, True)

check("a book that is out is not refused for its date",
      _listenarr.not_yet_published(TODAY.isoformat()), False)
check("a timestamp is read off its leading ten characters",
      _listenarr.not_yet_published(FUTURE + "T00:00:00Z"), True)

harness.discard(DB_PATH)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("ok")
