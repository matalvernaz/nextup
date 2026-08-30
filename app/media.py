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


def _build() -> dict[str, Medium]:
    found: dict[str, Medium] = {}
    if radarr.configured():
        found[MOVIE] = Medium(MOVIE, "Films", ("movie",),
                              config.MOVIE_DAILY_CAP,
                              tuple(jellyfin.library_ids(MOVIE)))
    if sonarr.configured():
        found[SERIES] = Medium(SERIES, "Series", ("series",),
                               config.SERIES_DAILY_CAP,
                               tuple(jellyfin.library_ids(SERIES)))
    if buskarr.configured():
        found[MUSIC] = Medium(MUSIC, "Music", buskarr.UNITS,
                              config.MUSIC_DAILY_CAP,
                              tuple(jellyfin.library_ids(MUSIC)))
    for key in (MOVIE, SERIES, MUSIC):
        if key not in found:
            log.info("%s is not offered: its backend is not configured", key)
        elif not found[key].library_ids:
            # Offered anyway. A library that has not been created yet is the
            # ordinary case for somebody setting this up before they own
            # anything, and refusing the medium would make the first request
            # impossible.
            log.warning("%s has no matching Jellyfin library; requests will "
                        "work but nothing will ever read as arrived", key)
    return found


_registry: dict[str, Medium] | None = None
_registry_guard = threading.Lock()


def available() -> dict[str, Medium]:
    """The media this server serves, built once per process.

    Cached because it costs a Jellyfin round trip and the answer only changes
    when the deployment does. `forget` exists for tests.
    """
    global _registry
    with _registry_guard:
        if _registry is None:
            _registry = _build()
            log.info("serving media: %s", ", ".join(sorted(_registry)) or "none")
        return _registry


def forget() -> None:
    """Drop the cached registry and library index."""
    global _registry
    with _registry_guard:
        _registry = None
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
