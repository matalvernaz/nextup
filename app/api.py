"""The JSON API, for clients that cannot sign in through a browser.

Everything here is reachable without the sign-in proxy in front of it, because
a native app has no way to complete an oauth2 flow. That makes this module the
only place in the service that authenticates a caller itself, and the only
place where getting authentication wrong exposes somebody else's list.

Two rules follow, and both are load-bearing:

* Identity comes from introspecting the caller's own Jellyfin access token,
  and a failure to introspect is a rejection. The browser resolver's fallback
  to `JELLYFIN_USER` must never be reachable from here.
* A GET does not acquire anything.
"""
import hashlib
import time
from threading import Lock

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from . import config, jellyfin, logs, media, recommendations, wants

log = logs.get("api")

router = APIRouter(prefix="/api/v1")

# Introspection results, keyed by a digest of the token rather than the token.
# Short-lived on purpose: expiry is the only thing that makes a token revoked
# in Jellyfin stop working here.
_tokens: dict[str, tuple[float, jellyfin.User]] = {}
_tokens_guard = Lock()


def _cached_user(digest: str) -> jellyfin.User | None:
    with _tokens_guard:
        entry = _tokens.get(digest)
    if entry and time.monotonic() - entry[0] <= config.TOKEN_CACHE_SECONDS:
        return entry[1]
    return None


def caller(authorization: str | None = Header(default=None),
           x_emby_token: str | None = Header(default=None)) -> jellyfin.User:
    """The authenticated account behind this request.

    Accepts the token inside the usual Jellyfin handshake header --
    `MediaBrowser Token="...", Client="...", Device="..."` -- or as the
    `X-Emby-Token` some clients send instead. Never from the query string: a
    token in a URL ends up in access logs.
    """
    token = jellyfin.token_from_header(authorization) or (x_emby_token or "").strip()
    if not token:
        log.warning("api call with no access token")
        raise HTTPException(status_code=401, detail="No Jellyfin access token.")

    digest = hashlib.sha256(token.encode()).hexdigest()
    if (found := _cached_user(digest)) is not None:
        return found
    try:
        found = jellyfin.user_from_token(token)
    except jellyfin.TokenRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except jellyfin.JellyfinUnavailable as exc:
        # Serving anything at all here would mean guessing at authorisation.
        raise HTTPException(
            status_code=503, detail="Jellyfin is unreachable.") from exc
    with _tokens_guard:
        # Expired entries go here rather than accumulating: the only thing that
        # ever reads one again is this same lookup, so nothing else would clear
        # a rotated token's row for the life of the process.
        cutoff = time.monotonic() - config.TOKEN_CACHE_SECONDS
        for stale in [k for k, (at, _) in _tokens.items() if at <= cutoff]:
            del _tokens[stale]
        _tokens[digest] = (time.monotonic(), found)
    return found


@router.get("/info")
def info() -> dict:
    """That this service is here, answered without a token.

    It exists so absence and malfunction stop being the same answer. A client
    looks for this service at the Jellyfin origin, which most servers do not
    serve it from, so a 404 there has to mean "not installed" -- and it also
    means a missing proxy rule, a stopped container, a rejected token and a
    version the client cannot read, none of which the client can tell apart
    while every route asks for credentials first.

    Deliberately says nothing about anybody, and deliberately does not list
    media: `/capabilities` settles those per account, and a second list here
    would be one more to keep in step for no gain.
    """
    return {"service": config.SERVICE_NAME, "protocol": config.API_VERSION}


@router.get("/capabilities")
def capabilities(user: jellyfin.User = Depends(caller)) -> dict:
    """What this server serves, and what this account may ask for.

    Reports configured support rather than live reachability. A probe would
    flap, would put a downstream timeout in front of a client's first screen,
    and still could not promise the next request will succeed -- so the POST
    stays authoritative about its own outcome.

    `media` is the whole extension point. A client shows a control for a
    medium listed here and shows nothing at all for one that is absent, which
    is how a deployment with only Radarr in it says "films, and nothing else"
    without the client knowing anything about acquisition tools.
    """
    blocks = []
    for found in media.available().values():
        blocks.append({
            "medium": found.key,
            "label": found.label,
            "libraryIds": list(found.library_ids),
            "units": list(found.units),
            "unitCosts": {unit: media.cost(found.key, unit)
                          for unit in found.units},
            "dailyCap": None if user.is_admin else found.daily_cap,
            "remainingToday": wants.allowance(user, found.key),
        })
    log.info("capabilities user=%s keyholder=%s media=%s",
             user.key, user.is_admin, [b["medium"] for b in blocks])
    try:
        recommendation_libraries = list(recommendations.library_ids())
    except jellyfin.JellyfinUnavailable:
        # Capabilities are discovery, not the recommendation request itself.
        # A temporary Jellyfin failure must not make every other medium vanish.
        recommendation_libraries = []
    return {
        "version": config.API_VERSION,
        "service": "nextup",
        "user": {"id": user.id, "name": user.name, "keyholder": user.is_admin},
        "media": blocks,
        "states": [wants.ON_ITS_WAY, wants.STILL_LOOKING, wants.IN_LIBRARY],
        "search": {"supported": True, "limit": config.SEARCH_LIMIT},
        "cancel": {"supported": True},
        "recommendations": {
            "media": ([{
                "medium": media.SERIES,
                "libraryIds": recommendation_libraries,
                "surfaces": ["owned"],
                "limit": config.SERIES_RECOMMENDATION_LIMIT,
            }] if recommendation_libraries else []),
        },
    }


