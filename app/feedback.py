"""Taking one mark from a client, and telling every shelf it changes.

The marks themselves, their weights and their rules are in `taste`; this is
the one place a request reaches them from, shared by the request API and the
audiobook route the book screen still speaks. Kept apart from `taste` because
it has to know every medium's shelves, and those import `taste` themselves.
"""
from . import logs, podcasts, recommendations, taste
from .books import shelves as book_shelves
from .books import store as book_store
from .jellyfin import User

log = logs.get("feedback")


def take(user: User, medium: str, item_key: str, surface: str,
         opinion: str, had: bool, title: str = "",
         recommendation_id: str | None = None,
         if_revision: int | None = None) -> dict:
    """Store one mark, start the rebuilds it calls for, and answer both states.

    Raises `taste.Invalid` for a mark that could never match anything, and
    `taste.Changed` for an Undo (`if_revision`) of a mark changed since.

    The rebuilds run behind this answer. The shelves already cached are
    filtered against the marks on every read, so what was just hidden is gone
    from the next one; how the rest re-rank arrives once the rebuild has.
    """
    taste.check(medium, surface, item_key, opinion)
    key = item_key.strip()
    details: dict = {}
    # What the catalogue said about something the library does not hold,
    # taken from the shelf it was offered on, so the mark can count for its
    # genres, people and blurb. Nothing to keep for a mark being taken back.
    if surface == taste.CATALOGUE and (opinion or had):
        if medium == "podcast":
            details = podcasts.catalogue_details(user, key)
        elif medium == "book":
            details = book_shelves.suggestion_details(user, key)
    # A library book's title, authors and ASIN, so the mark also finds the
    # same book in another edition: the other marketplace's, offered as a
    # suggestion. Without authors it matches by id and ASIN only.
    elif medium == "book" and (opinion or had):
        details = book_shelves.owned_details(user, key)
    previous, current = taste.record(
        user.key, medium, key, surface, opinion, had,
        title or str(details.get("title") or ""), details,
        if_revision=if_revision)

    if medium == "book":
        # Beside the book screen's other outcomes, so a later look at what
        # became of a run's suggestions sees these too.
        action = "+".join(part for part in (opinion, "had" if had else "") if part)
        book_store.record_feedback(user.key, key, action or "clear", recommendation_id)
        # The playlist is published from the cached shelf, so the next read
        # writes it again, against the marks as they stand then.
        book_shelves.playlist_owed(user)
        book_shelves.rebuild_after_mark(user)
    else:
        recommendations.rebuild_after_mark(user, medium)
        if medium == "podcast":
            podcasts.rebuild_catalogue_after_mark(user)

    before = previous.state() if previous is not None else {
        "opinion": taste.NO_OPINION, "had": False}
    return {
        "medium": medium,
        "itemKey": key,
        "surface": surface,
        "opinion": current.opinion,
        "had": current.had,
        # What Undo sends back as `ifRevision`, so it only ever takes back
        # this change and never a later one.
        "revision": current.revision,
        "previous": before,
    }
