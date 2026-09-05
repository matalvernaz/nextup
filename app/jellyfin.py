"""Jellyfin client. Jellyfin is the single library of record for Nextup.

Two things here are load-bearing:

* `user_from_token` is the whole of the JSON API's authentication and has no
  fallback of any kind.
* `owned_index` decides whether a request has arrived, and it decides it on
  provider ids rather than on titles. Measured on the library this was built
  against, 430 of 431 films carry a TMDB id and 125 of 126 series carry both a
  TMDB and a TVDB id -- the same ids Radarr and Sonarr are asked for. Matching
  on a title instead would reintroduce a whole class of near-miss that exact
  ids simply do not have.
"""
import time
from dataclasses import dataclass, field

import httpx

from . import config, logs

log = logs.get("jellyfin")

def _headers() -> dict:
    """This service's own credential, read now rather than at import.

    It was a module-level dict, and that made the setup page a lie: signing in
    stored a credential, every later call went on using the empty string this
    was built from at boot, and nothing worked until somebody restarted the
    container. A fresh install is precisely the case with no token at import.
    """
    return {
        "Authorization": f'MediaBrowser Token="{config.JELLYFIN_TOKEN}"',
        "Accept": "application/json",
    }

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# The credential check answers a health endpoint, and the container's health
# probe gives that endpoint ten seconds. The ordinary timeout would spend all
# of them waiting on a server that is merely slow.
_CREDENTIAL_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

#: Collection type of a Jellyfin view, per medium this service serves.
COLLECTION_TYPES = {"movie": "movies", "series": "tvshows",
                    "music": "music", "book": "books"}


def normalise_id(value: str) -> str:
    """Jellyfin quotes item ids both dashed and undashed. Compare one way."""
    return (value or "").replace("-", "").lower()


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    is_admin: bool = False

    @property
    def key(self) -> str:
        """How this account is written in the ledger.

        Jellyfin's own account id, not the display name. The name was the key
        until 2026-09-02, and it moves: renaming an account emptied that
        listener's request list and refunded their daily allowance, and
        recreating a name inherited a stranger's outstanding requests. The id
        is in hand on both identity paths, so nothing had to be looked up to
        get this right.
        """
        return self.id


@dataclass(frozen=True, slots=True)
class Owned:
    """What the library already holds, keyed the way the arrs identify things.

    `series_item_ids` maps a provider id to Jellyfin's own item id, which is
    what makes an episode count askable. The count itself is deliberately NOT
    here: this fork returns neither `RecursiveItemCount` nor `ChildCount` on a
    Series however they are requested (measured, both come back null), so the
    only route to it is asking about episodes -- and asking about every
    episode in the library is a 24 MB, six-second answer to a question only
    ever asked about the two or three series somebody is waiting on.
    """
    movie_tmdb: frozenset[str] = frozenset()
    series_tvdb: frozenset[str] = frozenset()
    series_tmdb: frozenset[str] = frozenset()
    series_item_ids: dict[str, str] = field(default_factory=dict)


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.JELLYFIN_URL, headers=_headers(),
                        timeout=_TIMEOUT)


def _to_user(dto: dict) -> User:
    """A Jellyfin UserDto as this app's user. Administrator means keyholder."""
    policy = dto.get("Policy") or {}
    return User(id=dto["Id"], name=dto["Name"],
                is_admin=bool(policy.get("IsAdministrator")))


class TokenRejected(Exception):
    """The caller's Jellyfin access token is missing, malformed, or unknown."""


class JellyfinUnavailable(Exception):
    """Jellyfin could not be reached, so no token can be judged either way."""


#: How long a credential verdict is reused for. Only the browser pages ask for
#: a cached answer: they ask on every page load, and a Jellyfin round trip per
#: load to render a banner that is almost always absent is not worth paying.
CREDENTIAL_CACHE_SECONDS = 60

_credential_verdict: tuple[float, bool] | None = None


