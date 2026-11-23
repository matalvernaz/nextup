"""One search row per book, GraphicAudio recognised in its own library, and never asked for.

Matt, 2026-11-22: "when there were different versions of a book it would just
group them together. There shouldn't be anything duplicated, it should just
give you the option later." Searching "domestication" gave three rows: Michael
Kramer's reading twice (the two stores' copies, only one shown as owned) and
GraphicAudio's dramatization shown as not owned, though it sits in the
household's GraphicAudio library as "Battle Mage Farmer 1: Domestication".
The rows below are those, as Listenarr's search returned them.
"""
import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid", JELLYFIN_USER="",
              GRAPHIC_AUDIO_LIBRARY_IDS="gaudiolib", REFUSE_GRAPHIC_AUDIO="true")
harness.forbid_network()

from app import artwork, config, jellyfin, listenarr  # noqa: E402
from app.books import engine, search, series, shelves, versions, wants  # noqa: E402

check = harness.Check("book versions")
USER = jellyfin.User(id="listener", name="listener", is_admin=True)
MINUTE = 600_000_000


def item(item_id, name, authors, library, *, series_name=None, narrators=(), asin=None,
         minutes=None, artist=None):
    people = [{"Name": a, "Type": "Author"} for a in authors]
    people += [{"Name": n, "Type": "Narrator"} for n in narrators]
    row = {"Id": item_id, "Name": name, "People": people, "_library": library,
           "ProviderIds": {"Audible": asin} if asin else {}}
    if series_name:
        row["SeriesName"] = series_name
    if artist:
        row["AlbumArtist"] = artist
    if minutes:
        row["RunTimeTicks"] = minutes * MINUTE
    return row


def row(asin, title, *, authors=("Seth Ring",), narrators=("Michael Kramer",), minutes=1020,
        publisher="Podium Audio", content_type="Product", book_format="unabridged"):
    return {"asin": asin, "title": title,
            "authors": [{"name": a} for a in authors],
            "narrators": [{"name": n} for n in narrators],
            "lengthMinutes": minutes, "publisher": publisher,
            "contentType": content_type, "bookFormat": book_format, "language": "english"}


KRAMER_US = row("B09WRTW2NQ", "Domestication")
KRAMER_CA = row("B09WRTK4DS", "Domestication", minutes=1021)
GRAPHIC = row("B0CZ4HN41N", "Domestication (Dramatized Adaptation)", narrators=("Full Cast",),
              minutes=600, publisher="Graphic Audio LLC", content_type="Performance")

LIBRARY = [
    # The reading, filed under a longer name, with the Canadian store's ASIN.
    item("r1", "Domestication : A Fantasy LitRPG Adventure (Battle Mage Farmer)", ["Seth Ring"],
         "bookslib", series_name="Battle Mage Farmer", narrators=["Michael Kramer"],
         asin="B09WRTK4DS", minutes=1021),
    # GraphicAudio's own release, as its library names it: no ASIN, GraphicAudio as the author.
    item("g1", "Battle Mage Farmer 1: Domestication", ["Graphic Audio LLC."], "gaudiolib",
         artist="Graphic Audio LLC."),
    item("g2", "001 Pilgrimage To Hell", [], "gaudiolib"),
    item("m2", "Mistborn: The Well of Ascension", ["Brandon Sanderson"], "bookslib",
         series_name="Mistborn"),
    # Two readings of one book exist; the library holds Nadia May's, without an ASIN.
    item("p1", "The Last Watch", ["J. S. Dewes"], "bookslib", narrators=["Nadia May"], minutes=900),
]

catalogue: list[dict] = []
library: list[dict] = list(LIBRARY)


def owned_index(user):
    index = engine._owned_index(library)
    index[1].held = versions.Held(library)
    return index


listenarr.audible_search = lambda query, limit=25, **kwargs: catalogue[:limit]
shelves.owned_index = owned_index
wants.states = lambda user_key, owned=None: []


def run(rows):
    catalogue[:] = rows
    return search.search(USER, "domestication")


# --- the case Matt saw ------------------------------------------------------

hits = run([KRAMER_US, GRAPHIC, KRAMER_CA])
check.equal(len(hits), 1, "three catalogue rows of one book are one search row")
hit = hits[0]
check.equal(hit["asin"], "B09WRTK4DS", "the row is the copy the library holds")
check.that(hit["owned"], "the reading is shown as owned")
check.equal(hit["title"], "Domestication", "the row's fields are its own recording's")
check.equal(len(hit.get("versions") or []), 1, "the stores' two copies are one recording, so one other version")
ga = (hit.get("versions") or [{}])[0]
check.that(ga.get("graphicAudio"), "the other version is GraphicAudio's")
check.that(ga.get("owned"), "GraphicAudio's is held, in its own library")
check.that(not ga.get("askable"), "a held dramatization is not offered")
check.equal(hit.get("versionSummary"),
            "Also a GraphicAudio dramatization, in your GraphicAudio library.",
            "the row says the dramatization is in the GraphicAudio library")

from app.books import adapter  # noqa: E402
check.equal(adapter._as_hit(hit)["overview"],
            "Also a GraphicAudio dramatization, in your GraphicAudio library.",
            "the website says it under the row")

# --- the stores' copies alone ------------------------------------------------

hits = run([KRAMER_US, KRAMER_CA])
check.equal(len(hits), 1, "the two stores' copies of one reading are one row")
check.that("versions" not in hits[0], "one recording has no versions to offer")
check.that("versionSummary" not in hits[0], "and nothing to say about them")

