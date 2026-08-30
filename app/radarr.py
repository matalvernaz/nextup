"""Films, via Radarr.

Nothing here treats Radarr as a catalogue of what is owned. On the library
this was built against Radarr held 27 films against Jellyfin's 431 -- it knows
what it fetched, not what is on the disk. Ownership questions go to Jellyfin.
"""
from . import arr, config, logs

log = logs.get("radarr")

MEDIUM = "movie"
UNIT = "movie"

#: How Radarr decides a film is worth searching for. `released` rather than
#: `announced`: asking for a film that has not come out yet otherwise fills the
#: queue with searches that cannot succeed for months.
MINIMUM_AVAILABILITY = "released"


def backend() -> arr.Arr:
    return arr.Arr(
        name="Radarr",
        url=config.RADARR_URL,
        api_key=config.RADARR_API_KEY,
        quality_profile_id=config.RADARR_QUALITY_PROFILE_ID,
        root_folder=config.RADARR_ROOT_FOLDER,
        resource="movie",
        id_field="tmdbId",
    )


def configured() -> bool:
    return backend().configured


def item_key(tmdb_id) -> str:
    """The ledger key for a film. Prefixed, because a bare number says nothing
    about which catalogue issued it and the ledger holds three media."""
    return f"tmdb:{tmdb_id}"


def search(query: str, limit: int, owned: frozenset[str]) -> list[dict]:
    """Films matching a title, marked with whether the library already has one.

    Owned films are marked rather than dropped. On a recommendation shelf an
    owned film is noise; to somebody typing its title it is the answer, and
    hiding it reads as the search being broken.
    """
    rows = backend().lookup(query, limit)
    return [_result(row, owned) for row in rows if row.get("tmdbId")]


def _result(row: dict, owned: frozenset[str]) -> dict:
    tmdb = str(row["tmdbId"])
    return {
        "itemKey": item_key(tmdb),
        "medium": MEDIUM,
        "unit": UNIT,
        "title": row.get("title") or "",
        "year": str(row.get("year") or ""),
        "overview": (row.get("overview") or "").strip(),
        "runtimeMinutes": row.get("runtime") or None,
        "owned": tmdb in owned,
    }


def add(tmdb_id: str, title: str = "", year: str = "",
        monitored: bool = True) -> arr.AddResult:
    """Hand one film to Radarr.

    `monitored=False` exists for verification runs. A default add is swept
    within the hour and downloads for real, so a test that leaves one behind
    spends bandwidth on a film nobody asked for.
    """
    tool = backend()
    if not tool.configured:
        return arr.AddResult(False, "Films are not available on this server.")

    if (row := tool.existing(tmdb_id)) is not None:
        return arr.AddResult(True, "Already in Radarr.", str(row.get("id") or ""),
                             row.get("title") or title,
                             str(row.get("year") or year))

    root = tool.root_folder_path()
    if not root:
        return arr.AddResult(
            False, "Radarr has no usable root folder configured.")

    found = _lookup_one(tool, tmdb_id)
    if found is None:
        return arr.AddResult(
            False, "That film could not be identified well enough to ask for "
                   "it. Nothing was added.")

    body = {
        **found,
        "qualityProfileId": tool.quality_profile_id,
        "rootFolderPath": root,
        "monitored": monitored,
        "minimumAvailability": MINIMUM_AVAILABILITY,
        # Searching immediately is the point of asking. Radarr queues the
        # search rather than running it inline, so this does not hold the
        # request open while indexers are polled.
        "addOptions": {"searchForMovie": monitored},
    }
    result = tool.add(body)
    log.info("add tmdb=%s monitored=%s ok=%s message=%s",
             tmdb_id, monitored, result.ok, result.message)
    return result


def _lookup_one(tool: arr.Arr, tmdb_id: str) -> dict | None:
    """Radarr's own record for one TMDB id.

    Posting Radarr's own lookup row back to it is what keeps titles, years and
    images consistent with what it would have stored anyway. A hand-built body
    would have to guess at fields this returns for free.
    """
    for row in tool.lookup(f"tmdb:{tmdb_id}", limit=1):
        if str(row.get("tmdbId")) == str(tmdb_id):
            return row
    return None


def cancel(backend_id: str) -> bool:
    """Stop looking for a film. Files already downloaded are left alone."""
    return backend().delete(backend_id)


def arrived(item_keys: set[str], owned: frozenset[str]) -> set[str]:
    """Which of these ledger keys are now in the Jellyfin film library."""
    return {key for key in item_keys
            if key.startswith("tmdb:") and key.split(":", 1)[1] in owned}
