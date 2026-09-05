"""The audiobook service's own protocol, still answered.

Every EchoFin build in the field derives its audiobook address as
``<companion>/nextread`` or ``<jellyfin origin>/nextread`` and speaks the shape
that service published. Those builds do not update themselves, and a TestFlight
one expires rather than upgrading, so the shape has to keep working after the
merge or the books half of those apps simply stops.

Most of this is **not a translation**. ``/shelves`` and ``/summary`` have no
counterpart in the request API -- they are the audiobook service's own handlers,
moved here and mounted under their old prefix, calling the same engine from its
new home in ``app.books``. What did change is only what had to: identity comes
from ``api.caller`` rather than a second copy of the token cache, the library
ids come from the merged registry's book medium, and the daily cap is
``BOOK_DAILY_CAP``.

Retired when the last build that needs it is gone. Nothing new should be added
here: the shape it serves is frozen by what is already installed on somebody's
phone.
"""
from fastapi import APIRouter, Body, Depends, HTTPException

from . import config, jellyfin, logs
from .api import caller
from .books import search, series, shelves, wants

log = logs.get("compat.nextread")

#: The audiobook protocol version, frozen. It is what shipped, and the clients
#: that read it cannot be updated to read anything else.
LEGACY_PROTOCOL = 1

router = APIRouter(prefix="/nextread/api/v1")


@router.get("/info")
def info() -> dict:
    """That this service is here, answered without a token.

    It exists so absence and malfunction stop being the same answer. A client
    looks for this service at the Jellyfin origin, which most servers do not
    serve it from, so a 404 there has to mean "not installed" -- and it also
    means a missing proxy rule, a stopped container, a rejected token and a
    version this client cannot read, none of which the client can tell apart
    while every route needs credentials first.

    Deliberately says nothing about anybody, and deliberately does not list
    features: `/capabilities` negotiates those, per account, and a second list
    here would be one to keep in step for no gain.
    """
    return {"service": "nextread", "protocol": LEGACY_PROTOCOL}


@router.get("/capabilities")
def capabilities(user: jellyfin.User = Depends(caller)) -> dict:
    """What this server supports, and what this account may do.

    Deliberately reports configured support rather than live reachability. A
    reachability probe would flap, would make a client's first screen wait on a
    downstream timeout, and still could not promise the next request will
    succeed -- so the POST stays authoritative about its own outcome.
    """
    remaining = wants.allowance(user)
    log.info("capabilities user=%s keyholder=%s remaining=%s",
             user.key, user.is_admin, remaining)
    return {
        "version": LEGACY_PROTOCOL,
        "user": {"id": user.id, "name": user.name, "keyholder": user.is_admin},
        "libraryIds": jellyfin.library_ids("book"),
        "playlistName": config.PLAYLIST_NAME,
        "want": {
            "supported": True,
            "dailyCap": None if user.is_admin else config.BOOK_DAILY_CAP,
            "remainingToday": remaining,
        },
        "states": [wants.ON_ITS_WAY, wants.STILL_LOOKING, wants.IN_LIBRARY],
        # Named blocks rather than a version bump: a client that predates either
        # route asks for neither, and one that postdates a server without them
        # hides its own control instead of failing a tap.
        "search": {"supported": True, "limit": config.SEARCH_LIMIT},
        "summary": {"supported": True},
        "cancel": {"supported": True},
        "dismiss": {
            "supported": True,
            "undo": True,
            "days": config.DISMISS_TTL_DAYS,
        },
        "seriesWant": {"supported": True, "limit": config.SERIES_WANT_LIMIT},
    }


@router.get("/shelves")
def get_shelves(user: jellyfin.User = Depends(caller)) -> dict:
    """Both shelves, plus this account's outstanding requests and their state.

    ``owned`` carries Jellyfin item ids rather than rendered rows: the client
    hydrates them through its ordinary item request, so resume position,
    downloads and play-on-activation all keep working. Only the unowned half
    has to be described here, because it has no library item to describe.
    """
    data = shelves.result(user, update_playlist=False)
    log.info("shelves served user=%s owned=%d suggestions=%d",
             user.key, len(data["own"]), len(data["discover"]))
    return {
        "version": LEGACY_PROTOCOL,
        "runId": data.get("run_id"),
        "rankerVersion": data.get("ranker_version"),
        "owned": [{
            "id": row["id"],
            "title": row["title"],
            "reason": row["why"],
            "recommendationId": row.get("recommendation_id"),
            "source": row.get("source"),
        }
                  for row in data["own"]],
        "suggestions": [_suggestion(row) for row in data["discover"]],
        # The index rather than the shelf's own view of what is owned: a book
        # arrives under the ASIN the other marketplace issued for it, so
        # arrival is decided on title and author too.
        "requests": wants.states(user.key, shelves.owned_index(user)),
    }


