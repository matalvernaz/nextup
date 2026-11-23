"""Direct catalogue search, for the book somebody already has in mind.

The shelves answer "what should I read next". This answers "do we have, or can
we get, *this*" -- which no amount of similarity ranking can, because the whole
point is that the listener names the book.

Audible's own `?keywords=` returns nothing unauthenticated, so the query goes
through Listenarr, whose provider is authenticated. That is the same route the
keyword-discovery channel already uses; it borrows Listenarr's search, never its
library, so Jellyfin remains the single library of record.

Owned books are *marked*, not hidden. On the Discover shelf an owned book is
noise and is filtered out. In a search it is the answer: somebody typing a title
wants to know it is already here, and silence would read as "we cannot get it".
"""
import html
import re

from .. import artwork, config, jellyfin, listenarr, logs
from . import engine, series, shelves, versions, wants

log = logs.get(__name__)

# Where one block of the blurb ends and the next begins. Substituted before the
# rest of the tags are dropped, or eight paragraphs arrive as one unbroken
# sentence with nothing in it for a screen reader to pause on.
_BLOCK_END = re.compile(r"(?i)</p\s*>|<br\s*/?>|</li\s*>|</div\s*>|</h[1-6]\s*>")
_TAG = re.compile(r"<[^>]+>")
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*")


#: How many catalogue rows a search reads for each book it shows. The stores'
#: copies of one recording and a book's other recordings fold into one row,
#: and a search should still fill its page.
_FOLD_ROOM = 3


def search(user: jellyfin.User, query: str, limit: int | None = None) -> list[dict]:
    """Catalogue hits for a free-text query, one per book, each flagged owned or requested.

    No descriptions: a blurb is one Audible request per book and a query of
    twenty-five would pay twenty-five of them for text nobody has asked to read.
    They come from `summary()` when a listener opens one.

    One row per book, not per catalogue row (Matt, 2026-11-22: "There shouldn't
    be anything duplicated, it should just give you the option later"). A row
    stands for one recording and its fields are that recording's own; a book
    with others carries them in `versions`, with a sentence about them in
    `versionSummary`. A book with one recording has neither, and reads exactly
    as before.
    """
    query = (query or "").strip()
    if not query:
        return []
    wanted = limit or config.SEARCH_LIMIT
    rows = listenarr.audible_search(query, limit=wanted * _FOLD_ROOM)
    asins, by_title = shelves.owned_index(user)
    # Outstanding only. A request that has arrived is described by `owned`, and
    # showing both would have a book claim to be on order and on the shelf.
    requested = {r["asin"] for r in wants.states(user.key, (asins, by_title))
                 if r["state"] != wants.IN_LIBRARY}
    held = getattr(by_title, "held", None)
    results = []
    for book in versions.group(rows):
        results.append(_book_hit(book, asins, by_title, requested, held))
        if len(results) >= wanted:
            break
    log.info("search user=%s query=%r hits=%d owned=%d",
             user.key, query, len(results), sum(1 for r in results if r["owned"]))
    return results


def _hit(row: dict, asins: set, by_title, requested: set) -> dict:
    """One catalogue row as a search hit, in the shape search has always returned."""
    asin = row["asin"]
    hit = {
        "asin": asin,
        "title": (row.get("title") or "").strip(),
        "authors": [a.get("name", "") for a in (row.get("authors") or []) if a.get("name")],
        "narrators": [n.get("name", "") for n in (row.get("narrators") or []) if n.get("name")],
        "runtimeMinutes": row.get("lengthMinutes"),
        # The cover Listenarr's own search returned, so a row can show one
        # without an Audible request per hit.
        **artwork.art(row.get("imageUrl")),
    }
    hit["owned"] = engine._already_owned(
        {"asin": asin, "title": hit["title"], "authors": hit["authors"]},
        asins, by_title)
    hit["requested"] = asin in requested
    return hit


