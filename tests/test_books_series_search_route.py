"""Searching for a series on the route the recommendation screen actually calls.

The nextup route has taken `unit=series` since books became a medium. The
screen a listener searches for a book on is the recommendation screen, which
talks to the nextread-compatible route, and that route had no way to ask for a
series at all. These checks cover the parameter, the row shape the shipped
client decodes, and the two things that must not change: a caller that sends no
`kind` still gets books, and a series row carries the name that can re-plan it.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("books-series-search-route")

harness.discard(DB_PATH)

from app import config, jellyfin, listenarr
from app.books import audible, series, shelves, store

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

LIBRARY = [
    {"Id": "f1", "Name": "Rise of the Living Forge: A LitRPG Adventure",
     "SeriesName": FORGE, "IndexNumber": 1,
     "People": [{"Name": "Al Forge", "Type": "Author"}],
     "ProviderIds": {"Audible": "B0FG01"}},
]

SERIES_BOOKS = {
    "SER-FORGE": [row(f"B0FG0{n}", f"Living Forge {n}", str(n),
                      "SER-FORGE", FORGE) for n in range(1, 5)],
}
CANDIDATES = {FORGE: [{"asin": "SER-FORGE", "name": FORGE, "region": "ca"}]}
PRODUCTS = {
    "B0FG01": {"title": "Living Forge 1", "_region": "ca",
               "series": [{"title": FORGE, "sequence": "1",
                           "asin": "SER-FORGE"}]},
}

jellyfin.books = lambda uid: [dict(item) for item in LIBRARY]
audible.product = lambda asin: PRODUCTS.get(asin)
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
listenarr.series_candidates = lambda name, region=None: CANDIDATES.get(name, [])
listenarr.audible_search = lambda query, limit=25: [
    {"asin": "B0KEYWORD", "title": f"Something about {query}",
     "authors": [{"name": "Al Forge"}], "narrators": [{"name": "A Reader"}],
     "lengthMinutes": 300},
]

# --- the row a series search puts on screen ---------------------------------
rows = series.search_rows(matt, FORGE)
check("a series search answers with one row", len(rows), 1)
found = rows[0]

# Every key the shipped client's `Hit` requires. Absent or misspelled, the whole
# response fails to decode and the screen reports an empty search against a 200
# -- which is the seam that cost a round on this codebase once already.
for key, kind in [("asin", str), ("title", str), ("authors", list),
                  ("narrators", list), ("owned", bool), ("requested", bool)]:
    check(f"the row carries {key} as {kind.__name__}",
          isinstance(found.get(key), kind), True)

check("the row is keyed on the series, not a book", found["asin"], "SER-FORGE")
check("and titled as the catalogue spells it", found["title"], FORGE)
check("one of four owned is not owned", found["owned"], False)
check("nor already asked for", found["requested"], False)
check("it says what asking would do", found["detail"],
      "1 of 4 in your library, 3 to ask for.")
check("it says it is a series", found["kind"], "series")

# Identity for the row and identity for the request are two fields: `asin` keys
# the row, `series` is the name `want_series` re-plans from, and it has no ASIN
# door to go through instead.
check("the row carries the name that re-plans it", found["series"], FORGE)

# --- a query nothing answers to -------------------------------------------
check("an unknown series puts no row on screen",
      series.search_rows(matt, "No Such Series Anywhere"), [])

# --- a name several series answer to offers them (2026-09-24) ---------------
# "disney" is a word in two of Audible's series. The planner refuses to guess
# which, rightly, and the search used to answer with nothing at all -- the same
# "No series found" as a name the catalogue has never heard of.
ORIGINALS = "Disney: Audible Originals"
BEDTIME = "Children's Favorites Disney Bedtime Favorites and Disney Storybook Collection"
both = [{"asin": "SER-ORIG", "name": ORIGINALS, "region": "ca"},
        {"asin": "SER-BED", "name": BEDTIME, "region": "ca"}]
CANDIDATES.update({"disney": both, ORIGINALS: both, BEDTIME: both})
SERIES_BOOKS.update({
    "SER-ORIG": [row(f"B0OR0{n}", f"Original {n}", str(n), "SER-ORIG", ORIGINALS,
                     author="Disney Press") for n in range(1, 4)],
    "SER-BED": [row(f"B0BD0{n}", f"Bedtime {n}", str(n), "SER-BED", BEDTIME,
                    author="Disney Books") for n in range(1, 3)],
})
listings = []
jellyfin.books = lambda uid: listings.append(uid) or [dict(item) for item in LIBRARY]
# Cold, as after a restart: the search above has left a listing in memory.
with shelves._cache_guard:
    shelves._owned_cache.clear()
    shelves._series_cache.clear()

rows = series.search_rows(matt, "disney")
check("an ambiguous name offers every series it could mean",
      sorted(r["title"] for r in rows), sorted([ORIGINALS, BEDTIME]))
check("each carrying the catalogue's own name, which re-plans only it",
      sorted(r["series"] for r in rows), sorted([ORIGINALS, BEDTIME]))
check("each saying what asking for it would do",
      {r["title"]: r["detail"] for r in rows}[ORIGINALS],
      "None of the 3 in your library, 3 to ask for.")
check("the library is listed once for all of them", len(listings), 1)
# Listing it was eleven of the twelve and a half seconds this search took live,
# of the twenty the client gives a search (2026-09-24).
series.search_rows(matt, "disney")
check("and a second search reads it from memory", len(listings), 1)

from app.books import adapter  # noqa: E402
hits = adapter.search_hits(matt, "disney", unit="series")
check("the nextup route offers the same choices",
      sorted(h["itemKey"] for h in hits), sorted([ORIGINALS, BEDTIME]))

check("the exact name still picks one series",
      [r["series"] for r in series.search_rows(matt, ORIGINALS)], [ORIGINALS])

many = [{"asin": f"SER-{n}", "name": f"Disney Collection {n}", "region": "ca"}
        for n in range(12)]
CANDIDATES["disney collection"] = many
for entry in many:
    CANDIDATES[entry["name"]] = many
    SERIES_BOOKS[entry["asin"]] = [row(f"B0{entry['asin']}", entry["name"], "1",
                                       entry["asin"], entry["name"])]
check("the choices stop at a listenable number",
      len(series.search_rows(matt, "disney collection")), series.MAX_CHOICES)


# --- what the plan counts, against another marketplace's listing ----------
# The live "Disney: Audible Originals" plan read "10 of 13 in your library, 3 to
# ask for" with 8 in the library and 5 on order. The listing came from the other
# store, whose ASINs are not the ones the books were asked for under, so books
# on order read as gaps -- asking would have requested them again -- and a
# prefix the shelf's Disney Princess books share made two unarrived ones held.
LIBRARY.append({"Id": "belle", "Name": "Disney Princess: Belle and the Rose Riddle",
                "SeriesName": ORIGINALS,
                "People": [{"Name": "Disney Press", "Type": "Author"}]})
SERIES_BOOKS["SER-ORIG"] = [
    row("B0COM-BELLE", "Disney Princess: Belle and the Rose Riddle", None,
        "SER-ORIG", ORIGINALS, author="Disney Press"),
    row("B0COM-MOANA", "Disney Princess: Moana and Tales from Motunui", None,
        "SER-ORIG", ORIGINALS, author="Disney Press"),
    row("B0COM-FORKY", "Disney Pixar Toy Story: Forky's Kindergarten Adventure",
        None, "SER-ORIG", ORIGINALS, author="Disney Press"),
]
with store.db() as conn:
    conn.execute("INSERT INTO requests(user_key,medium,item_key,unit,title,"
                 "requested_at) VALUES(?,?,?,?,?,?)",
                 ("kid", "book", "B0CA-FORKY", "book",
                  "Disney Pixar Toy Story: Forky's Kindergarten Adventure", 1.0))
listed_before = len(listings)
planned = series.plan(matt, ORIGINALS)
check("what asking plans against is listed afresh, not kept in memory",
      len(listings), listed_before + 1)
check("the book on the shelf is held",
      [c["title"] for c in planned["have"]],
      ["Disney Princess: Belle and the Rose Riddle"])
check("a book asked for under the other store's ASIN is on order, not a gap",
      [c["title"] for c in planned["onOrder"]],
      ["Disney Pixar Toy Story: Forky's Kindergarten Adventure"])
check("and a book sharing only the Disney Princess prefix is still a gap",
      [c["title"] for c in planned["missing"]],
      ["Disney Princess: Moana and Tales from Motunui"])


# --- the sentence at both ends --------------------------------------------
check("owning none of it reads as English, not arithmetic",
      series.state_sentence(0, 0, 8), "None of the 8 in your library, 8 to ask for.")
check("owning all of it says so",
      series.state_sentence(6, 0, 0), "You already have all 6 that Audible lists.")
check("held and on order is not all held",
      series.state_sentence(8, 5, 0),
      "8 of 13 in your library, 5 already on order. Nothing left to ask for.")
check("everything on order is not a claim to own it",
      series.state_sentence(0, 3, 0),
      "Nothing left to ask for out of the 3 Audible lists.")

# --- the route itself ------------------------------------------------------
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import compat_nextread  # noqa: E402

# The route introspects the caller's own Jellyfin token and has no fallback,
# so the token is what stands in for an account here -- the same shape
# `test_api_auth` uses, rather than a dependency override that would bypass the
# very thing every route on this service depends on.
jellyfin.user_from_token = lambda token: matt
routed = FastAPI()
routed.include_router(compat_nextread.router)
client = TestClient(routed, raise_server_exceptions=False)
TOKEN = {"X-Emby-Token": "a-real-looking-token"}

answer = client.get("/nextread/api/v1/search",
                    params={"q": FORGE, "kind": "series"}, headers=TOKEN)
check("the route takes kind=series", answer.status_code, 200)
body = answer.json()
check("and answers with the series row", [r["asin"] for r in body["results"]],
      ["SER-FORGE"])
check("echoing the kind it answered in", body["kind"], "series")

# The whole point of a default: a client that predates this parameter must get
# exactly what it got before, which is books.
plain = client.get("/nextread/api/v1/search", params={"q": "anything"},
                   headers=TOKEN)
check("no kind still means books", plain.status_code, 200)
check("and books are what come back",
      plain.json()["results"][0]["asin"], "B0KEYWORD")
check("named as books, so a client can tell", plain.json()["kind"], "book")

harness.discard(DB_PATH)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("ok")
