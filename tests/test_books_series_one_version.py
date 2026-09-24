"""Asking for the rest of a series asks for one recording of it.

Audible files every recording of a book at the same position in a series: the
other marketplace's copy of the same reading, a re-recording by a new narrator,
an abridgement, and GraphicAudio's dramatization. The planner took whichever
row came first, so the recording was the listing's accident. Battle Mage
Farmer, asked for from Michael Kramer's first two books on 2026-09-24, came
back with book 5 as "Transformation (Dramatized Adaptation)": the us listing
puts that one ahead of Kramer's reading at position 5 and nowhere else. The
listings below are those live ones, in the order Audible sends them, trimmed
to the fields the planner reads.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("books-series-one-version")

harness.discard(DB_PATH)

from app import jellyfin, listenarr
from app import store as ledger
from app.books import audible, series, store

store.init()

# Administrators, so the allowance never decides what gets asked for here.
matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)
newcomer = jellyfin.User(id="user-new", name="newcomer", is_admin=True)
pike_fan = jellyfin.User(id="user-pike", name="pike", is_admin=True)
cast_fan = jellyfin.User(id="user-cast", name="cast", is_admin=True)
marked_fan = jellyfin.User(id="user-marked", name="marked", is_admin=True)

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


KRAMER = ["Michael Kramer"]
KR = ["Kate Reading", "Michael Kramer"]


def row(asin, title, position, series_asin, series_name, narrators, *,
        subtitle=None, publisher="Recorded Books", content_type="Product",
        book_format="unabridged", language="english", release="2022-01-01",
        author="Seth Ring", minutes=720):
    return {"asin": asin, "title": title, "subtitle": subtitle,
            "authors": [{"name": author}],
            "narrators": [{"name": name} for name in narrators],
            "publisher": publisher, "contentType": content_type,
            "bookFormat": book_format, "language": language,
            "releaseDate": release, "runtimeLengthMin": minutes,
            "series": [{"asin": series_asin, "name": series_name, "position": position}]}


def cast(*names):
    return ["full cast", *names]


BMF = "Battle Mage Farmer"
BMF_US = "B09WTKBF53"
BMF_CA = "B09WTLP4Z5"


def bmf(asin, title, position, narrators, series_asin=BMF_US, **kw):
    return row(asin, title, position, series_asin, BMF, narrators, **kw)


def ga(asin, title, position, series_asin=BMF_US, **kw):
    kw.setdefault("publisher", "Graphic Audio LLC")
    kw.setdefault("content_type", "Performance")
    return bmf(asin, title, position, cast("Peter Holdway", "Wyn Delano", "Jenna Sharpe"),
               series_asin=series_asin, **kw)


# The us listing, in the order it arrived. Dramatization second everywhere
# but position 5.
US = [
    bmf("B09WRTK4DS", "Domestication", "1", KRAMER),
    ga("B0CZ4NDBSK", "Domestication (Dramatized Adaptation)", "1"),
    bmf("B0B75K91ZL", "Germination", "2", KRAMER),
    ga("B0D1ZWWZQJ", "Germination (Dramatized Adaptation)", "2"),
    bmf("B0BJW6XXFM", "Cultivation", "3", KRAMER),
    ga("B0DDCVCVKN", "Cultivation (Dramatized Adaptation)", "3"),
    bmf("B0BVDVSJMR", "Fermentation", "4", KRAMER),
    ga("B0DJMZB8BS", "Fermentation (Dramatized Adaptation)", "4"),
    ga("B0FNP14T9P", "Transformation (Dramatized Adaptation)", "5"),
    bmf("B0C29G3MR4", "Transformation", "5", KRAMER),
    bmf("B0C9SNYNLT", "Preservation", "6", KRAMER),
    # GraphicAudio under a second spelling, as an ordinary product: only its
    # title and its publisher say what it is.
    ga("B0GVHPR164", "Preservation [Dramatized Adaptation]", "6",
       publisher="GraphicAudio", content_type="Product"),
    bmf("B0CQQGL5YH", "Separation: A Fantasy LitRPG Adventure", "7", KRAMER),
    bmf("B0CV4MZSYH", "Conservation", "8", KRAMER),
    bmf("B0D82C9MV5", "Culmination: A Fantasy LitRPG Adventure", "9", KRAMER),
]

# The ca listing of the same series, where the dramatization comes first at
# four positions of the six it has.
CA = [
    ga("B0CZ4HN41N", "Domestication (Dramatized Adaptation)", "1", BMF_CA),
    bmf("B09WRTW2NQ", "Domestication", "1", KRAMER, BMF_CA),
    bmf("B0B75NR1VS", "Germination", "2", KRAMER, BMF_CA),
    ga("B0D213Z1CH", "Germination (Dramatized Adaptation)", "2", BMF_CA),
    ga("B0DDCWMNNY", "Cultivation (Dramatized Adaptation)", "3", BMF_CA),
    bmf("B0BJW4TKTK", "Cultivation", "3", KRAMER, BMF_CA),
    ga("B0DJMZY8CD", "Fermentation (Dramatized Adaptation)", "4", BMF_CA),
    bmf("B0BVD1PLZD", "Fermentation", "4", KRAMER, BMF_CA),
    ga("B0FNNMHZD4", "Transformation (Dramatized Adaptation)", "5", BMF_CA),
    bmf("B0C29KZ5HT", "Transformation", "5", KRAMER, BMF_CA),
    bmf("B0C9SP4VPV", "Preservation", "6", KRAMER, BMF_CA),
    ga("B0GVHQ82RR", "Preservation [Dramatized Adaptation]", "6", BMF_CA,
       publisher="GraphicAudio", content_type="Product"),
    bmf("B0CQPKYBJD", "Separation: A Fantasy LitRPG Adventure", "7", KRAMER, BMF_CA),
    bmf("B0CV4MSBBV", "Conservation", "8", KRAMER, BMF_CA),
    bmf("B0D82DY2YF", "Culmination: A Fantasy LitRPG Adventure", "9", KRAMER, BMF_CA),
]

WOT = "The Wheel of Time"
WOT_CA = "B071VQSWSF"


def wot(asin, title, position, narrators, **kw):
    kw.setdefault("publisher", "Macmillan Audio")
    kw.setdefault("author", "Robert Jordan")
    return row(asin, title, position, WOT_CA, WOT, narrators, **kw)


# The ca listing: Rosamund Pike's re-recordings sit beside Kramer and Reading's
# at 1 to 4, first at three of them; an abridgement by another reader at 5; a
# bundle at 6.
WHEEL = [
    wot("B072325KCX", "New Spring", "0", KR),
    wot("B09JT5KS5K", "The Eye of the World", "1", ["Rosamund Pike"]),
    wot("B072FF6JLC", "The Eye of the World", "1", KR),
    wot("B0B7KJCW22", "The Great Hunt", "2", ["Rosamund Pike"]),
    wot("B071Z5TGW8", "The Great Hunt", "2", KR),
    wot("B071NYTTSK", "The Dragon Reborn", "3", KR),
    wot("B0B7KLM4WY", "The Dragon Reborn", "3", ["Rosamund Pike"]),
    wot("B0D94V85S5", "The Shadow Rising", "4", ["Rosamund Pike"]),
    wot("B071F6P26V", "The Shadow Rising", "4", KR),
    wot("B0714985KY", "The Fires of Heaven", "5", KR),
    wot("B0711MDSYQ", "The Fires of Heaven", "5", ["Mark Rolston"],
        publisher="The Publishing Mills", book_format="abridged", minutes=187),
    wot("B0711PP5B1", "Lord of Chaos", "6", KR),
    wot("B07HKP8MJL", "Lord of Chaos", "6", KR, minutes=None),
    wot("B0719GJXJD", "A Crown of Swords", "7", KR),
    wot("B0725MXNNW", "Path of Daggers", "8", ["Michael Kramer", "Kate Reading"]),
]

SERIES_BOOKS = {BMF_US: US, BMF_CA: CA, WOT_CA: WHEEL}
CANDIDATES = {
    BMF: [{"asin": BMF_CA, "name": BMF, "region": "ca"}],
    WOT: [{"asin": WOT_CA, "name": WOT, "region": "ca"}],
}
PRODUCTS = {
    "B09WRTK4DS": {"title": "Domestication", "_region": "us",
                   "series": [{"title": BMF, "sequence": "1", "asin": BMF_US}]},
    "B0CZ4NDBSK": {"title": "Domestication (Dramatized Adaptation)", "_region": "us",
                   "series": [{"title": BMF, "sequence": "1", "asin": BMF_US}]},
    "B09JT5KS5K": {"title": "The Eye of the World", "_region": "ca",
                   "series": [{"title": WOT, "sequence": "1", "asin": WOT_CA}]},
}


def member(item_id, name, series_name, index, asin=None, narrator=None,
           author="Seth Ring"):
    people = [{"Name": author, "Type": "Author"}]
    if narrator:
        people.append({"Name": narrator, "Type": "Narrator"})
    item = {"Id": item_id, "Name": name, "SeriesName": series_name,
            "IndexNumber": index, "People": people}
    if asin:
        item["ProviderIds"] = {"Audible": asin}
    return item


LIBRARIES = {
    # As the live library holds them: the first with its Audible id, the
    # second with only its reader to say which recording it is.
    matt.id: [
        member("m1", "Domestication : A Fantasy LitRPG Adventure (Battle Mage Farmer)",
               BMF, 1, asin="B09WRTK4DS", narrator="Michael Kramer"),
        member("m2", "Germination : A Fantasy LitRPG Adventure (Battle Mage Farmer)",
               BMF, 2, narrator="Michael Kramer"),
    ],
    newcomer.id: [],
    pike_fan.id: [
        member("p1", "The Eye of the World", WOT, 1, asin="B09JT5KS5K",
               narrator="Rosamund Pike", author="Robert Jordan"),
    ],
    # Somebody who bought GraphicAudio's first book of the series.
    cast_fan.id: [
        member("c1", "Domestication (Dramatized Adaptation)", BMF, 1, asin="B0CZ4NDBSK"),
    ],
    # The same, torrented: no id and no cast, only the name.
    marked_fan.id: [
        member("k1", "Domestication (Dramatized Adaptation)", BMF, 1),
    ],
}

jellyfin.books = lambda uid: [dict(item) for item in LIBRARIES.get(uid, [])]
audible.product = lambda asin: PRODUCTS.get(asin)
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
listenarr.series_candidates = lambda name, region=None: CANDIDATES.get(name, [])

added = []


def fake_add(asin, monitored=True, metadata=None):
    added.append(asin)
    return listenarr.AddResult(True, "Sent to Listenarr", len(added),
                               (metadata or {}).get("title") or "", ())


listenarr.add = fake_add
listenarr.enqueue_search = lambda audiobook_id: True


def asins(rows):
    return [c["asin"] for c in rows]


# Every plan first: asking records requests, and a request on order changes
# what the next plan says.

# --- Matt's case: two of Kramer's readings held ------------------------------
kramer = series.plan(matt, BMF, anchor_item_id="m1")
check("the series resolves through the id-carrying book", kramer["seriesAsin"], BMF_US)
check("every gap is Kramer's reading, book 5 included",
      asins(kramer["missing"]),
      ["B0BJW6XXFM", "B0BVDVSJMR", "B0C29G3MR4", "B0C9SNYNLT", "B0CQQGL5YH",
       "B0CV4MZSYH", "B0D82C9MV5"])
attempt("nothing is left out for being another recording",
        lambda: kramer["otherVersion"], [])

# --- nobody holds any of it: a reading, wherever Audible has one --------------
fresh = series.plan(newcomer, BMF)
check("a series nobody holds is resolved from the catalogue", fresh["seriesAsin"], BMF_CA)
check("and asked for as Kramer's reading, where the dramatization is listed first",
      asins(fresh["missing"]),
      ["B09WRTW2NQ", "B0B75NR1VS", "B0BJW4TKTK", "B0BVD1PLZD", "B0C29KZ5HT",
       "B0C9SP4VPV", "B0CQPKYBJD", "B0CV4MSBBV", "B0D82DY2YF"])

# --- one voice where the catalogue offers two ---------------------------------
wheel = series.plan(newcomer, WOT)
check("the reader of most of the series reads all of it, unabridged",
      asins(wheel["missing"]),
      ["B072325KCX", "B072FF6JLC", "B071Z5TGW8", "B071NYTTSK", "B071F6P26V",
       "B0714985KY", "B0711PP5B1", "B0719GJXJD", "B0725MXNNW"])

pike = series.plan(pike_fan, WOT)
check("somebody holding the re-recording gets the re-recording where there is one",
      asins(pike["missing"]),
      ["B072325KCX", "B0B7KJCW22", "B0B7KLM4WY", "B0D94V85S5", "B0714985KY",
       "B0711PP5B1", "B0719GJXJD", "B0725MXNNW"])
attempt("and a book only the first narrator has read is still asked for, not held back",
        lambda: pike["otherVersion"], [])

# --- somebody who holds the dramatization gets dramatizations -----------------
dramatized = series.plan(cast_fan, BMF)
check("a held dramatization picks dramatizations",
      asins(dramatized["missing"]),
      ["B0D1ZWWZQJ", "B0DDCVCVKN", "B0DJMZB8BS", "B0FNP14T9P", "B0GVHPR164"])
attempt("the books GraphicAudio has not adapted are held back, not read to them instead",
        lambda: asins(dramatized["otherVersion"]),
        ["B0CQQGL5YH", "B0CV4MZSYH", "B0D82C9MV5"])
attempt("and why", lambda: dramatized["otherVersionReason"],
        "not on Audible as a dramatized adaptation")
check("the search row counts them and says so",
      series._search_row(dramatized)["detail"],
      "1 of 9 in your library, 3 not on Audible as a dramatized adaptation, "
      "5 to ask for.")

named = series.plan(marked_fan, BMF)
check("a dramatization with no id is known by its name",
      asins(named["missing"]),
      ["B0D213Z1CH", "B0DDCWMNNY", "B0DJMZY8CD", "B0FNNMHZD4", "B0GVHQ82RR"])

# --- a book listed only as another recording ----------------------------------
LONE = "Lone Recording Saga"
LONE_SERIES = "SER-LONE"
SERIES_BOOKS[LONE_SERIES] = [
    row("B0LR01", "Lone One", "1", LONE_SERIES, LONE, KRAMER, author="Lone Author"),
    row("B0LR02", "Lone Two", "2", LONE_SERIES, LONE, KRAMER, author="Lone Author"),
    row("B0LR03", "Lone Three (Dramatized Adaptation)", "3", LONE_SERIES, LONE,
        cast("Peter Holdway"), publisher="Graphic Audio LLC", content_type="Performance",
        author="Lone Author"),
    # A Spanish edition where the English one should be.
    row("B0LR04", "Solo Cuatro", "4", LONE_SERIES, LONE, ["Francesc Belda"],
        language="spanish", author="Lone Author"),
    # Announced only as a dramatization: not a book this reader is waiting on.
    row("B0LR05", "Lone Five (Dramatized Adaptation)", "5", LONE_SERIES, LONE, [],
        publisher="ZZZ - Series Advisor Placeholder", release="2200-01-01",
        author="Lone Author"),
    # Out in one marketplace, a placeholder in the other: one book, asked for.
    row("B0LR06", "Lone Six", "6", LONE_SERIES, LONE, KRAMER, author="Lone Author"),
    row("B0LR06CA", "Lone Six", "6", LONE_SERIES, LONE, [],
        publisher="ZZZ - Series Advisor Placeholder", release="2200-01-01",
        author="Lone Author"),
    # Announced in this recording: not out yet, not another recording.
    row("B0LR07", "Lone Seven", "7", LONE_SERIES, LONE, [],
        publisher="ZZZ - Series Advisor Placeholder", release="2200-01-01",
        author="Lone Author"),
    row("B0LR07GA", "Lone Seven (Dramatized Adaptation)", "7", LONE_SERIES, LONE,
        cast("Wyn Delano"), publisher="Graphic Audio LLC", content_type="Performance",
        author="Lone Author"),
    # On order from one marketplace, a placeholder in the other: on its way,
    # and nothing else.
    row("B0LR08", "Lone Eight", "8", LONE_SERIES, LONE, KRAMER, author="Lone Author"),
    row("B0LR08CA", "Lone Eight", "8", LONE_SERIES, LONE, [],
        publisher="ZZZ - Series Advisor Placeholder", release="2200-01-01",
        author="Lone Author"),
]
PRODUCTS["B0LR01"] = {"title": "Lone One", "_region": "us",
                      "series": [{"title": LONE, "sequence": "1", "asin": LONE_SERIES}]}
LIBRARIES[matt.id].append(member("l1", "Lone One", LONE, 1, asin="B0LR01",
                                 narrator="Michael Kramer", author="Lone Author"))

store.record_request(newcomer.key, "B0LR08", "Lone Eight", ["Lone Author"])

lone = series.plan(matt, LONE)
check("only the books in the recording held are gaps", asins(lone["missing"]),
      ["B0LR02", "B0LR06"])
attempt("a dramatization and a translation are held back",
        lambda: asins(lone["otherVersion"]), ["B0LR03", "B0LR04"])
attempt("for two reasons, which are not one sentence",
        lambda: lone["otherVersionReason"], "only on Audible in another version")
check("only this recording's announcement is not out yet", asins(lone["notOut"]),
      ["B0LR07"])
check("a book on order is on order and nothing else", asins(lone["onOrder"]),
      ["B0LR08"])

# --- how a recording is recognised -------------------------------------------
def dramatized_row(listing_row):
    return series._row_version(listing_row).dramatized


attempt("GraphicAudio by its title",
        lambda: dramatized_row({"title": "Book [Dramatized Adaptation]"}), True)
attempt("by its publisher alone",
        lambda: dramatized_row({"title": "Book", "publisher": "Graphic Audio LLC"}), True)
attempt("anybody's performance by Audible's content type",
        lambda: dramatized_row({"title": "Book", "contentType": "Performance"}), True)
attempt("a cast list by its first name",
        lambda: dramatized_row({"title": "Book", "narrators": [{"name": "Full Cast"}]}),
        True)
attempt("a full-cast edition by its title",
        lambda: dramatized_row(
            {"title": "Harry Potter and the Chamber of Secrets (Full-Cast Edition)"}), True)
attempt("and a reading is none of those", lambda: dramatized_row(US[0]), False)
attempt("two marketplaces' copies of one reading are one voice",
        lambda: series._row_version(
            {"narrators": [{"name": "Michael Kramer"}, {"name": "Kate Reading"}]}).narrators
        == series._row_version(
            {"narrators": [{"name": "Kate Reading"}, {"name": "Michael Kramer"}]}).narrators,
        True)

# --- asking: what reaches Listenarr -------------------------------------------
asked = series.want_series(matt, BMF, anchor_item_id="m1")
check("Kramer's book 5 is the one sent", "B0C29G3MR4" in added, True)
DRAMATIZATIONS = {r["asin"] for r in US + CA if "Dramatized" in r["title"]}
check("no dramatization is sent", [a for a in added if a in DRAMATIZATIONS], [])
attempt("the answer counts nothing held back", lambda: asked["otherVersionCount"], 0)

# A book on order in any recording is on order for the whole household, by
# the same rule that makes one held in any recording held. So the next
# listener starts from an empty ledger, or Matt's requests would answer for
# theirs.
with ledger.db() as conn:
    conn.execute("DELETE FROM requests")

cast_asked = series.want_series(cast_fan, BMF)
check("the dramatizations are what is sent for somebody who holds one",
      [c["asin"] for c in cast_asked["requested"]],
      ["B0D1ZWWZQJ", "B0DDCVCVKN", "B0DJMZB8BS", "B0FNP14T9P", "B0GVHPR164"])
check("and the answer names the three it did not ask for", cast_asked["message"],
      "Asked for 5 books from Battle Mage Farmer: Germination (Dramatized Adaptation), "
      "Cultivation (Dramatized Adaptation), Fermentation (Dramatized Adaptation), "
      "Transformation (Dramatized Adaptation), Preservation [Dramatized Adaptation]. "
      "3 books are not on Audible as a dramatized adaptation, so they were not asked "
      "for: Separation: A Fantasy LitRPG Adventure, Conservation, Culmination: "
      "A Fantasy LitRPG Adventure.")
attempt("which it counts", lambda: cast_asked["otherVersionCount"], 3)

again = series.want_series(cast_fan, BMF)
check("and says again, with nothing left to ask for", again["message"],
      "You have 1 book of Battle Mage Farmer. Another 5 books are already being looked "
      "for. 3 books are not on Audible as a dramatized adaptation, so they were not "
      "asked for: Separation: A Fantasy LitRPG Adventure, Conservation, Culmination: "
      "A Fantasy LitRPG Adventure.")
attempt("a row with nothing left to ask for does not claim the whole series",
        lambda: series.state_sentence(6, 0, 0, 3, "not on Audible as a dramatized adaptation"),
        "6 of 9 in your library, 3 not on Audible as a dramatized adaptation. "
        "Nothing left to ask for.")
check("and one with no other recording in it reads as it always did",
      series.state_sentence(2, 0, 7), "2 of 9 in your library, 7 to ask for.")

harness.discard(DB_PATH)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("ok")
