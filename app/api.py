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

import httpx
from threading import Lock

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from . import (config, describarr, jellyfin, logs, media, recommendations,
               store, wants)

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
    return {"service": config.SERVICE_NAME,
            "protocol": config.API_VERSION,
            # Additive. A shipped client reads `protocol`, ignores unknown
            # keys, and goes on speaking 1; a newer one sees that 2 is
            # available and asks for it. See `capabilities`.
            "protocols": list(SUPPORTED_PROTOCOLS)}


#: Every shape this server can answer in. 1 is what shipped: films, series and
#: music. 2 adds books, which is the whole of the difference.
SUPPORTED_PROTOCOLS = (1, 2)

#: Media a protocol-1 client is told about. Books are withheld deliberately,
#: and not because a fourth medium would fail to decode -- it decodes fine. A
#: books library named for requests here, while the audiobook protocol also
#: names it for recommendations, makes a shipped client draw two rows for the
#: one feature from the one server. The merge exists to stop that, not to ship
#: it to builds already in the field.
PROTOCOL_1_MEDIA = ("movie", "series", "music")


@router.get("/capabilities")
def capabilities(protocol: int = 1,
                 user: jellyfin.User = Depends(caller)) -> dict:
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
    if protocol not in SUPPORTED_PROTOCOLS:
        # Refused rather than served the nearest shape. A client asking for a
        # protocol this server does not have is either newer than the server
        # or has a typo, and quietly handing it the legacy answer would make
        # both of those look like a server that simply has fewer media.
        raise HTTPException(
            status_code=400,
            detail=f"This server speaks protocol "
                   f"{', '.join(str(v) for v in SUPPORTED_PROTOCOLS)}, "
                   f"not {protocol}.")
    offered = media.available()
    if protocol == 1:
        offered = {key: value for key, value in offered.items()
                   if key in PROTOCOL_1_MEDIA}
    blocks = []
    for found in offered.values():
        blocks.append({
            "medium": found.key,
            "label": found.label,
            "libraryIds": list(found.library_ids),
            "units": list(found.units),
            "unitCosts": {unit: media.cost(found.key, unit)
                          for unit in found.units},
            "dailyCap": wants.daily_cap(user, found.key),
            "remainingToday": wants.allowance(user, found.key),
            # Whether the tool that acquires this medium answered when it was
            # last asked. Published because a client had no way to tell "no
            # matches" from "Radarr did not answer", and the medium is
            # deliberately still offered while a backend is down -- so without
            # this the outage is invisible on every surface.
            "backendReachable": found.backend_reachable,
            "backendDetail": found.backend_detail,
        })
    log.info("capabilities user=%s keyholder=%s media=%s",
             user.key, user.is_admin, [b["medium"] for b in blocks])
    recommendation_media = []
    for medium_key in recommendations.SUPPORTED_MEDIA:
        try:
            recommendation_libraries = list(
                recommendations.library_ids(medium_key))
        except jellyfin.JellyfinUnavailable:
            # Capabilities are discovery, not the recommendation request
            # itself. One temporary library failure must not make request
            # support or the other recommendation medium vanish.
            continue
        if recommendation_libraries:
            recommendation_media.append({
                "medium": medium_key,
                "libraryIds": recommendation_libraries,
                "surfaces": ["owned"],
                "limit": recommendations.limit(medium_key),
            })
    return {
        "version": config.API_VERSION,
        "service": "nextup",
        "user": {"id": user.id, "name": user.name, "keyholder": user.is_admin},
        "media": blocks,
        "states": [wants.ON_ITS_WAY, wants.STILL_LOOKING, wants.IN_LIBRARY],
        "search": {"supported": True, "limit": config.SEARCH_LIMIT},
        "cancel": {"supported": True},
        # Absent on a server with no describarr configured, and a client shows
        # nothing at all for it then -- the same rule `media` follows, and the
        # same reason it is reported from configuration rather than from a
        # probe. Additive, so a client that predates it goes on working.
        "describe": {"supported": describarr.configured()},
        "recommendations": {
            "media": recommendation_media,
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
    try:
        results = wants.search(query, medium, unit, user) if query else []
    except jellyfin.JellyfinUnavailable as exc:
        # 503 in JSON, like the recommendation route. The application's own
        # handler renders an HTML page, which is right for the browser pages
        # and useless to a client that asked for JSON.
        raise HTTPException(
            status_code=503,
            detail="Jellyfin could not be reached, so what the library "
                   "already holds is not known.") from exc
    log.info("search user=%s medium=%s unit=%s q=%r hits=%d",
             user.key, medium, unit or "-", query, len(results))
    return {"version": config.API_VERSION, "medium": medium,
            "unit": unit, "query": query, "results": results}


@router.get("/requests")
def get_requests(medium: str | None = None,
                 user: jellyfin.User = Depends(caller)) -> dict:
    """This account's requests and what has become of each."""
    try:
        rows = wants.states(user, medium)
    except jellyfin.JellyfinUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Jellyfin could not be reached, so whether these have "
                   "arrived is not known.") from exc
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
    refresh: bool = Query(default=False),
    user: jellyfin.User = Depends(caller),
) -> dict:
    """Owned recommendations for one medium and one authenticated account."""
    if medium not in recommendations.SUPPORTED_MEDIA:
        raise HTTPException(
            status_code=404,
            detail=f"This server does not recommend {medium} yet.")
    try:
        shelf = recommendations.result(
            user, library_id, force=refresh, medium=medium)
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


