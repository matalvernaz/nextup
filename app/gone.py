"""What to clear when something is deleted from the library.

Deleting a film in a Jellyfin client removes the file and tells nothing else.
Radarr goes on holding a monitored row for it, so the next missing-search or
RSS sweep downloads it again -- and to whoever deleted it, it simply came back.
Sonarr is the same. Listenarr is milder only by accident: a book that arrived
was unmonitored on arrival, so its stale row does not re-acquire, but it is
still a row pointing at a hole.

Two decisions shape everything here.

**The acquisition tool is the thing to look in, not the ledger.** Nextup has
only existed since 2026-08-30; almost everything in Radarr and Sonarr predates
it and was never asked for through this service. Measured 2026-09-16, Jellyfin
holds 429 films and 130 series against Radarr's 44 and Sonarr's 66 -- so a
ledger-first implementation would answer "nothing to clear" for most of the
library while looking like a working feature. Resolution is therefore by
provider id straight against the tool, and a ledger row is tidied up as a
side effect where one happens to exist.

**A client's word is not taken for it.** The caller says a thing was deleted;
this module checks. Jellyfin is re-read for the provider id -- not for the item
id, which proves only that *some* item went and is no protection at all when a
library carries a wrong TMDB id, which this one does in places. Nothing is
cleared while anybody is still waiting, and files are never touched on either
side.
"""
from . import jellyfin, listenarr, logs, media, radarr, sonarr, store
from .books import engine as book_engine
from .books import store as book_store
from .books import wants as book_wants

log = logs.get("gone")


class StillHere(Exception):
    """The thing is still in the library, so nothing is cleared."""


class Unsettled(Exception):
    """Jellyfin could not be asked, so nothing is cleared.

    Its own type because the answer is a 503 and not a refusal: nothing about
    the request was wrong, and a client that retries later is right to.
    """


#: Jellyfin item types this understands, and the medium each belongs to.
#:
#: `Episode` and `Season` are deliberately absent. A ledger key and a Sonarr row
#: are both series-level, so the only thing this module could do with a deleted
#: episode is unmonitor the whole series -- which is worse than doing nothing,
#: and doing nothing is what an unknown type gets.
#:
#: Music is absent for a different reason: a buskarr key is a catalogue source
#: and reference (`bk:album:<source>:<ref>`) or, for a track, a digest of the
#: artist and title. Jellyfin holds neither, so there is nothing to resolve
#: against. Named here rather than left to be discovered.
MEDIUM_OF_TYPE = {
    "Movie": media.MOVIE,
    "Series": media.SERIES,
    "AudioBook": media.BOOK,
    "Book": media.BOOK,
}


def clear(item: dict) -> dict:
    """Clear whatever is still pointing at a thing that has been deleted.

    `item` is the deleted item as the client last saw it: `type`, `name`,
    `providerIds`, and `authors` for a book. The Jellyfin id is accepted and
    logged but never relied on -- by the time this is called it names nothing.

    Returns the report, which is always a valid answer: a deletion this service
    has nothing to say about is the ordinary case, not a failure.
    """
    kind = (item.get("type") or "").strip()
    medium = MEDIUM_OF_TYPE.get(kind)
    if medium is None:
        log.info("deleted type=%r id=%s: nothing this service tracks",
                 kind, item.get("itemId"))
        return _report(medium=None, cleared=None)

    if medium == media.BOOK:
        return _clear_book(item)
    return _clear_by_provider_id(medium, item)


# --- Films and series --------------------------------------------------------


