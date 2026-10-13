"""A book's subtitle must reach Listenarr with the rest of its record.

Audible lists some books under a numbered series title and keeps their real
name in the subtitle: "Dark Tower III" is "The Waste Lands". Indexers publish
the book under that name, and Listenarr can only tell such a release is the
book if it knows the subtitle. Nextup dropped it on the way in, so no book it
added from a search row or a series listing had one (2026-10-13), and Dark
Tower III was refused on every MyAnonamouse copy while a dead public torrent
named for "Dark Tower III" was grabbed twice.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("books-subtitle-handoff")

from app import listenarr

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class FakeResponse:
    def __init__(self, results): self._results = results
    def raise_for_status(self): return self
    def json(self): return {"results": self._results}


class FakeClient:
    def __init__(self, results): self._results = results
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None): return FakeResponse(self._results)


# Verbatim shape of Listenarr's /search/audible row for the book.
ROW = {
    "asin": "B071LC7M1P",
    "title": "Dark Tower III",
    "subtitle": "The Waste Lands",
    "authors": [{"name": "Stephen King"}],
    "narrators": [{"name": "Frank Muller"}],
    "series": [{"name": "The Dark Tower", "position": "3", "asin": "B071VQV8VY"}],
    "releaseDate": "2016-01-01",
    "lengthMinutes": 1094,
}

check("a search row's subtitle is sent",
      listenarr._to_add_metadata(ROW).get("subtitle"), "The Waste Lands")
check("a series listing row's subtitle is sent",
      listenarr.metadata_from_search_row(ROW, region="ca").get("subtitle"), "The Waste Lands")
check("the subtitle is trimmed",
      listenarr._to_add_metadata({**ROW, "subtitle": "  The Waste Lands "}).get("subtitle"),
      "The Waste Lands")

# Listenarr keeps a field it is sent, so an empty one is left out rather than
# stored as an empty subtitle.
check("a blank subtitle is not sent",
      listenarr._to_add_metadata({**ROW, "subtitle": "   "}).get("subtitle"), None)
check("a missing subtitle is not sent",
      listenarr._to_add_metadata({"asin": "B0TEST", "title": "A Book"}).get("subtitle"), None)

# The ASIN lookup a single request goes through carries it too.
listenarr._client = lambda: FakeClient([ROW])
check("an ASIN lookup carries the subtitle",
      (listenarr.audible_metadata("B071LC7M1P") or {}).get("subtitle"), "The Waste Lands")

if failures:
    print("FAIL")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("test_books_subtitle_handoff: all checks passed")
