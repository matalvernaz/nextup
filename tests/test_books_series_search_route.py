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
from app.books import audible, series, store

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

# --- the sentence at both ends --------------------------------------------
check("owning none of it reads as English, not arithmetic",
      series.state_sentence(0, 0, 8), "None of the 8 in your library, 8 to ask for.")
check("owning all of it says so",
      series.state_sentence(6, 0, 0), "You already have all 6 that Audible lists.")
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
