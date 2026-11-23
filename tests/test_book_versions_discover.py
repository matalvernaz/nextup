"""Discover never offers a GraphicAudio dramatization where the server gets those from GraphicAudio.

Matt, 2026-11-22: "the dramatized adaptions are all graphic audio, which I also
already have", and "I don't get it through audible since their versions are
lower quality". With REFUSE_GRAPHIC_AUDIO set, one is passed over the way a
held book is; unset, Discover is what it always was.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
os.environ["REFUSE_GRAPHIC_AUDIO"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness  # noqa: E402

DB_PATH = harness.use("versions-discover")
harness.discard(DB_PATH)

from app import config, jellyfin, listenarr  # noqa: E402
from app.books import engine, store  # noqa: E402

harness.no_book_ratings()
store.init()
check = harness.Check("graphicaudio on discover")
USER = jellyfin.User(id="reader-id", name="Reader")

LIBRARY = [{
    "Id": "played", "Name": "Played Book", "Overview": "kindly heroes build a magical farm",
    "Genres": ["Fantasy"], "People": [{"Type": "Author", "Name": "Seth Ring"}],
    "UserData": {"Played": True}, "ProviderIds": {"Audible": "PLAYED"},
}]


def suggestion(asin, title, publisher):
    return {"asin": asin, "title": title, "authors": ["Seth Ring"], "narrators": [],
            "series": None, "series_position": None, "publisher": publisher,
            "description": "kindly heroes build a magical farm", "image": None}


NEIGHBOURS = {"PLAYED": [
    suggestion("B0CZ4HN41N", "Domestication (Dramatized Adaptation)", "Graphic Audio LLC"),
    suggestion("B0READING1", "Conservation", "Podium Audio"),
    suggestion("B0POTTER01", "Harvest (Full-Cast Edition)", "Soundbooth Theater"),
]}

jellyfin.books = lambda uid: [dict(row) for row in LIBRARY]
engine._seed_sims = lambda seed, **kwargs: NEIGHBOURS.get(engine._asin(seed) or "", [])
listenarr.queued_asins = lambda: set()
config.MAX_SHELF = 20


def discover():
    return [row["asin"] for row in engine.run(USER, update_playlist=False)["discover"]]


refused = discover()
check.that("B0CZ4HN41N" not in refused, "GraphicAudio's dramatization is not offered")
check.that("B0READING1" in refused, "a reading still is")
check.that("B0POTTER01" in refused, "and so is another publisher's full-cast edition")

config.REFUSE_GRAPHIC_AUDIO = False
check.that("B0CZ4HN41N" in discover(), "with the setting off, Discover is what it always was")

# The keyword channel builds its own candidates from Listenarr's search rows,
# and has to keep the publisher that tells GraphicAudio's apart.
config.REFUSE_GRAPHIC_AUDIO = True
config.KEYWORD_PULL_ENABLED = True
engine._candidate_description = lambda asin: ""
listenarr.audible_search = lambda query, limit=25, **kwargs: [
    {"asin": "B0CZ4HN41N", "title": "Domestication (Dramatized Adaptation)",
     "authors": [{"name": "Seth Ring"}], "publisher": "Graphic Audio LLC"},
    {"asin": "B0READING1", "title": "Conservation",
     "authors": [{"name": "Seth Ring"}], "publisher": "Podium Audio"},
]
found = engine._keyword_candidates(["magical farm"], engine._refused)
check.that("B0CZ4HN41N" not in found, "a keyword hit is known as GraphicAudio's by its publisher")
check.that("B0READING1" in found, "and a reading found by keyword is kept")

raise SystemExit(check.report())