def credential_rejected(force: bool = True) -> bool:
    """Whether Jellyfin is actively refusing this service's own API key.

    True means a person has to act: the key was revoked, or the server stopped
    accepting the form it is sent in. Everything else -- a refused connection,
    a timeout, a 5xx -- is Jellyfin having a moment, which this service cannot
    fix and which passes on its own, so it reads as False rather than flapping
    on somebody else's restart.

    `force=False` accepts an answer up to `CREDENTIAL_CACHE_SECONDS` old.
    """
    global _credential_verdict
    if not force and _credential_verdict is not None:
        asked_at, verdict = _credential_verdict
        if time.monotonic() - asked_at < CREDENTIAL_CACHE_SECONDS:
            return verdict
    try:
        with httpx.Client(base_url=config.JELLYFIN_URL, headers=_headers(),
                          timeout=_CREDENTIAL_TIMEOUT) as c:
            resp = c.get("/System/Info")
    except httpx.HTTPError:
        # Not cached. An unreachable Jellyfin is not a verdict about the
        # credential, and storing it as one would keep saying "fine" for a
        # minute after the server comes back refusing.
        return False
    verdict = resp.status_code in (401, 403)
    _credential_verdict = (time.monotonic(), verdict)
    return verdict


def all_users() -> dict[str, str]:
    """Every Jellyfin account, as casefolded display name to account id.

    Only the ledger rekey needs this, and only once.
    """
    with _client() as c:
        users = c.get("/Users").raise_for_status().json()
    return {dto["Name"].casefold(): dto["Id"] for dto in users}


def user(name: str | None = None) -> User:
    """Resolve a proxy-supplied username to the matching Jellyfin account."""
    name = name or config.JELLYFIN_USER
    if not name:
        raise LookupError("no signed-in user and no JELLYFIN_USER configured")
    with _client() as c:
        users = c.get("/Users").raise_for_status().json()
    for dto in users:
        if dto["Name"].casefold() == name.casefold():
            return _to_user(dto)
    log.warning("no Jellyfin account matches signed-in user %r", name)
    raise LookupError(f"no Jellyfin user named {name!r}")


def user_from_token(token: str) -> User:
    """The account a caller's own Jellyfin access token belongs to.

    Deliberately without a fallback. `GET /Users/Me` answers 200 only for a
    real user token: a service API key carries no user context and is refused,
    and an unknown token is refused. Anything that is not a 200 is a rejection.

    The header resolver the browser pages use falls back to `JELLYFIN_USER`
    when no identity is present. That fallback must never be reachable from
    here -- the API path is not behind the proxy, so it would hand any caller
    the owner's requests and the owner's daily allowance.
    """
    if not token:
        raise TokenRejected("no access token")
    headers = {"Authorization": f'MediaBrowser Token="{token}"',
               "Accept": "application/json"}
    try:
        with httpx.Client(base_url=config.JELLYFIN_URL, headers=headers,
                          timeout=_TIMEOUT) as c:
            resp = c.get("/Users/Me")
    except httpx.HTTPError as exc:
        log.error("token introspection unreachable fingerprint=%s (%s)",
                  logs.fingerprint(token), exc)
        raise JellyfinUnavailable(str(exc)) from exc
    if resp.status_code != 200:
        log.warning("token rejected fingerprint=%s status=%d",
                    logs.fingerprint(token), resp.status_code)
        raise TokenRejected(f"Jellyfin answered {resp.status_code}")
    try:
        found = _to_user(resp.json())
    except (ValueError, KeyError) as exc:
        log.error("token introspection returned an unreadable user "
                  "fingerprint=%s", logs.fingerprint(token))
        raise TokenRejected("Jellyfin returned an unreadable user") from exc
    log.info("token accepted fingerprint=%s user=%s keyholder=%s",
             logs.fingerprint(token), found.name, found.is_admin)
    return found


def token_from_header(value: str | None) -> str:
    """The Token= field of a Jellyfin `Authorization` header, or "".

    Clients send the whole handshake rather than a bare token --
    `MediaBrowser Token="abc", Client="EchoFin", Device="iPhone"` -- and the
    order of those fields is not guaranteed, so it is picked out by name.
    """
    if not value:
        return ""
    for part in value.split(","):
        key, sep, raw = part.strip().partition("=")
        if sep and key.strip().rpartition(" ")[2].casefold() == "token":
            return raw.strip().strip('"')
    return ""


