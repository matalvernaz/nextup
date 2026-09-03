"""Series, via Sonarr.

A series is the one medium here that does not arrive all at once. Sonarr
accepts the request immediately and the episodes land over hours or weeks, so
`arrived` is not a yes or no: it is a yes with a count, and a request whose
series exists with no episode in it has not arrived at all.
"""
from typing import NamedTuple

import httpx

from . import arr, config, logs

log = logs.get("sonarr")

MEDIUM = "series"
UNIT = "series"
_PROGRESS_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class AcquisitionProgress(NamedTuple):
    """What Sonarr currently knows about one whole-series request."""

    episodes_total: int | None
    episodes_queued: int | None


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


def acquisition_progress(
    backend_ids: set[str],
) -> dict[str, AcquisitionProgress]:
    """Aired and queued episode counts from one batched Sonarr read.

    Jellyfin remains the authority for what a listener can actually play.
    Sonarr supplies the denominator and work in flight that Jellyfin cannot:
    without them, one imported episode looks indistinguishable from a complete
    series request.
    """
    wanted = {str(value) for value in backend_ids if value}
    tool = backend()
    if not wanted or not tool.configured:
        return {}
    try:
        # Status is optional detail on a request-list response. It must fail
        # faster than an acquisition write so one stopped Sonarr does not leave
        # the whole screen saying only "Loading" for the transport's 30s cap.
        with tool.client(timeout=_PROGRESS_TIMEOUT) as client:
            response = client.get("/series")
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                return {}
            totals = {
                str(row["id"]): _count(
                    (row.get("statistics") or {}).get("episodeCount"))
                for row in rows
                if isinstance(row, dict)
                and str(row.get("id") or "") in wanted
                and isinstance(row.get("statistics") or {}, dict)
            }
            queued = _queued_counts(client, wanted)
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("Sonarr progress failed ids=%d (%s)", len(wanted), exc)
        return {}
    return {
        backend_id: AcquisitionProgress(
            total,
            None if queued is None else queued.get(backend_id, 0),
        )
        for backend_id, total in totals.items()
    }


def _queued_counts(
    client: httpx.Client,
    backend_ids: set[str],
) -> dict[str, int] | None:
    """Distinct queued episodes per requested Sonarr series."""
    try:
        response = client.get("/queue/details")
        response.raise_for_status()
        rows = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Sonarr queue progress failed ids=%d (%s)",
                    len(backend_ids), exc)
        return None
    if not isinstance(rows, list):
        return None
    episode_ids = {backend_id: set() for backend_id in backend_ids}
    for row in rows:
        if not isinstance(row, dict):
            continue
        backend_id = str(row.get("seriesId") or "")
        episode_id = row.get("episodeId")
        if backend_id in episode_ids and episode_id is not None:
            episode_ids[backend_id].add(episode_id)
    return {backend_id: len(ids) for backend_id, ids in episode_ids.items()}


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


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
