"""Series, via Sonarr.

A series is the one medium here that does not arrive all at once. Sonarr
accepts the request immediately and the episodes land over hours or weeks, so
`arrived` is not a yes or no: it is a yes with a count, and a request whose
series exists with no episode in it has not arrived at all.
"""
from . import arr, config, logs

log = logs.get("sonarr")

MEDIUM = "series"
UNIT = "series"


def backend() -> arr.Arr:
    return arr.Arr(
        name="Sonarr",
        url=config.SONARR_URL,
        api_key=config.SONARR_API_KEY,
        quality_profile_id=config.SONARR_QUALITY_PROFILE_ID,
        root_folder=config.SONARR_ROOT_FOLDER,
        resource="series",
        id_field="tvdbId",
    )


def configured() -> bool:
    return backend().configured


def item_key(tvdb_id) -> str:
    return f"tvdb:{tvdb_id}"


def search(query: str, limit: int, owned: frozenset[str]) -> list[dict]:
    """Series matching a title, marked with whether the library has one.

    Marked, not counted. How many episodes are held is one request per series
    on this Jellyfin, and a page of twenty-five hits would pay for all of them
    to show a number nobody asked for. The count belongs on the request list,
    where there are three rows and it is the thing being watched.
    """
    rows = backend().lookup(query, limit)
    return [_result(row, owned) for row in rows if row.get("tvdbId")]


def _result(row: dict, owned: frozenset[str]) -> dict:
    tvdb = str(row["tvdbId"])
    return {
        "itemKey": item_key(tvdb),
        "medium": MEDIUM,
        "unit": UNIT,
        "title": row.get("title") or "",
        "year": str(row.get("year") or ""),
        "overview": (row.get("overview") or "").strip(),
        "network": row.get("network") or "",
        "status": row.get("status") or "",
        "seasonCount": _season_count(row),
        "owned": tvdb in owned,
    }


def _season_count(row: dict) -> int:
    """Real seasons, excluding the specials season Sonarr numbers zero."""
    seasons = row.get("seasons") or []
    return sum(1 for s in seasons if s.get("seasonNumber"))


def add(tvdb_id: str, title: str = "", monitored: bool = True) -> arr.AddResult:
    """Hand one series to Sonarr.

    Monitors whatever `SONARR_MONITOR` says, `all` by default. Somebody asking
    for a series they do not have means the series; a first-season default
    would quietly fill half the request and report it as met.
    """
    tool = backend()
    if not tool.configured:
        return arr.AddResult(False, "Series are not available on this server.")

    if (row := tool.existing(tvdb_id)) is not None:
        return arr.AddResult(True, "Already in Sonarr.", str(row.get("id") or ""),
                             row.get("title") or title,
                             str(row.get("year") or ""))

    root = tool.root_folder_path()
    if not root:
        return arr.AddResult(
            False, "Sonarr has no usable root folder configured.")

    found = _lookup_one(tool, tvdb_id)
    if found is None:
        return arr.AddResult(
            False, "That series could not be identified well enough to ask "
                   "for it. Nothing was added.")

    body = {
        **found,
        "qualityProfileId": tool.quality_profile_id,
        "rootFolderPath": root,
        "monitored": monitored,
        "seasonFolder": config.SONARR_SEASON_FOLDER,
        "addOptions": {
            "monitor": config.SONARR_MONITOR if monitored else "none",
            "searchForMissingEpisodes": monitored,
            "searchForCutoffUnmetEpisodes": False,
        },
    }
    result = tool.add(body)
    log.info("add tvdb=%s monitored=%s monitor=%s ok=%s message=%s",
             tvdb_id, monitored, config.SONARR_MONITOR, result.ok, result.message)
    return result


def _lookup_one(tool: arr.Arr, tvdb_id: str) -> dict | None:
    for row in tool.lookup(f"tvdb:{tvdb_id}", limit=1):
        if str(row.get("tvdbId")) == str(tvdb_id):
            return row
    return None


def cancel(backend_id: str) -> bool:
    return backend().delete(backend_id)


def arrived(item_keys: set[str], owned: frozenset[str],
            episodes: dict[str, int]) -> set[str]:
    """Which series have started landing.

    A series with a folder and no episode file has not arrived: Sonarr creates
    the series row the moment it is added, so counting that as an arrival would
    close the request before anything was downloaded and take it off the list
    the person is watching.
    """
    out = set()
    for key in item_keys:
        if not key.startswith("tvdb:"):
            continue
        tvdb = key.split(":", 1)[1]
        if tvdb in owned and episodes.get(tvdb, 0) > 0:
            out.add(key)
    return out


def progress(item_key_value: str, episodes: dict[str, int]) -> int | None:
    """Episodes of this series in the library, or None where it is not known.

    None rather than zero. Zero is a real answer -- the series exists and
    nothing has downloaded -- and a Jellyfin that could not be asked must not
    be reported as having said it.
    """
    if not item_key_value.startswith("tvdb:"):
        return None
    return episodes.get(item_key_value.split(":", 1)[1])