def library_ids(medium: str) -> list[str]:
    """Jellyfin view ids this medium covers.

    Configured ids win. With none, every view of the matching collection type
    is used, which is right on almost every server and saves an install from
    copying ids out of a browser URL.
    """
    configured = {
        "movie": config.MOVIE_LIBRARY_IDS,
        "series": config.SERIES_LIBRARY_IDS,
        "music": config.MUSIC_LIBRARY_IDS,
        "book": config.BOOK_LIBRARY_IDS,
    }.get(medium, [])
    if configured:
        return configured
    wanted = COLLECTION_TYPES.get(medium)
    if not wanted:
        return []
    try:
        with _client() as c:
            views = c.get("/Library/VirtualFolders").raise_for_status().json()
    except httpx.HTTPError as exc:
        # Raised, not swallowed into an empty list. An empty list means "this
        # server has no library of that kind", which is a settled fact worth
        # caching; an outage is not, and caching one would leave every medium
        # with no library ids until somebody restarted the process. Empty
        # library ids are exactly what tells a client to show no control at
        # all, so that failure is silent and total.
        log.error("could not list Jellyfin libraries for %s (%s)", medium, exc)
        raise JellyfinUnavailable(str(exc)) from exc
    return [normalise_id(v["ItemId"]) for v in views
            if v.get("CollectionType") == wanted]


def owned_index() -> Owned:
    """Provider ids of everything already in the film and TV libraries.

    One request per medium rather than one per lookup: a client asking about
    twenty search hits would otherwise be twenty round trips to answer a
    question one page of ids already answers.
    """
    movie_tmdb: set[str] = set()
    series_tvdb: set[str] = set()
    series_tmdb: set[str] = set()
    item_ids: dict[str, str] = {}

    for item in _items("movie", "Movie", fields="ProviderIds"):
        if tmdb := _provider(item, "Tmdb"):
            movie_tmdb.add(tmdb)

    for item in _items("series", "Series", fields="ProviderIds"):
        tvdb = _provider(item, "Tvdb")
        tmdb = _provider(item, "Tmdb")
        for key in (tvdb, tmdb):
            if key:
                item_ids[key] = item["Id"]
        if tvdb:
            series_tvdb.add(tvdb)
        if tmdb:
            series_tmdb.add(tmdb)

    log.info("owned index movies=%d series=%d", len(movie_tmdb), len(series_tvdb))
    return Owned(frozenset(movie_tmdb), frozenset(series_tvdb),
                 frozenset(series_tmdb), item_ids)


def episode_count(item_id: str) -> int | None:
    """How many episodes of one series are in the library, or None if unknown.

    `limit=0` with the total requested makes this a fifty-byte answer -- the
    count is the whole point and none of the episodes are wanted. None means
    Jellyfin could not be asked, which is not the same as zero and must not be
    read as one: zero closes nothing but says the series has not started
    arriving, while unknown should leave a request exactly as it was.
    """
    if not item_id:
        return None
    try:
        with _client() as c:
            data = c.get("/Items", params={
                "parentId": item_id,
                "includeItemTypes": "Episode",
                "recursive": "true",
                "limit": 0,
                "enableTotalRecordCount": "true",
                "enableImages": "false",
                "enableUserData": "false",
            }).raise_for_status().json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("episode count failed for %s (%s)", item_id, exc)
        return None
    count = data.get("TotalRecordCount")
    return count if isinstance(count, int) else None


def recommendation_items_for_user(
    medium: str,
    uid: str,
    libraries: list[str] | tuple[str, ...],
) -> list[dict]:
    """Every recommendable item with one user's play state attached.

    This is intentionally separate from ``owned_index``. Arrival checks need
    only provider ids and must stay cheap; recommendations need descriptive
    metadata and, critically, ``userId`` so one account's watching does not
    become another account's taste profile.
    """
    item_type = {"movie": "Movie", "series": "Series"}.get(medium)
    if not item_type:
        return []
    fields = "Genres,People,Studios,CommunityRating,DateCreated,UserData"
    found: dict[str, dict] = {}
    try:
        with _client() as c:
            for library in libraries:
                data = c.get("/Items", params={
                    "parentId": library,
                    "includeItemTypes": item_type,
                    "recursive": "true",
                    "fields": fields,
                    "userId": uid,
                    "limit": 10000,
                }).raise_for_status().json()
                for item in data.get("Items", []):
                    if item_id := item.get("Id"):
                        found[normalise_id(item_id)] = item
    except (httpx.HTTPError, ValueError) as exc:
        log.error("%s recommendation library read failed user=%s (%s)",
                  medium, uid, exc)
        raise JellyfinUnavailable(str(exc)) from exc
    return list(found.values())


