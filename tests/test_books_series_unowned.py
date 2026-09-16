"""Asking for a whole series the library holds none of.

The series screen can only be reached through a book already here, so the plan
used to refuse a name with no members. Search asks the opposite question --
"get me this whole series" -- and refusing it meant acquiring book one, waiting
for the import, and only then asking for the rest from a screen that had just
appeared. These checks cover the unowned plan, the row search puts on screen
for it, and the two refusals that must survive the change.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("books-series-unowned")

harness.discard(DB_PATH)

from app import config, jellyfin, listenarr
from app.books import adapter, audible, series, store

store.init()

matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def row(asin, title, position, series_asin, series_name, author="Al Forge"):
    return {"asin": asin, "title": title, "authors": [{"name": author}],
            "narrators": [{"name": "A Reader"}], "lengthMinutes": 600,
            "releaseDate": "2020-01-01",
            "series": [{"asin": series_asin, "name": series_name,
                        "position": position}]}


FORGE = "Rise of the Living Forge"

# Nothing in this library belongs to the series being asked for. One unrelated
# book, so the library read is not simply empty -- an empty library would pass
# a plan that only works because there was nothing to disagree with.
LIBRARY = [
    {"Id": "x1", "Name": "Something Else", "SeriesName": "Another Saga",
     "IndexNumber": 1, "People": [{"Name": "Someone", "Type": "Author"}],
     "ProviderIds": {"Audible": "B0OTHER"}},
]

SERIES_BOOKS = {
    "SER-FORGE": [row(f"B0FG0{n}", f"Living Forge {n}", str(n),
                      "SER-FORGE", FORGE) for n in range(1, 5)],
}

# Audible's own series search, which is the only way to resolve a name with no
# member book behind it. Keyed exactly as `_series_by_name` asks for it.
CANDIDATES = {
    FORGE: [{"asin": "SER-FORGE", "name": FORGE, "region": "ca"}],
    "living forge": [{"asin": "SER-FORGE", "name": FORGE, "region": "ca"}],
    "Ambiguous Saga": [
        {"asin": "SER-A1", "name": "Ambiguous Saga", "region": "ca"},
        {"asin": "SER-A2", "name": "Ambiguous Saga", "region": "ca"},
    ],
}

jellyfin.books = lambda uid: [dict(item) for item in LIBRARY]
audible.product = lambda asin: None
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
listenarr.series_candidates = lambda name, region=None: CANDIDATES.get(name, [])

added = []


def fake_add(asin, monitored=True, metadata=None):
    added.append(asin)
    return listenarr.AddResult(True, "Sent to Listenarr", len(added),
                               (metadata or {}).get("title") or "", ())


listenarr.add = fake_add
listenarr.enqueue_search = lambda audiobook_id: True

# --- the plan, with nothing of the series in the library ---------------------
planned = series.plan(matt, FORGE)
check("a series owned none of resolves from the catalogue alone",
      planned["seriesAsin"], "SER-FORGE")
check("nothing reads as owned", planned["have"], [])
check("every book is a gap",
      [c["title"] for c in planned["missing"]],
      ["Living Forge 1", "Living Forge 2", "Living Forge 3", "Living Forge 4"])

# --- the row search puts on screen -------------------------------------------
hits = adapter.search_hits(matt, FORGE, unit="series")
check("search answers with one series row", len(hits), 1)
check("the row is a series, not a book", hits[0]["unit"], "series")
check("it is not marked owned", hits[0]["owned"], False)
check("the counts read as English at zero, not as arithmetic",
      hits[0]["overview"], "None of the 4 in your library, 4 to ask for.")

# --- the title is the catalogue's, the key is what re-plans -------------------
# `_series_by_name` takes a name whose words are all present, so a short query
# resolves a longer series. The row has to say which series is about to be
# acquired; the key has to be the string that plans to this same answer.
typed = adapter.search_hits(matt, "living forge", unit="series")
check("a shorter query still resolves the series", len(typed), 1)
check("the row is titled as the catalogue spells it", typed[0]["title"], FORGE)
check("the key stays the name that planned it", typed[0]["itemKey"], "living forge")

# --- asking for the whole thing in one go ------------------------------------
outcome = series.want_series(matt, FORGE)
check("every book of an unowned series is asked for",
      [c["title"] for c in outcome["requested"]],
      ["Living Forge 1", "Living Forge 2", "Living Forge 3", "Living Forge 4"])
check("and each one reached Listenarr", sorted(added),
      ["B0FG01", "B0FG02", "B0FG03", "B0FG04"])
check("nothing was owned", outcome["ownedCount"], 0)
check("nothing was held back", outcome["heldBackCount"], 0)

# --- what must still be refused ----------------------------------------------
# The series screen names a book on screen. A name that does not hold that book
# is a mismatch, and relaxing the members rule must not relax this one.
try:
    series.plan(matt, FORGE, anchor_item_id="x1")
    check("an anchor not filed under the name is refused", "no refusal", "NotASeries")
except series.NotASeries:
    pass

# Two series answering to one name is a guess about which edition was meant,
# and a guess here acquires the wrong narrator's books.
try:
    series.plan(matt, "Ambiguous Saga")
    check("an ambiguous name is refused", "no refusal", "Unresolvable")
except series.Unresolvable:
    pass

# A name nothing carries is not a series; a name three things carry is a series
# this cannot pick an edition of. Separate answers, because the first deserves
# no row and the second deserves an explanation.
try:
    series.plan(matt, "No Such Series At All")
    check("a name the catalogue has never heard of", "no refusal", "NotASeries")
except series.Unresolvable:
    check("a name nothing carries is not a series, rather than unresolvable",
          "Unresolvable", "NotASeries")
except series.NotASeries:
    pass

# Either way search says nothing rather than inventing a row.
check("an unknown name puts no row on screen",
      adapter.search_hits(matt, "No Such Series At All", unit="series"), [])

harness.discard(DB_PATH)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("ok")
