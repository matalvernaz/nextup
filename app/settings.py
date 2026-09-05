"""Settings a person can change from a page, and how they meet the environment.

The connection settings -- where Jellyfin is, which acquisition tools this
household runs and how to reach them -- used to be environment variables only.
That is right for a deployment described in a compose file and checked into
somewhere, and wrong for somebody who has just run `docker compose up -d` and
wants to point the thing at their Radarr. It also meant the quality profile
was a number to be dug out of another application's URL and typed in blind.

So the same settings can now be written from the Backends page, and are kept
here. **The environment still wins.** A value set in `.env` is a deliberate
statement about a deployment, and a page that silently overrode it would make
a compose file a lie; the page says which settings are held that way and does
not offer to change them.

Nothing tuning-related lives here. Weights, caps, TTLs and the like stay in
`config` as constants, because they are decisions about behaviour rather than
about wiring, and a page full of them would bury the four fields that matter.
"""
import threading

from . import logs, store

log = logs.get("settings")

#: Only these may be written from a page. An allowlist rather than "anything
#: in config": a settings table that can set arbitrary names is one where a
#: bug or a bad actor sets `DB_PATH`.
WRITABLE = frozenset({
    "JELLYFIN_URL",
    "JELLYFIN_TOKEN",
    "RADARR_URL", "RADARR_API_KEY", "RADARR_QUALITY_PROFILE_ID",
    "RADARR_ROOT_FOLDER",
    "SONARR_URL", "SONARR_API_KEY", "SONARR_QUALITY_PROFILE_ID",
    "SONARR_ROOT_FOLDER",
    "BUSKARR_URL", "BUSKARR_API_KEY",
    "LISTENARR_URL", "LISTENARR_QUALITY_PROFILE_ID",
    "MOVIE_LIBRARY_IDS", "SERIES_LIBRARY_IDS", "MUSIC_LIBRARY_IDS",
    "BOOK_LIBRARY_IDS",
})

#: Never logged, never rendered back into a form field, never in the doctor's
#: output. A key that has been set is shown as the fact that it is set.
SECRET = frozenset({
    "JELLYFIN_TOKEN", "RADARR_API_KEY", "SONARR_API_KEY", "BUSKARR_API_KEY",
})

_PREFIX = "setting:"

_cache: dict[str, str] | None = None
_guard = threading.Lock()


def _load() -> dict[str, str]:
    """Every stored setting, cached until something writes one.

    Cached because `config` reads through this on every attribute access, and
    that happens per request rather than per deployment. Read straight through
    on the first miss and tolerant of a database that does not exist yet: this
    is imported long before `store.init()` runs.
    """
    global _cache
    with _guard:
        if _cache is not None:
            return _cache
    try:
        with store.db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE ?",
                (_PREFIX + "%",)).fetchall()
        found = {row["key"][len(_PREFIX):]: row["value"] for row in rows}
    except Exception as exc:  # noqa: BLE001 -- no table yet is the ordinary case
        log.debug("no stored settings yet (%s)", exc)
        found = {}
    with _guard:
        _cache = found
        return found


def get(name: str) -> str | None:
    """A stored value, or None. Says nothing about the environment."""
    return _load().get(name)


def put(name: str, value: str) -> None:
    """Store one setting. An empty value removes it rather than storing "".

    Removing rather than blanking is what lets a setting go back to being
    unset, which for a backend URL is how a household says it no longer runs
    that tool.
    """
    if name not in WRITABLE:
        raise KeyError(f"{name} is not settable from a page")
    global _cache
    with store.db() as conn:
        if value:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (_PREFIX + name, value))
        else:
            conn.execute("DELETE FROM meta WHERE key=?", (_PREFIX + name,))
    with _guard:
        _cache = None
    log.info("set %s to %s", name,
             "a new value" if name in SECRET else repr(value) if value
             else "unset")


def put_all(values: dict[str, str]) -> None:
    for name, value in values.items():
        put(name, value)


def forget() -> None:
    """Drop the cache, so the next read goes to the database."""
    global _cache
    with _guard:
        _cache = None


def held_in_environment(name: str) -> bool:
    """Whether the environment is deciding this one, so a page cannot.

    Imported here rather than at the top because `config` reads through this
    module and importing it the other way round would be a cycle.
    """
    import os
    return bool(os.environ.get(name, "").strip())