def _items(medium: str, item_type: str, fields: str) -> list[dict]:
    """Every item of one type across a medium's libraries."""
    out: list[dict] = []
    libraries = library_ids(medium)
    if not libraries:
        return out
    try:
        with _client() as c:
            for lib in libraries:
                params = {
                    "parentId": lib,
                    "includeItemTypes": item_type,
                    "recursive": "true",
                    "fields": fields,
                    "limit": 10000,
                }
                data = c.get("/Items", params=params).raise_for_status().json()
                out.extend(data.get("Items", []))
    except httpx.HTTPError as exc:
        # An empty index makes everything look unarrived, which shows a request
        # as still on its way. That is the safe direction to be wrong in, but
        # it is invisible without this line -- so it is logged loudly.
        log.error("library index for %s failed; every request will read as "
                  "not yet arrived (%s)", medium, exc)
        return []
    return out


def _provider(item: dict, name: str) -> str:
    """One provider id off an item, as a string, or "" when it has none."""
    ids = item.get("ProviderIds") or {}
    for key, value in ids.items():
        if key.casefold() == name.casefold() and value:
            return str(value).strip()
    return ""


# --- Books -------------------------------------------------------------------

#: Everything the book engine needs to both rank and describe an audiobook.
#: Requested explicitly because Jellyfin omits most of it by default.
_BOOK_FIELDS = (
    "ProviderIds,Genres,UserData,DateCreated,People,Overview,RunTimeTicks,"
    "SeriesName,IndexNumber,ParentIndexNumber,AlbumArtist"
)


def books(uid: str) -> list[dict]:
    """Every audiobook this account can see, with its own play state attached.

    `userId` is not optional here. The play state, the favourites and the
    ratings are what the taste model is built from, and a listing fetched
    without it would make one person's history into everybody's profile.

    The audiobook fork excludes owned multi-part children by default, so this
    returns whole books rather than parts.
    """
    out: list[dict] = []
    libraries = library_ids("book")
    if not libraries:
        return out
    try:
        with _client() as c:
            for lib in libraries:
                data = c.get("/Items", params={
                    "parentId": lib,
                    "includeItemTypes": "AudioBook",
                    "recursive": "true",
                    "fields": _BOOK_FIELDS,
                    "userId": uid,
                    "limit": 5000,
                }).raise_for_status().json()
                out.extend(data.get("Items", []))
    except (httpx.HTTPError, ValueError) as exc:
        # Raised rather than returned empty. An empty book library is what
        # tells the engine there is nothing to recommend, and reporting an
        # outage as that would quietly empty somebody's shelf.
        log.error("book library read failed user=%s (%s)", uid, exc)
        raise JellyfinUnavailable(str(exc)) from exc
    return out


def find_playlist(uid: str, name: str) -> str | None:
    """Id of this account's playlist with that name, or None."""
    with _client() as c:
        data = c.get("/Items", params={
            "includeItemTypes": "Playlist",
            "recursive": "true",
            "userId": uid,
            "limit": 500,
        }).raise_for_status().json()
    for item in data.get("Items", []):
        if item.get("Name") == name:
            return item["Id"]
    return None


def set_playlist(uid: str, name: str, item_ids: list[str]) -> str | None:
    """Create or update a playlist in place, so its id survives between runs.

    A playlist and not a collection: collections are server-global and a shelf
    belongs to one account. Updated in place because recreating it would churn
    the item id and reset whatever the client had scrolled to.
    """
    pid = find_playlist(uid, name)
    # Nothing to create from an empty first result. An existing playlist still
    # has to be cleared below, or a stale shelf survives indefinitely.
    if pid is None and not item_ids:
        return None
    with _client() as c:
        if pid is None:
            created = c.post("/Playlists", json={
                "Name": name, "Ids": item_ids, "UserId": uid,
                "MediaType": "Audio",
            }).raise_for_status().json()
            return created["Id"]
        existing = c.get(f"/Playlists/{pid}/Items",
                         params={"userId": uid, "limit": 5000}
                         ).raise_for_status().json()
        entry_ids = [i["PlaylistItemId"] for i in existing.get("Items", [])
                     if i.get("PlaylistItemId")]
        if entry_ids:
            c.request("DELETE", f"/Playlists/{pid}/Items",
                      params={"entryIds": ",".join(entry_ids)}
                      ).raise_for_status()
        if item_ids:
            c.post(f"/Playlists/{pid}/Items",
                   params={"ids": ",".join(item_ids), "userId": uid}
                   ).raise_for_status()
    return pid