@router.get("/search")
def get_search(q: str = "", user: jellyfin.User = Depends(caller)) -> dict:
    """Catalogue hits for a title the listener already has in mind.

    Owned books are marked, not dropped: on the shelf an owned book is noise,
    but to somebody typing its title it is the answer.
    """
    return {
        "version": LEGACY_PROTOCOL,
        "query": q.strip(),
        "results": search.search(user, q),
    }


@router.get("/summary")
def get_summary(asin: str, user: jellyfin.User = Depends(caller)) -> dict:
    """One book's blurb, for a book that is not in the library to describe it.

    Its own request rather than a field on every row: a blurb is one Audible
    call per book, and a shelf or a search would pay for twenty-five of them to
    show text nobody has opened.
    """
    found = search.summary(asin)
    if not found["summary"]:
        # Not a 404: the book exists, the blurb does not, and a client that
        # cannot tell those apart shows the wrong message.
        log.info("summary empty asin=%s", asin)
    return {"version": LEGACY_PROTOCOL, **found}


@router.post("/want")
def post_want(user: jellyfin.User = Depends(caller),
              asin: str = Body(..., embed=True),
              title: str = Body("", embed=True),
              recommendation_id: str | None = Body(
                  None, embed=True, alias="recommendationId")) -> dict:
    """Ask for one book. Repeating it is free and does not spend the allowance."""
    log.info("api want user=%s asin=%s", user.key, asin)
    try:
        state, message = wants.want(user, asin, title, recommendation_id)
    except wants.Denied as denied:
        raise HTTPException(status_code=409, detail=str(denied)) from denied
    shelves.forget_asin(asin)
    return {"asin": asin, "state": state, "message": message,
            "remainingToday": wants.allowance(user)}


@router.post("/cancel")
def post_cancel(user: jellyfin.User = Depends(caller),
                asin: str = Body(..., embed=True)) -> dict:
    """Take one book off this account's list, and stop looking for it.

    Not a DELETE: the ASIN is the marketplace's, not this app's, and putting it
    in a path would need it escaped by every client that has one. It also does
    more than erase a row -- it calls an acquisition off -- and `cancel` says so
    where a method alone would not.
    """
    removed, message = wants.cancel(user, asin)
    if not removed:
        raise HTTPException(status_code=404, detail=message)
    return {"asin": asin, "removed": True, "message": message,
            "remainingToday": wants.allowance(user)}


@router.post("/dismiss")
def post_dismiss(user: jellyfin.User = Depends(caller),
                 asin: str = Body(..., embed=True),
                 recommendation_id: str | None = Body(
                     None, embed=True, alias="recommendationId")) -> dict:
    """Hide this book for the configured cooling-off period."""
    wants.dismiss(user, asin, recommendation_id)
    shelves.invalidate(user.key)
    return {"asin": asin, "dismissed": True, "days": config.DISMISS_TTL_DAYS}


@router.post("/restore")
def post_restore(user: jellyfin.User = Depends(caller),
                 asin: str = Body(..., embed=True),
                 recommendation_id: str | None = Body(
                     None, embed=True, alias="recommendationId")) -> dict:
    """Undo a dismissal made by this account."""
    if not wants.restore(user, asin, recommendation_id):
        raise HTTPException(status_code=404, detail="That suggestion is not hidden.")
    shelves.invalidate(user.key)
    return {"asin": asin, "restored": True}


@router.post("/series/want")
def post_series_want(user: jellyfin.User = Depends(caller),
                     name: str = Body(..., embed=True, alias="series"),
                     anchor_item_id: str | None = Body(
                         None, embed=True, alias="anchorItemId")) -> dict:
    """Ask for the books of one series the library does not hold yet.

    Decided against the library, not against Listenarr: whatever is already on
    the shelf under any edition is not asked for again. Bounded per tap and,
    for a capped account, per day; the sentence in the answer says what was
    asked for and what was not, because the screen that made the request has
    no row to show it on. See `series.want_series`.
    """
    log.info("api series want user=%s series=%r anchor=%s",
             user.key, name, anchor_item_id)
    try:
        outcome = series.want_series(user, name.strip(), anchor_item_id)
    except series.NotASeries as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except series.Unresolvable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except series.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"version": LEGACY_PROTOCOL, **outcome,
            "remainingToday": wants.allowance(user)}


def _suggestion(row: dict) -> dict:
    """One unowned recommendation, as little of it as a client needs to show."""
    return {
        "asin": row.get("asin"),
        "title": row.get("title") or "",
        "authors": row.get("authors") or [],
        "narrators": row.get("narrators") or [],
        "series": row.get("series"),
        "seriesPosition": row.get("series_position"),
        "runtimeMinutes": row.get("runtime_min"),
        "description": row.get("description") or "",
        "reason": row.get("why") or [],
        "recommendationId": row.get("recommendation_id"),
        "source": row.get("source"),
        "state": "available",
    }