def _clear_by_provider_id(medium: str, item: dict) -> dict:
    """The film and series path, which a provider id settles outright."""
    provider_ids = item.get("providerIds") or {}
    provider_id = str(
        provider_ids.get("Tmdb" if medium == media.MOVIE else "Tvdb") or "").strip()
    if not provider_id:
        # Not an error. A film with no TMDB id was never askable through this
        # service and cannot be in Radarr under an id nobody has, so there is
        # genuinely nothing to look for.
        log.info("deleted medium=%s name=%r: no provider id, nothing to clear",
                 medium, item.get("name"))
        return _report(medium=medium, cleared=None)

    _require_absent(medium, provider_id)
    item_key = (radarr.item_key(provider_id) if medium == media.MOVIE
                else sonarr.item_key(provider_id))
    _require_nobody_waiting(medium, item_key)

    row = _backend_row(medium, provider_id)
    if row is None:
        # The ordinary case for most of this library: deleted something the
        # acquisition tool never held. The ledger is still swept, because a
        # request can outlive the arr row that served it.
        dropped = store.drop_settled(medium, item_key)
        log.info("deleted medium=%s key=%s: no backend row, ledger rows=%d",
                 medium, item_key, dropped)
        return _report(medium=medium, cleared=None, item_key=item_key,
                       ledger_rows=dropped)

    if not _has_landed(medium, row):
        # Nothing has been downloaded yet, so the row is not a leftover -- it
        # is work in progress that happens to share an id with something that
        # was in the library. Left alone.
        log.info("deleted medium=%s key=%s: backend row holds no file, kept",
                 medium, item_key)
        raise StillHere(
            "That is still being acquired, so it has been left alone.")

    stopped = _stop(medium, str(row.get("id") or ""))
    dropped = store.drop_settled(medium, item_key)
    log.info("deleted medium=%s key=%s backend_id=%s stopped=%s ledger_rows=%d",
             medium, item_key, row.get("id"), stopped, dropped)
    return _report(medium=medium, cleared=row.get("title") or item.get("name"),
                   item_key=item_key, stopped=stopped, ledger_rows=dropped)


def _require_absent(medium: str, provider_id: str) -> None:
    """Refuse unless the library really has stopped holding this id.

    Read fresh, and read as the whole index rather than as a filter, because
    `/Items?anyProviderIdEquals=` is silently ignored on this Jellyfin --
    verified 2026-09-16, it returns every row in the library, so a guard built
    on it would have passed everything. The index costs 0.87 s for 428 films
    and 130 series, and it is the same index that decides a request has
    *arrived*: a thing entered it when it turned up and has left it now.
    """
    try:
        owned = media.owned(force=True)
    except jellyfin.JellyfinUnavailable as exc:
        raise Unsettled(str(exc)) from exc
    present = (owned.movie_tmdb if medium == media.MOVIE else owned.series_tvdb)
    if provider_id in present:
        raise StillHere("That is still in the library, so nothing was changed.")


def _backend_row(medium: str, provider_id: str) -> dict | None:
    tool = radarr.backend() if medium == media.MOVIE else sonarr.backend()
    if not tool.configured:
        return None
    return tool.existing(provider_id)


def _has_landed(medium: str, row: dict) -> bool:
    """Whether the acquisition tool believes it already has the files.

    True is what makes a row a leftover rather than work in flight. Moments
    after a Jellyfin delete the tool has not rescanned and still says it has
    the file, which is exactly the stale state being cleared; a row that has
    never had one belongs to something still being looked for.
    """
    if medium == media.MOVIE:
        return bool(row.get("hasFile"))
    statistics = row.get("statistics") or {}
    return int(statistics.get("episodeFileCount") or 0) > 0


def _stop(medium: str, backend_id: str) -> bool:
    """Remove the acquisition tool's row. Files are never touched.

    `arr.Arr.delete` sends `deleteFiles=false`, and it stays false here even
    though Jellyfin has just removed the file: if the tool's view of the path
    is not Jellyfin's, false is the value that cannot destroy anything.
    """
    if not backend_id:
        return False
    return (radarr.cancel(backend_id) if medium == media.MOVIE
            else sonarr.cancel(backend_id))


# --- Books -------------------------------------------------------------------