@router.get("/search")
def get_search(medium: str, q: str = "", unit: str = "",
               user: jellyfin.User = Depends(caller)) -> dict:
    """Catalogue hits for something the caller already has in mind.

    Things the library already holds are marked, not dropped: to somebody
    typing a title, the copy they own is the answer, and hiding it reads as
    the search being broken.
    """
    if media.get(medium) is None:
        raise HTTPException(
            status_code=404, detail=f"This server does not serve {medium}.")
    query = q.strip()
    results = wants.search(query, medium, unit) if query else []
    log.info("search user=%s medium=%s unit=%s q=%r hits=%d",
             user.key, medium, unit or "-", query, len(results))
    return {"version": config.API_VERSION, "medium": medium,
            "unit": unit, "query": query, "results": results}


@router.get("/requests")
def get_requests(medium: str | None = None,
                 user: jellyfin.User = Depends(caller)) -> dict:
    """This account's requests and what has become of each."""
    rows = wants.states(user, medium)
    return {
        "version": config.API_VERSION,
        "requests": rows,
        "remainingToday": {found.key: wants.allowance(user, found.key)
                           for found in media.available().values()},
    }


@router.get("/recommendations")
def get_recommendations(
    medium: str,
    library_id: str = Query(default="", alias="libraryId"),
    user: jellyfin.User = Depends(caller),
) -> dict:
    """Owned recommendations for one medium and one authenticated account."""
    if medium != media.SERIES:
        raise HTTPException(
            status_code=404,
            detail=f"This server does not recommend {medium} yet.")
    try:
        shelf = recommendations.result(user, library_id)
    except recommendations.UnknownLibrary as exc:
        raise HTTPException(
            status_code=404,
            detail="This server does not recommend that library.") from exc
    except jellyfin.JellyfinUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Jellyfin is unreachable.") from exc
    return {
        "version": config.API_VERSION,
        "medium": medium,
        "libraryId": library_id,
        "rankerVersion": shelf["ranker_version"],
        "seedCount": shelf["seed_count"],
        "recommendations": shelf["recommendations"],
    }


@router.post("/want")
def post_want(user: jellyfin.User = Depends(caller),
              medium: str = Body(..., embed=True),
              item_key: str = Body(..., embed=True, alias="itemKey"),
              unit: str = Body("", embed=True),
              title: str = Body("", embed=True),
              year: str = Body("", embed=True),
              artist: str = Body("", embed=True),
              source: str = Body("", embed=True),
              ref: str = Body("", embed=True)) -> dict:
    """Ask for one thing. Repeating it is free and spends no allowance.

    The extra fields are what the search hit said. Films and series need none
    of them -- their ledger key carries the provider id and the acquisition
    tool looks the rest up itself -- but music has no such id, so the credit
    and the catalogue reference have to travel with the request.
    """
    log.info("api want user=%s medium=%s key=%s", user.key, medium, item_key)
    hit = {"title": title, "year": year, "artist": artist,
           "source": source, "ref": ref}
    try:
        state, message = wants.want(user, medium, item_key, unit, hit)
    except wants.Denied as denied:
        raise HTTPException(status_code=409, detail=str(denied)) from denied
    return {"medium": medium, "itemKey": item_key, "state": state,
            "message": message,
            "remainingToday": wants.allowance(user, medium)}


@router.post("/cancel")
def post_cancel(user: jellyfin.User = Depends(caller),
                medium: str = Body(..., embed=True),
                item_key: str = Body(..., embed=True, alias="itemKey")) -> dict:
    """Take one thing off this account's list, and stop looking for it.

    Not a DELETE: the identifier belongs to a catalogue rather than to this
    service, and it does more than erase a row -- it calls an acquisition off,
    which `cancel` says and a method alone does not.
    """
    removed, message = wants.cancel(user, medium, item_key)
    if not removed:
        raise HTTPException(status_code=404, detail=message)
    return {"medium": medium, "itemKey": item_key, "removed": True,
            "message": message,
            "remainingToday": wants.allowance(user, medium)}
