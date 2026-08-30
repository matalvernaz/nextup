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
from dataclasses import dataclass, field

import httpx

from . import config, logs

log = logs.get("jellyfin")

_HEADERS = {
    "Authorization": f'MediaBrowser Token="{config.JELLYFIN_TOKEN}"',
    "Accept": "application/json",
}

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Collection type of a Jellyfin view, per medium this service serves.
COLLECTION_TYPES = {"movie": "movies", "series": "tvshows", "music": "music"}


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
        """Stable-enough local scope, shared with the proxy's username."""
        return self.name.casefold()


@dataclass(frozen=True, slots=True)
class Owned:
    """What the library already holds, keyed the way the arrs identify things.

    `series_episodes` counts episode files rather than merely recording that a
    series exists. A series is not a single arrival: Sonarr accepts the request
    the moment it is added and the first episode may be days behind the last,
    so "in the library" has to be able to say how much of it is.
    """
    movie_tmdb: frozenset[str] = frozenset()
    series_tvdb: frozenset[str] = frozenset()
    series_tmdb: frozenset[str] = frozenset()
    series_episodes: dict[str, int] = field(default_factory=dict)


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.JELLYFIN_URL, headers=_HEADERS,
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
        # Not fatal: a medium with no library ids is simply not advertised, and
        # saying so beats refusing to start over a transient Jellyfin blip.
        log.error("could not list Jellyfin libraries for %s (%s)", medium, exc)
        return []
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
    episodes: dict[str, int] = {}

    for item in _items("movie", "Movie", fields="ProviderIds"):
        if tmdb := _provider(item, "Tmdb"):
            movie_tmdb.add(tmdb)

    for item in _items("series", "Series",
                       fields="ProviderIds,RecursiveItemCount"):
        tvdb = _provider(item, "Tvdb")
        tmdb = _provider(item, "Tmdb")
        # Jellyfin counts a series' descendants here, which for a series is its
        # episodes. Absent on an older server, in which case the series is
        # known to exist and its progress is not, and nothing pretends it is.
        count = item.get("RecursiveItemCount")
        for key in (tvdb, tmdb):
            if not key:
                continue
            if isinstance(count, int):
                episodes[key] = count
        if tvdb:
            series_tvdb.add(tvdb)
        if tmdb:
            series_tmdb.add(tmdb)

    log.info("owned index movies=%d series=%d", len(movie_tmdb), len(series_tvdb))
    return Owned(frozenset(movie_tmdb), frozenset(series_tvdb),
                 frozenset(series_tmdb), episodes)


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