@router.post("/describe")
def post_describe(user: jellyfin.User = Depends(caller),
                  item_id: str = Body(..., embed=True, alias="itemId")) -> dict:
    """Queue one film, series or season to have audio description added.

    Not an allowance question and not written to the ledger. Nothing is
    acquired and nobody's line is used: the file is already here, and this asks
    for a track to be built against it from a source describarr already
    searches. Repeating it is harmless -- describarr's own ledger skips what it
    has finished -- so there is nothing to spend and nothing to refuse.

    The path comes from this service because `Path` is administrator-only in
    Jellyfin, and a listener's client cannot see it.
    """
    if not describarr.configured():
        raise HTTPException(
            status_code=503,
            detail="This server has no describarr configured.")
    item = jellyfin.item_with_path(item_id, user.id)
    if item is None:
        raise HTTPException(status_code=404, detail="No such item.")
    kind = item.get("Type", "")
    if kind not in describarr.DESCRIBABLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"A {kind or 'thing'} cannot be described. "
                   "Ask for a film, a series or a season.")
    if not (item.get("Path") or "").strip():
        # A title Jellyfin knows about but cannot place on disk -- a stale
        # entry, or a library this container does not mount. Said plainly
        # rather than passed on, because describarr would answer 400 about a
        # path the listener never supplied.
        raise HTTPException(
            status_code=404,
            detail=f"{item.get('Name', 'That')} has no file on the server.")
    log.info("api describe user=%s type=%s id=%s", user.key, kind, item_id)
    try:
        detail = describarr.request(item)
    except describarr.DescribeRefused as refused:
        # 409 rather than a 5xx. describarr answering "no source has a
        # description for this" is the ordinary outcome for a film nothing has
        # described, not an outage, and its own sentence is better than one
        # invented here. A client that reads 5xx as "the server is down" would
        # both hide that sentence and blame the wrong thing.
        raise HTTPException(
            status_code=409,
            detail=refused.detail or "describarr would not take the request.")
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"describarr could not be reached: {exc}")
    return {"queued": True, "detail": detail}