def _clear_book(item: dict) -> dict:
    """The book path, which has no provider id to lean on.

    76% of this audiobook library carries no Audible ASIN, and the ASIN a book
    is asked for is not the one it arrives under anyway -- two marketplaces
    issue different ids for the same edition. So a deleted book is matched to
    its ledger row the way an arriving one is: on title, with an author to
    agree. `book_wants._arrived` is that matcher, and it is called rather than
    reimplemented, with the deleted book standing in for the library index.
    """
    name = (item.get("name") or "").strip()
    asin = str((item.get("providerIds") or {}).get("Audible") or "").strip()
    authors = [a for a in (item.get("authors") or []) if a]
    if not name and not asin:
        return _report(medium=media.BOOK, cleared=None)

    _require_book_absent(name, authors, asin)

    index = _one_book_index(name, authors, asin)
    rows = [row for row in store.settled_rows(media.BOOK)
            if book_wants._arrived(_as_book_row(row), *index)]
    if not rows:
        log.info("deleted book name=%r: no settled request matches", name)
        return _report(medium=media.BOOK, cleared=None)

    keys = {row["item_key"] for row in rows}
    waiting = {key for key in keys if store.waiting(media.BOOK, key)}
    if waiting:
        raise StillHere(
            "Somebody is still waiting on that, so it has been left alone.")

    stopped = all(book_wants._stop_acquiring(key) for key in sorted(keys))
    dropped = sum(store.drop_settled(media.BOOK, key) for key in sorted(keys))
    log.info("deleted book name=%r asins=%s stopped=%s ledger_rows=%d",
             name, sorted(keys), stopped, dropped)
    return _report(medium=media.BOOK, cleared=name or asin,
                   item_key=sorted(keys)[0], stopped=stopped,
                   ledger_rows=dropped)


def _require_book_absent(name: str, authors: list[str], asin: str) -> None:
    """Refuse while the library still holds a book this deletion would match.

    A targeted search rather than the whole listing: `books()` is 3,700 rows
    and 14 s, and the question is about one title. Matching on what comes back
    is the same title-and-author test the ledger side uses, so a search hit for
    a different book by the same name and a different author does not save a
    row that should go.
    """
    try:
        candidates = jellyfin.books_named(name) if name else []
    except jellyfin.JellyfinUnavailable as exc:
        raise Unsettled(str(exc)) from exc
    index = _one_book_index(name, authors, asin)
    for found in candidates:
        row = {"asin": book_engine._asin(found) or "",
               "title": found.get("Name") or "",
               "authors": book_engine._authors(found)}
        if book_wants._arrived(row, *index):
            raise StillHere(
                "That is still in the library, so nothing was changed.")


def _one_book_index(name: str, authors: list[str],
                    asin: str) -> tuple[set, dict]:
    """The deleted book, in the shape a library index has.

    `_arrived` asks "is this row's book in that index", and the question here
    is the mirror of it -- which rows mean this one book. Building an index of
    exactly one book turns the second question into the first, so there is one
    title matcher in the service and not two that can drift apart.
    """
    authors_norm = {book_engine._norm_author(a) for a in authors if a}
    by_title = {key: set(authors_norm)
                for key in book_engine._title_keys(name or "")}
    return ({asin} if asin else set()), by_title


def _as_book_row(row) -> dict:
    """A ledger row as `_arrived` wants it: an ASIN, a title, and authors.

    The authors column is JSON, and `_authors_of` is the one place that knows
    it -- including that a row written before the column existed has none.
    """
    return {"asin": row["item_key"],
            "title": row["title"] or "",
            "authors": book_store._authors_of(row["authors"])}


# --- Shared ------------------------------------------------------------------


def _require_nobody_waiting(medium: str, item_key: str) -> None:
    if store.waiting(medium, item_key):
        raise StillHere(
            "Somebody is still waiting on that, so it has been left alone.")


def _report(*, medium: str | None, cleared: str | None,
            item_key: str = "", stopped: bool = False,
            ledger_rows: int = 0) -> dict:
    """What the caller is told, and the sentence a client may read out.

    `cleared` names what was stopped, or is absent. The distinction matters to
    a client: "nothing was being acquired" is the ordinary answer and worth
    saying nothing about, where a failure to stop something that was is worth
    a sentence.
    """
    if cleared is None:
        message = "Nothing was still looking for that."
    elif stopped:
        message = f"Nothing is looking for {cleared} any more."
    else:
        message = (f"{cleared} was deleted, but the tool acquiring it could "
                   f"not be reached, so it may come back.")
    return {"medium": medium, "itemKey": item_key, "cleared": cleared is not None,
            "stopped": stopped, "ledgerRows": ledger_rows, "message": message}


def supported() -> bool:
    """Whether this deployment can clear anything at all.

    False on an install with no acquisition tool configured, where the whole
    feature is a no-op -- and a client that is told false says nothing about
    it, the same rule every other optional capability follows.
    """
    return any(tool() for tool in
               (radarr.configured, sonarr.configured, listenarr.configured))