def _recording_hit(copies: list[dict], asins: set, by_title, requested: set,
                   held, several: bool) -> tuple[dict, dict]:
    """The hit one recording stands as, and what it says about itself as a version.

    The stores' copies of one recording are one: the row is the copy the
    library holds, else the first listed, and it is owned or asked for when
    either copy is. Where the book has more than one recording, a title match
    says only "some version is held", so the reader and the length decide
    which, and a GraphicAudio dramatization is held when its own library has it.
    """
    chosen = next((row for row in copies if (row.get("asin") or "").upper() in asins), copies[0])
    hit = _hit(chosen, asins, by_title, requested)
    copy_asins = [row["asin"] for row in copies]
    graphic = versions.is_graphic_audio(chosen)
    hit["owned"] = any(
        _hit(row, asins, by_title, requested)["owned"] for row in copies) or hit["owned"]
    if graphic and held is not None and held.graphic_audio and held.graphic_audio.holds(chosen):
        hit["owned"] = True
    if several and hit["owned"] and not graphic and held is not None and not (
            {asin.upper() for asin in copy_asins} & asins):
        readers = set(series._readers(hit["narrators"]))
        authors = {engine._norm_author(a) for a in hit["authors"]}
        if held.recordings.holds(hit["title"], authors, readers, hit["runtimeMinutes"]) is False:
            hit["owned"] = False
    hit["requested"] = any(asin in requested for asin in copy_asins)
    version_flags = series._row_version(chosen)
    version = {
        "asin": hit["asin"],
        "asins": copy_asins,
        "title": hit["title"],
        "narrators": hit["narrators"],
        "runtimeMinutes": hit["runtimeMinutes"],
        "graphicAudio": graphic,
        "dramatized": bool(version_flags.dramatized),
        "abridged": bool(version_flags.abridged),
        "owned": hit["owned"],
        "requested": hit["requested"],
        "askable": not hit["owned"] and not hit["requested"] and not versions.refuses(chosen),
    }
    version["label"] = versions.label(version)
    return hit, version


def _book_hit(book: list[list[dict]], asins: set, by_title, requested: set, held) -> dict:
    """One search row for one book, standing for the recording that best answers it.

    Held first (a reading before a dramatization when both are), then asked
    for, then the one to ask for: a reading over a dramatization, unabridged
    over abridged, in the catalogue's order. GraphicAudio's is the row only
    when it is all the book has.
    """
    several = len(book) > 1
    pairs = [_recording_hit(copies, asins, by_title, requested, held, several) for copies in book]

    def rank(pair):
        hit, version = pair
        return (
            not version["owned"],
            not version["requested"],
            version["graphicAudio"],
            version["dramatized"],
            version["abridged"],
        )

    order = sorted(range(len(pairs)), key=lambda position: (rank(pairs[position]), position))
    hit, _ = pairs[order[0]]
    if several:
        others = [pairs[position][1] for position in order[1:]]
        hit["versions"] = others
        hit["versionSummary"] = versions.summary(others)
    return hit


def summary(asin: str) -> dict:
    """One book's blurb, fetched when somebody asks to hear it.

    Audible gives a short `merchandising_summary` and a longer
    `publisher_summary`; the longer one is preferred here because this is the
    surface where somebody has explicitly asked for more, and the short one is
    what the shelf row already implies.
    """
    product = store_backed_product(asin) or {}
    text = _plain(product.get("publisher_summary")
                  or product.get("merchandising_summary") or "")
    return {
        "asin": asin,
        "title": (product.get("title") or "").strip(),
        "authors": [a.get("name", "") for a in (product.get("authors") or []) if a.get("name")],
        "runtimeMinutes": product.get("runtime_length_min"),
        "summary": text,
        # On the summary because it comes off the same product lookup, so a
        # client that wants to play it pays no second request for the address.
        "sampleUrl": sample_url(product),
        # The cover, off the same lookup for the same reason.
        **artwork.art(artwork.audible_cover(product)),
    }


def sample_url(product: dict) -> str | None:
    """Audible's free preview of a book, or None where it has not published one.

    A five-minute MP3 on Audible's own CDN, played straight from there by the
    phone or the browser. Only an https address is passed on, because whatever
    this returns is what a client will fetch.
    """
    url = (product.get("sample_url") or "").strip()
    return url if url.startswith("https://") else None


def _plain(markup: str) -> str:
    """Audible's blurb as something a screen reader can read aloud.

    Both summaries come back as HTML -- paragraphs, bold, italics -- and neither
    surface that shows one renders any of it: the search page assigns it to
    `textContent` and EchoFin draws it in a `Text`. So the markup was being
    spoken as words. Entities are unescaped last, so an `&lt;` that was text in
    the blurb stays text rather than becoming a tag one pass too late to strip.
    """
    text = _TAG.sub("", _BLOCK_END.sub("\n\n", markup))
    text = html.unescape(text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def store_backed_product(asin: str):
    """Indirection so a test can stand in for the network without a fixture DB."""
    from . import audible
    return audible.product(asin)