@router.post("/allowance/reset")
def post_allowance_reset(
        caller_user: jellyfin.User = Depends(caller),
        account: str = Body(..., embed=True),
        medium: str = Body("", embed=True)) -> dict:
    """Give one account its day's requests back. Administrators only.

    `medium` empty means every medium this server serves, which is what
    somebody who has been told "I have run out" almost always means; naming
    one is for giving back the books without also giving back the films.

    Not a reset of anything stored against the requests themselves. The ledger
    keeps saying when each thing was asked for, and a marker records that the
    day was forgiven -- so the list a person sees, and the order it is in, are
    the same either side of this call.
    """
    if not caller_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Giving an account its requests back needs a Jellyfin "
                   "administrator account.")
    try:
        target = jellyfin.account(account)
    except LookupError as unknown:
        raise HTTPException(status_code=404, detail=str(unknown)) from unknown

    wanted = [medium] if medium else list(media.available())
    reset = []
    for each in wanted:
        try:
            remaining = wants.reset_allowance(target, each)
        except LookupError as unserved:
            raise HTTPException(status_code=404,
                                detail=str(unserved)) from unserved
        reset.append({"medium": each, "remainingToday": remaining})

    log.info("allowance reset by=%s for=%s media=%s",
             caller_user.key, target.key, [r["medium"] for r in reset])
    return {"account": {"id": target.id, "name": target.name,
                        "keyholder": target.is_admin},
            "reset": reset}


@router.post("/allowance/cap")
def post_allowance_cap(
        caller_user: jellyfin.User = Depends(caller),
        account: str = Body(..., embed=True),
        medium: str = Body("", embed=True),
        daily_cap: int | None = Body(None, embed=True, alias="dailyCap")
) -> dict:
    """Give one account its own daily allowance. Administrators only.

    `dailyCap` null puts the account back on the configured cap, which is not
    the same as setting it to that number: the default moves when the setting
    does, and a copy of today's value does not.

    `medium` empty means every medium this server serves. An administrator's
    own allowance is stored all the same and simply not consulted while they
    remain one -- refusing to store it would mean a demoted account silently
    inheriting the default instead of what somebody chose for it.
    """
    if not caller_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Setting an account's allowance needs a Jellyfin "
                   "administrator account.")
    try:
        target = jellyfin.account(account)
    except LookupError as unknown:
        raise HTTPException(status_code=404, detail=str(unknown)) from unknown

    served = list(media.available())
    wanted = [medium] if medium else served
    for each in wanted:
        if each not in served:
            raise HTTPException(status_code=404,
                                detail=f"this server does not serve {each!r}")
    try:
        for each in wanted:
            if daily_cap is None:
                store.clear_cap(target.key, each)
            else:
                store.set_cap(target.key, each, daily_cap)
    except ValueError as refused:
        raise HTTPException(status_code=422, detail=str(refused)) from refused

    log.info("allowance cap by=%s for=%s media=%s cap=%s",
             caller_user.key, target.key, wanted,
             "default" if daily_cap is None else daily_cap)
    return {"account": {"id": target.id, "name": target.name,
                        "keyholder": target.is_admin},
            "allowances": [{"medium": each,
                            "dailyCap": wants.daily_cap(target, each),
                            "ownCap": store.cap_override(target.key, each),
                            "remainingToday": wants.allowance(target, each)}
                           for each in wanted]}


@router.get("/allowance")
def get_allowance(caller_user: jellyfin.User = Depends(caller),
                  account: str = "") -> dict:
    """What one account is allowed and has left, per medium.

    Its own account for anybody; another's for an administrator. Asking about
    somebody else is how the page that sets these draws what is there now,
    and reading one's own is what makes this useful to a client that wants the
    figures without the rest of `capabilities`.
    """
    target = caller_user
    if account:
        if not caller_user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Reading another account's allowance needs a Jellyfin "
                       "administrator account.")
        try:
            target = jellyfin.account(account)
        except LookupError as unknown:
            raise HTTPException(status_code=404,
                                detail=str(unknown)) from unknown
    return {"account": {"id": target.id, "name": target.name,
                        "keyholder": target.is_admin},
            "allowances": [{"medium": key,
                            "dailyCap": wants.daily_cap(target, key),
                            "ownCap": store.cap_override(target.key, key),
                            "remainingToday": wants.allowance(target, key)}
                           for key in sorted(media.available())]}


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
