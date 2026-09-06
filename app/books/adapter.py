"""Books, in the request API's vocabulary.

The other three media are thin: a search, an add, and an arrival test against
a provider id. Books are not, and the difference is not incidental.

* A book does not arrive under the ASIN it was asked for. The marketplace it
  was found in and the tagger's marketplace issue different ASINs for the same
  edition, so arrival is decided on the title with an author to agree with it.
* Asking for a series is a bounded batch of individual requests, decided
  against the library rather than against Listenarr, and it answers with a
  sentence because the screen that asked has no row to put it on.

Both of those were got right once, in code that is now `app.books.wants` and
`app.books.series`. This module hands the whole medium to them rather than
reproducing either as another arm of a `if medium ==` chain in the shared
request path, where they would be a second chance to get them wrong.
"""
from .. import jellyfin, logs
from . import search as book_search
from . import series as book_series
from . import shelves, wants

log = logs.get("books.adapter")

#: What "ask for the rest of a series" is called on the wire, alongside the
#: ordinary `book`. A unit and not a separate endpoint, because to somebody
#: using the page it is the same control with a different scope -- the way
#: music has artist, album and track.
SERIES_UNIT = "series"


class Denied(Exception):
    """Refused before anything was asked for. Carries what to say."""


def search_hits(user: jellyfin.User | None, query: str,
                unit: str = "book") -> list[dict]:
    """Catalogue hits, in the shape the shared search results template reads.

    An owned book is marked rather than dropped. On a recommendation shelf an
    owned book is noise; to somebody typing its title it is the answer.
    """
    if user is None:
        # Every other medium can answer without knowing who is asking. This one
        # cannot: what is already owned is read with the caller's own play
        # state attached, and there is nothing sensible to substitute.
        log.warning("book search asked for with no caller; returning nothing")
        return []
    if unit == SERIES_UNIT:
        return _series_hits(user, query)
    return [_as_hit(row) for row in book_search.search(user, query)]


def _series_hits(user: jellyfin.User, query: str) -> list[dict]:
    """Series the library could be asked to fill in, as searchable rows.

    A plan rather than a catalogue lookup: the question is not "does this
    series exist" but "what of it is missing here", and `series.plan` answers
    that against the library.
    """
    try:
        planned = book_series.plan(user, query.strip())
    except book_series.NotASeries:
        return []
    except (book_series.Unresolvable, book_series.Unavailable) as exc:
        log.info("series search unresolved query=%r (%s)", query, exc)
        return []
    name = planned.get("series") or query.strip()
    # `plan` answers with the rows themselves, not counts, on all three of
    # these. Reading one of them as a number was how this path came to raise
    # TypeError for every series that actually had a gap -- which is to say,
    # in the only case it exists for.
    missing = len(planned.get("missing") or ())
    have = len(planned.get("have") or ())
    on_order = len(planned.get("onOrder") or ())
    return [{
        "medium": "book",
        "unit": SERIES_UNIT,
        "itemKey": name,
        "title": name,
        "year": "",
        "authors": [],
        # Owned when there is nothing left to ask for. A series row is not a
        # book: what "owned" means for it is that the library already holds
        # everything Audible lists.
        "owned": missing == 0,
        "overview": _series_detail(have, on_order, missing),
    }]


def _series_detail(have: int, on_order: int, missing: int) -> str:
    """What the row says about a series, before anybody asks for it.

    Deliberately not `series.sentence`, which describes what one tap just did
    and reads as a past tense. This describes the state a person is choosing
    from.
    """
    if not missing:
        return f"You already have all {have} that Audible lists."
    parts = [f"{have} of {have + on_order + missing} in your library"]
    if on_order:
        parts.append(f"{on_order} already on order")
    parts.append(f"{missing} to ask for")
    return ", ".join(parts) + "."


def _as_hit(row: dict) -> dict:
    """One audiobook, as the shared results list expects a hit to look.

    `itemKey` and `overview` are the shared names, spelled the way `radarr`,
    `sonarr` and `buskarr` spell them. They were `item_key` and `detail` here,
    which is not a style difference: the results template reads `itemKey` into
    the hidden field its Ask button posts, so every book hit posted an empty
    identifier and came back refused.
    """
    return {
        "medium": "book",
        "unit": "book",
        "itemKey": row.get("asin") or "",
        "title": row.get("title") or "",
        "year": "",
        "authors": row.get("authors") or [],
        "artist": ", ".join(row.get("authors") or []),
        # An audiobook is chosen by who reads it as much as by who wrote it,
        # and this library has three narration series of one book series.
        "narrators": row.get("narrators") or [],
        "owned": bool(row.get("owned")),
        # Already asked for by this account. The other three media have no such
        # state on a search hit; a book does, because `search` reads the ledger
        # to build it, and dropping it made a book already on order look
        # askable.
        "requested": bool(row.get("requested")),
        "overview": row.get("description") or "",
    }


def want(user: jellyfin.User, item_key: str, unit: str,
         hit: dict) -> tuple[str, str]:
    """Ask for one book, or for the rest of one series."""
    if unit == SERIES_UNIT:
        return _want_series(user, item_key)
    try:
        state, message = wants.want(user, item_key, hit.get("title", ""))
    except wants.Denied as denied:
        raise Denied(str(denied)) from denied
    shelves.forget_asin(item_key)
    return state, message


def _want_series(user: jellyfin.User, name: str) -> tuple[str, str]:
    try:
        outcome = book_series.want_series(user, name.strip())
    except book_series.NotASeries as exc:
        raise Denied(str(exc)) from exc
    except book_series.Unresolvable as exc:
        raise Denied(str(exc)) from exc
    except book_series.Unavailable as exc:
        raise Denied(str(exc)) from exc
    except wants.Denied as denied:
        raise Denied(str(denied)) from denied
    # The sentence is the answer here, not a row: a series request becomes
    # several ledger rows and none of them is the thing that was asked for.
    return wants.ON_ITS_WAY, outcome.get("message") or "Asked for."


def cancel(user: jellyfin.User, item_key: str) -> tuple[bool, str]:
    """Take one book off this account's list, and stop looking for it."""
    return wants.cancel(user, item_key)


def states(user: jellyfin.User) -> list[dict]:
    """This account's book requests, in the shared request shape.

    Arrival for a book is not a provider-id match and cannot be made into one.
    The ASIN a book was asked for is the one issued by whichever marketplace
    it was found in, and the tagger writes the one the *other* store issued for
    the same edition -- so the two never meet, and arrival is decided on the
    title with an author to agree with it. That is what `books.wants.states`
    does, against the library index the engine builds anyway.
    """
    try:
        owned = shelves.owned_index(user)
    except jellyfin.JellyfinUnavailable as exc:
        # Reported unsettled rather than raised. Every other medium's state is
        # perfectly knowable, and letting this one take the request list away
        # loses somebody's outstanding films and series because a listing of
        # three and a half thousand audiobooks timed out.
        log.warning("book arrivals not settled: %s", exc)
        owned = None
    return [{
        "itemKey": row["asin"],
        "medium": "book",
        "unit": "book",
        "title": row["title"],
        "year": "",
        "state": row["state"],
        "requestedAt": row["requested_at"],
    } for row in wants.states(user.key, owned)]
