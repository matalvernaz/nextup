"""A cast credit alone does not make a recording a dramatization.

Redwall, asked for whole on 2026-11-01, came back with ten of its nineteen
published books. The other nine were held back as "only on Audible in another
version", and eight of them are the same recordings as two that were asked
for: Recorded Books' unabridged readings by Brian Jacques with a cast doing the
voices. Audible credits that cast four ways across the series -- "Full Cast",
"full cast", "a full cast", "A Full Cast" -- sometimes after the author and
sometimes before, and the planner called a recording a dramatization whenever
a credit read exactly "full cast". So "a full cast" was a reading and "Full
Cast" was a dramatization, and one set of recordings was split in two by an
article.

A cast credit is now what `Version` calls the catalogue not saying: it agrees
with either kind. What does say -- a "Dramatized Adaptation" or "Full-Cast
Edition" title, GraphicAudio as the publisher, Audible's "Performance" type --
still holds a book back. And where a book has a recording known to be the kind
wanted beside one the catalogue is silent about, the known one is asked for, so
a radio drama whose cast reads every book of a series does not win the series
from readers who each read one.

The Redwall listing below is the live one (ca marketplace) in the order Audible
sends it, trimmed to the fields the planner reads and to the positions that
show the rule.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("books-series-full-cast-readings")

harness.discard(DB_PATH)

from app import jellyfin, listenarr
from app.books import audible, series, store

store.init()

# Administrators, so the allowance never decides what gets asked for here.
fresh_listener = jellyfin.User(id="user-fresh", name="fresh", is_admin=True)
cast_holder = jellyfin.User(id="user-cast", name="cast", is_admin=True)
drama_holder = jellyfin.User(id="user-drama", name="drama", is_admin=True)
torrent_holder = jellyfin.User(id="user-torrent", name="torrent", is_admin=True)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def attempt(label, fn, want):
    """A check whose subject may not exist, recorded rather than raised.

    So a run against the planner as it was before this rule reads as a list of
    what it got wrong, not as the first missing name.
    """
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001 -- any failure is the finding
        failures.append(f"{label}: raised {type(exc).__name__}: {exc}")
        return
    check(label, got, want)


def row(asin, title, position, series_asin, series_name, narrators, *,
        subtitle=None, publisher="Recorded Books", content_type="Product",
        book_format="unabridged", language="english", release="2004-10-29",
        author="Brian Jacques", minutes=700):
    return {"asin": asin, "title": title, "subtitle": subtitle,
            "authors": [{"name": author}],
            "narrators": [{"name": name} for name in narrators],
            "publisher": publisher, "contentType": content_type,
            "bookFormat": book_format, "language": language,
            "releaseDate": release, "runtimeLengthMin": minutes,
            "series": [{"asin": series_asin, "name": series_name, "position": position}]}


REDWALL = "Redwall"
REDWALL_CA = "B07149JV3S"


def redwall(asin, title, position, narrators, **kw):
    return row(asin, title, position, REDWALL_CA, REDWALL, narrators, **kw)


REDWALL_LISTING = [
    redwall("B071Y193M4", "Redwall", "1", ["Brian Jacques", "Full Cast"],
            subtitle="Redwall, Book 1", publisher="Listening Library"),
    redwall("B07FPRC6JK", "Redwall", "1", ["Stuart Blinder"],
            subtitle="A Tale from Redwall", publisher="Listening Library"),
    redwall("B0725LYHFH", "Mossflower", "2", ["Brian Jacques", "Full Cast"]),
    redwall("B0BDSF829H", "Mossflower", "2", ["Daniel Rigby"], publisher="Puffin"),
    redwall("B072HZLLQH", "Mariel of Redwall", "4", ["Brian Jacques", "a full cast"]),
    redwall("B0BDSHCC53", "Mariel Of Redwall", "4", ["Chloe Pirrie"], publisher="Puffin"),
    redwall("B06ZXTHT3H", "Salamandastron", "5", ["Full Cast"]),
    redwall("B0725MTV3V", "The Bellmaker", "7", ["Brian Jacques", "A Full Cast"]),
    redwall("B0723216GZ", "Outcast of Redwall", "8", ["Brian Jacques", "full cast"]),
    redwall("B0BM4W2KGP", "Pearls of Lutra", "9", [],
            publisher="ZZZ - Series Advisor Placeholder", release="2200-01-01",
            minutes=None),
    redwall("B072HY1YMR", "The Long Patrol", "10", ["Brian Jacques"]),
    redwall("B0716V3NVR", "Triss", "15", ["Brian Jacques", "Full Cast"]),
    redwall("B0711PJZ1N", "The Sable Quean", "21", ["Full Cast", "Brian Jacques"]),
]

# A made-up series shaped like a radio theatre: one cast performs every book,
# credited the way a reading credits its reader, beside a different single
# reader for each book. Nothing in its title, publisher or content type says
# dramatization, so the catalogue is silent about which kind it is.
RADIO = "Wardrobe Chronicles"
RADIO_US = "SER-RADIO"
RADIO_CAST = ["Full Cast", "Paul Scofield", "David Suchet"]


def radio(asin, title, position, narrators, **kw):
    return row(asin, title, position, RADIO_US, RADIO, narrators,
               author="Clive Example", publisher="Radio Theatre", **kw)


RADIO_LISTING = [
    radio("B0RD01", "The First Door", "1", RADIO_CAST),
    radio("B0RN01", "The First Door", "1", ["Michael York"]),
    radio("B0RD02", "The Second Door", "2", RADIO_CAST),
    radio("B0RN02", "The Second Door", "2", ["Kenneth Branagh"]),
    radio("B0RD03", "The Third Door", "3", RADIO_CAST),
    radio("B0RN03", "The Third Door", "3", ["Lynn Redgrave"]),
]

SERIES_BOOKS = {REDWALL_CA: REDWALL_LISTING, RADIO_US: RADIO_LISTING}
CANDIDATES = {
    REDWALL: [{"asin": REDWALL_CA, "name": REDWALL, "region": "ca"}],
    RADIO: [{"asin": RADIO_US, "name": RADIO, "region": "us"}],
}
PRODUCTS = {
    "B072HZLLQH": {"title": "Mariel of Redwall", "_region": "ca",
                   "series": [{"title": REDWALL, "sequence": "4", "asin": REDWALL_CA}]},
    "B0RD01": {"title": "The First Door", "_region": "us",
               "series": [{"title": RADIO, "sequence": "1", "asin": RADIO_US}]},
}


def member(item_id, name, series_name, index, asin, narrators, author):
    people = [{"Name": author, "Type": "Author"}]
    people.extend({"Name": narrator, "Type": "Narrator"} for narrator in narrators)
    item = {"Id": item_id, "Name": name, "SeriesName": series_name,
            "IndexNumber": index, "People": people}
    if asin:
        item["ProviderIds"] = {"Audible": asin}
    return item


LIBRARIES = {
    fresh_listener.id: [],
    # Somebody holding the full-cast reading of book 4, as Matt does.
    cast_holder.id: [
        member("c4", "Mariel of Redwall", REDWALL, 4, "B072HZLLQH",
               ["Brian Jacques", "a full cast"], "Brian Jacques"),
    ],
    # Somebody holding the radio drama of book 1.
    drama_holder.id: [
        member("d1", "The First Door", RADIO, 1, "B0RD01",
               ["Paul Scofield", "David Suchet"], "Clive Example"),
    ],
    # Somebody holding book 5 from a torrent: no Audible id, and the only
    # narrator credit is the cast, so neither the id nor a reader can say
    # which recording it is.
    torrent_holder.id: [
        member("t5", "Salamandastron", REDWALL, 5, None, ["Full Cast"], "Brian Jacques"),
    ],
}

jellyfin.books = lambda uid: [dict(item) for item in LIBRARIES.get(uid, [])]
audible.product = lambda asin: PRODUCTS.get(asin)
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
listenarr.series_candidates = lambda name, region=None: CANDIDATES.get(name, [])


def asins(rows):
    return [c["asin"] for c in rows]


# --- how a cast credit reads ----------------------------------------------------
def version_of(narrators, **fields):
    return series._row_version(
        {"title": "Book", "narrators": [{"name": name} for name in narrators], **fields})


for spelling in ("Full Cast", "full cast", "a full cast", "A Full Cast"):
    attempt(f"a reading credited with {spelling!r} is not called a dramatization",
            lambda: version_of(["Brian Jacques", spelling]).dramatized, None)
    attempt(f"and {spelling!r} is not counted as somebody reading it",
            lambda: version_of(["Brian Jacques", spelling]).narrators,
            frozenset({"brian jacques"}))
attempt("nor when the cast is credited first",
        lambda: version_of(["Full Cast", "Brian Jacques"]).dramatized, None)
attempt("nor when the cast is the only credit",
        lambda: version_of(["Full Cast"]).dramatized, None)
attempt("a dramatized adaptation still says so by its title",
        lambda: series._row_version(
            {"title": "Book (Dramatized Adaptation)",
             "narrators": [{"name": "Full Cast"}]}).dramatized, True)
attempt("GraphicAudio still says so by its publisher",
        lambda: version_of(["Full Cast"], publisher="Graphic Audio LLC").dramatized, True)
attempt("a performance still says so by Audible's type",
        lambda: version_of(["Full Cast"], contentType="Performance").dramatized, True)
attempt("a full-cast edition still says so by its title",
        lambda: series._row_version(
            {"title": "Harry Potter and the Chamber of Secrets (Full-Cast Edition)",
             "narrators": [{"name": "Full Cast"}]}).dramatized, True)

# --- Redwall, for somebody who holds none of it ---------------------------------
fresh = series.plan(fresh_listener, REDWALL)
attempt("no book is held back for being read with a cast",
        lambda: asins(fresh["otherVersion"]), [])
attempt("every published book is asked for: a single reader where Audible has one, "
        "the full-cast reading where it is the only recording",
        lambda: asins(fresh["missing"]),
        ["B07FPRC6JK", "B0BDSF829H", "B0BDSHCC53", "B06ZXTHT3H", "B0725MTV3V",
         "B0723216GZ", "B072HY1YMR", "B0716V3NVR", "B0711PJZ1N"])
check("the placeholder is still not out", asins(fresh["notOut"]), ["B0BM4W2KGP"])
attempt("and the search row has nothing to explain",
        lambda: series._search_row(fresh)["detail"],
        "None of the 9 in your library, 9 to ask for.")

# --- Redwall, for somebody who holds a full-cast reading --------------------------
holder = series.plan(cast_holder, REDWALL)
check("the held book is held", asins(holder["have"]), ["B072HZLLQH"])
attempt("the rest come in the same voice wherever Audible has it",
        lambda: asins(holder["missing"]),
        ["B071Y193M4", "B0725LYHFH", "B06ZXTHT3H", "B0725MTV3V", "B0723216GZ",
         "B072HY1YMR", "B0716V3NVR", "B0711PJZ1N"])
attempt("and nothing is held back", lambda: asins(holder["otherVersion"]), [])

# --- Redwall, for somebody whose copy says nothing but its cast -------------------
torrent = series.plan(torrent_holder, REDWALL)
check("the torrented book is held", asins(torrent["have"]), ["B06ZXTHT3H"])
attempt("its cast credit does not make the rest of the series a dramatization: "
        "the readings are still asked for",
        lambda: asins(torrent["otherVersion"]), [])

# --- a radio drama does not take a series from its readers ------------------------
readers = series.plan(fresh_listener, RADIO)
check("a reading known to be one is asked for over a cast the catalogue is silent about",
      asins(readers["missing"]), ["B0RN01", "B0RN02", "B0RN03"])
check("and the drama is not held back either: there was a reading to ask for instead",
      asins(readers["otherVersion"]), [])

dramas = series.plan(drama_holder, RADIO)
check("somebody who holds the drama gets the drama, by its cast",
      asins(dramas["missing"]), ["B0RD02", "B0RD03"])

harness.discard(DB_PATH)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("ok")
