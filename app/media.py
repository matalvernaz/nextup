"""Which media this deployment can actually serve, and the library index.

The registry is derived, never declared. A medium appears because its backend
is configured and Jellyfin has a library of that kind -- so an install with
only Radarr offers films and says nothing at all about series or music. That
is what makes this service safe to point EchoFin at without knowing in advance
what is behind it.
"""
import threading
import time
from dataclasses import dataclass

from . import buskarr, config, jellyfin, logs, radarr, sonarr

log = logs.get("media")

MOVIE = radarr.MEDIUM
SERIES = sonarr.MEDIUM
MUSIC = buskarr.MEDIUM


@dataclass(frozen=True, slots=True)
class Medium:
    """One kind of thing this server can be asked for."""
    key: str
    #: What to call it on screen. Plural, because it names a section.
    label: str
    #: What can be asked for within it. One for film and TV, three for music.
    units: tuple[str, ...]
    daily_cap: int
    library_ids: tuple[str, ...]


#: Cost of one request, by medium and unit. Film and TV are one apiece; music
#: is weighted because its three units are not remotely the same size.
def cost(medium: str, unit: str) -> int:
    if medium == MUSIC:
        return buskarr.COSTS.get(unit, 1)
    return 1


#: One medium's backend, its label, its cap, and the units it offers. The
#: library ids are looked up separately because that is the part that can fail.
_BACKENDS = (
    (MOVIE, "Films", ("movie",), radarr.configured, lambda: config.MOVIE_DAILY_CAP),
    (SERIES, "Series", ("series",), sonarr.configured, lambda: config.SERIES_DAILY_CAP),
    (MUSIC, "Music", buskarr.UNITS, buskarr.configured, lambda: config.MUSIC_DAILY_CAP),
)


def _build() -> tuple[dict[str, Medium], bool]:
    """The registry, and whether it is complete enough to keep.

    An incomplete build is one where Jellyfin could not be asked which
    libraries exist. It is still served -- refusing every medium because a
    library listing timed out would be worse -- but it is not cached, because
    empty library ids are exactly what tells a client to show no control at
    all, and this box has started services before Jellyfin was up.
    """
    found: dict[str, Medium] = {}
    complete = True
    for key, label, units, is_configured, cap in _BACKENDS:
        if not is_configured():
            log.info("%s is not offered: its backend is not configured", key)
            continue
        try:
            libraries = tuple(jellyfin.library_ids(key))
        except jellyfin.JellyfinUnavailable:
            log.error("%s offered without library ids: Jellyfin could not be "
                      "asked. This will be retried rather than cached.", key)
            libraries = ()
            complete = False
        else:
            if not libraries:
                # A library that does not exist yet is the ordinary case for
                # somebody setting this up before they own anything, and
                # refusing the medium would make the first request impossible.
                # Served, then, but not settled: empty library ids are what
                # tell a client to show no control at all, and creating the
                # library ought to be enough on its own to make one appear.
                log.warning("%s has no matching Jellyfin library; requests "
                            "will work but nothing will read as arrived", key)
                complete = False
        found[key] = Medium(key, label, tuple(units), cap(), libraries)
    return found, complete


#: How long an unsettled registry is kept before it is built again. Long
#: enough that requests do not each pay for a Jellyfin round trip while the
#: deployment is half up; short enough that starting Jellyfin, or creating the
#: library a configured backend has no items in yet, is enough on its own.
PROVISIONAL_TTL_SECONDS = 300

_registry: dict[str, Medium] | None = None
_registry_built_at = 0.0
_registry_settled = False
_registry_guard = threading.Lock()


def available() -> dict[str, Medium]:
    """The media this server serves.

    Cached, because it costs a Jellyfin round trip and a settled answer only
    changes when the deployment does. An unsettled one -- Jellyfin could not be
    asked, or a configured backend has no library yet -- is cached only briefly,
    so a startup-order accident does not outlive the deployment that caused it.
    """
    global _registry, _registry_built_at, _registry_settled
    with _registry_guard:
        if _registry is not None and (
                _registry_settled
                or time.monotonic() - _registry_built_at < PROVISIONAL_TTL_SECONDS):
            return _registry
    built, settled = _build()
    with _registry_guard:
        _registry = built
        _registry_built_at = time.monotonic()
        _registry_settled = settled
        if settled:
            log.info("serving media: %s", ", ".join(sorted(built)) or "none")
        return built


def forget() -> None:
    """Drop the cached registry and library index."""
    global _registry, _registry_settled
    with _registry_guard:
        _registry = None
        _registry_settled = False
    _owned.forget()


def get(medium: str) -> Medium | None:
    return available().get(medium)


class _OwnedCache:
    """The library index, rebuilt no more often than its TTL.

    Arrival can only ever be as fresh as this. Fifteen minutes by default:
    long enough that a screenful of requests costs one Jellyfin scan, short
    enough that a film imported while somebody is looking at the screen shows
    up on their next visit rather than their next hour.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._value: jellyfin.Owned | None = None
        self._built_at = 0.0

    def get(self, force: bool = False) -> jellyfin.Owned:
        with self._guard:
            fresh = (self._value is not None and not force and
                     time.monotonic() - self._built_at
                     <= config.OWNED_INDEX_TTL_SECONDS)
            if fresh:
                return self._value
        # Built outside the lock: it is a Jellyfin round trip over the whole
        # film and TV libraries, and holding the lock across it would make
        # every concurrent caller wait for one slow scan.
        built = jellyfin.owned_index()
        with self._guard:
            self._value = built
            self._built_at = time.monotonic()
        return built

    def forget(self) -> None:
        with self._guard:
            self._value = None
            self._built_at = 0.0


_owned = _OwnedCache()


def owned(force: bool = False) -> jellyfin.Owned:
    return _owned.get(force=force)


def episode_counts(provider_ids: set[str]) -> dict[str, int]:
    """Episodes in the library for each of these series, where askable.

    Asked per series rather than indexed for the whole library, because the
    only route to the number on this Jellyfin is to count episodes, and the
    library-wide answer is 24 MB to answer a question about the two or three
    series somebody is actually waiting on. A series Jellyfin will not answer
    for is simply absent from the result, which the caller reads as unknown.
    """
    index = owned()
    counts: dict[str, int] = {}
    for provider_id in provider_ids:
        item_id = index.series_item_ids.get(provider_id)
        if not item_id:
            continue
        count = jellyfin.episode_count(item_id)
        if count is not None:
            counts[provider_id] = count
    return counts