# Neither copy's id is held, and only one copy's title is the library's: the
# recording is held all the same, whichever copy the row shows.
library[:] = [dict(entry, ProviderIds={}) if entry["Id"] == "r1" else entry for entry in LIBRARY]
hits = run([row("B09WRTK4DS", "Domestication: Battle Mage Farmer, Book 1", minutes=1021), KRAMER_US])
check.equal(len(hits), 1, "two copies under different names are still one row")
check.that(hits[0]["owned"], "either store's copy matching the library makes the reading held")
library[:] = list(LIBRARY)

# The other store's copy alone is owned too: the library's name only describes it.
check.that(run([KRAMER_US])[0]["owned"], "Audible's plain 'Domestication' is the library's reading")

# --- a book with one row reads exactly as before ------------------------------

single = run([row("B00ONLY001", "Pilgrimage to Hell", authors=("Brandon Sanderson",))])[0]
check.equal(set(single), {"asin", "title", "authors", "narrators", "runtimeMinutes", "owned",
                          "requested"} | set(artwork.art(None)),
            "a one-row book keeps the old shape")
check.that("versions" not in single, "a one-row book carries no versions")

# --- GraphicAudio's library, matched by its own names -------------------------

pilgrimage = run([row("B0GAPILGRM", "Pilgrimage to Hell (Dramatized Adaptation)",
                      authors=("Brandon Sanderson",), narrators=("Full Cast",),
                      publisher="GraphicAudio", content_type="Performance")])[0]
check.that(pilgrimage["owned"], "'001 Pilgrimage To Hell' holds the dramatization, number and all")

# --- which reading the library holds ------------------------------------------

watch_may = row("B0WATCHMAY", "The Last Watch", authors=("J. S. Dewes",), narrators=("Nadia May",), minutes=900)
watch_other = row("B0WATCHOTH", "The Last Watch", authors=("J. S. Dewes",), narrators=("Someone Else",), minutes=600)
hits = run([watch_other, watch_may])
check.equal(len(hits), 1, "two readings of one book are one row")
check.equal(hits[0]["asin"], "B0WATCHMAY", "the row is the reading the library holds, by its reader")
others = hits[0].get("versions") or [{}]
check.that(not others[0].get("owned"), "the other reading is not taken for held")
check.that(others[0].get("askable"), "the other reading can be asked for")
check.equal(hits[0].get("versionSummary"), "Also a reading by Someone Else.",
            "the row names the other reading by its reader")

# --- nothing held: the reading stands for the book, not the dramatization -----

library[:] = [entry for entry in LIBRARY if entry["Id"] not in ("r1", "g1")]
hits = run([GRAPHIC, KRAMER_US])
check.equal(hits[0]["asin"], "B09WRTW2NQ", "with neither held, the reading is the row")
ga = (hits[0].get("versions") or [{}])[0]
check.that(not ga.get("askable"), "the dramatization is never offered where the server refuses them")
check.equal(hits[0].get("versionSummary"),
            "Also a GraphicAudio dramatization, which NextUp does not get.",
            "and the row says so")
library[:] = list(LIBRARY)

# --- the owned rule's limits ---------------------------------------------------

asins, by_title = owned_index(USER)
check.that(not engine._already_owned(
    {"asin": "X1", "title": "Domestication", "authors": ["Someone Else"]}, asins, by_title),
    "another author's 'Domestication' is not the library's")
check.that(not engine._already_owned(
    {"asin": "X2", "title": "Mistborn", "authors": ["Brandon Sanderson"]}, asins, by_title),
    "a named subtitle is another book: 'Mistborn' is not 'The Well of Ascension'")

# --- never asked for --------------------------------------------------------------

listenarr.find_by_asin = lambda asin: None
meta = {"asin": "B0CZ4HN41N", "title": "Domestication (Dramatized Adaptation)",
        "publisher": "Graphic Audio LLC", "authors": [{"name": "Seth Ring"}]}
refused = listenarr.add("B0CZ4HN41N", metadata=meta)
check.that(not refused.ok, "a request for GraphicAudio's dramatization is refused")
check.that("GraphicAudio" in refused.message, "and says why")

check.that(engine._refused({"asin": "B0CZ4HN41N", "publisher": "GraphicAudio"}),
           "Discover passes GraphicAudio's dramatizations over")
check.that(not engine._refused({"asin": "B0POTTER01", "publisher": "Pottermore Publishing",
                                "title": "Harry Potter (Full-Cast Edition)"}),
           "a full-cast edition from another publisher is still offered")

reading = {**KRAMER_US, "version": series._row_version(KRAMER_US)}
drama = {**GRAPHIC, "version": series._row_version(GRAPHIC)}
wanted = series.Version(None, None, "english", frozenset())
check.equal(series._choose([drama], wanted, series.Counter()), None,
            "a series plan never asks for GraphicAudio's, even where it is all that is listed")
check.equal((series._choose([drama, reading], wanted, series.Counter()) or {}).get("asin"),
            "B09WRTW2NQ", "a series plan takes the reading")

# --- the setting off: the server behaves as it always did ------------------------

config.REFUSE_GRAPHIC_AUDIO = False
check.that(not engine._refused({"asin": "B0CZ4HN41N", "publisher": "GraphicAudio"}),
           "with the setting off, Discover offers dramatizations as before")
check.equal((series._choose([drama], wanted, series.Counter()) or {}).get("asin"), "B0CZ4HN41N",
            "with the setting off, a series plan may ask for one as before")
config.REFUSE_GRAPHIC_AUDIO = True

raise SystemExit(check.report())