# --- Signing in as a person, rather than as this service ---------------------

#: What this service calls itself in Jellyfin's session list. A person looking
#: at the dashboard should be able to tell what authenticated.
_CLIENT_NAME = "Nextup"
_CLIENT_VERSION = "1"


def _handshake(device: str) -> str:
    """Jellyfin's authorisation header for a client that has no token yet."""
    return (f'MediaBrowser Client="{_CLIENT_NAME}", Device="{_CLIENT_NAME}", '
            f'DeviceId="{device}", Version="{_CLIENT_VERSION}"')


def authenticate(username: str, password: str,
                 device: str) -> tuple[str, User]:
    """Exchange a username and password for that account's access token.

    Used only by the browser pages. The JSON API never sees a password: its
    callers already hold a token, which is the whole reason it can be reached
    without a sign-in proxy in front of it.

    A refusal and an unreachable Jellyfin are different exceptions on purpose.
    "That password is wrong" and "the server is not answering" are different
    things to do next, and a page that says the first when it means the second
    sends somebody to reset a password that was never the problem.
    """
    if not username or not password:
        raise TokenRejected("A username and a password are both needed.")
    try:
        with httpx.Client(base_url=config.JELLYFIN_URL, timeout=_TIMEOUT,
                          headers={"Authorization": _handshake(device),
                                   "Accept": "application/json"}) as c:
            resp = c.post("/Users/AuthenticateByName",
                          json={"Username": username, "Pw": password})
    except httpx.HTTPError as exc:
        log.error("sign-in could not reach Jellyfin (%s)", exc)
        raise JellyfinUnavailable(str(exc)) from exc
    if resp.status_code in (400, 401, 403):
        log.warning("sign-in refused for %r (%d)", username, resp.status_code)
        raise TokenRejected("Jellyfin did not accept that username and password.")
    if resp.status_code >= 400:
        log.error("sign-in got %d from Jellyfin", resp.status_code)
        raise JellyfinUnavailable(f"Jellyfin answered {resp.status_code}.")
    try:
        body = resp.json()
        token = body["AccessToken"]
        dto = body["User"]
    except (ValueError, KeyError, TypeError) as exc:
        log.error("sign-in answer from Jellyfin would not parse (%s)", exc)
        raise JellyfinUnavailable("Jellyfin's answer could not be read.") from exc
    return token, _to_user(dto)


#: The item type an audiobook fork files whole books under. Stock Jellyfin has
#: no such type: its Books libraries hold `Book` items, which are ebooks, and
#: audiobooks on a stock server live in a music library as albums and tracks.
AUDIOBOOK_TYPE = "AudioBook"


def serves_audiobooks() -> bool | None:
    """Whether this Jellyfin files audiobooks as whole books.

    True on a fork that has the `AudioBook` item type, False on stock
    Jellyfin, None when the question could not be asked.

    It matters because a books library exists on both, and on stock Jellyfin it
    holds ebooks. Offering the book medium there would produce a search box
    that can ask for audiobooks and a library that can never show one arriving
    -- the exact silent absence this service is arranged against. Better to say
    plainly that books need the fork.

    Not folded into `library_ids`: a stock server's books library should still
    be *found*, so the doctor can say what it found and why it is not offered.
    """
    libraries = library_ids("book")
    if not libraries:
        return None
    try:
        with _client() as c:
            for lib in libraries:
                data = c.get("/Items", params={
                    "parentId": lib,
                    "includeItemTypes": AUDIOBOOK_TYPE,
                    "recursive": "true",
                    "limit": 1,
                }).raise_for_status().json()
                if data.get("TotalRecordCount"):
                    return True
        # Asked and answered zero. An empty library on a fork looks the same as
        # a stock server, which is the honest answer: nothing here says books
        # can be served, so nothing claims they can.
        return False
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not ask whether this Jellyfin serves audiobooks (%s)",
                    exc)
        return None
