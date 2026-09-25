"""Series, via Sonarr.

A series is the one medium here that does not arrive all at once. Sonarr
accepts the request immediately and the episodes land over hours or weeks, so
`arrived` is not a yes or no: it is a yes with a count, and a request whose
series exists with no episode in it has not arrived at all.
"""
import time
from datetime import datetime, timezone
from typing import NamedTuple

import httpx

from . import arr, config, logs, seasons

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


def add(tvdb_id: str, title: str = "", monitored: bool = True,
        choice: seasons.Seasons | None = None) -> arr.AddResult:
    """Hand one series to Sonarr.

    With no choice of seasons it monitors whatever `SONARR_MONITOR` says, `all`
    by default. Somebody asking for a series they do not have means the series;
    a first-season default would quietly fill half the request and report it
    as met. A choice is them saying how much they meant, and the request is met
    when that much has arrived: Sonarr's episode total counts only monitored
    episodes, so the arrival and progress code needs to know nothing about it.
    """
    tool = backend()
    if not tool.configured:
        return arr.AddResult(False, "Series are not available on this server.")

    if (row := tool.existing(tvdb_id)) is not None:
        return arr.AddResult(True, "Already in Sonarr.", "",
                             row.get("title") or title,
                             str(row.get("year") or ""), created=False)

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
    if not monitored or choice is None:
        result = tool.add(body)
        log.info("add tvdb=%s monitored=%s monitor=%s ok=%s message=%s",
                 tvdb_id, monitored, body["addOptions"]["monitor"],
                 result.ok, result.message)
        return result

    name = found.get("title") or title or "That series"
    listed = _listed_seasons(found)
    if choice.choice == seasons.ALL:
        body["addOptions"]["monitor"] = "all"
        asked = choice
    elif choice.choice == seasons.RANGE:
        chosen = [number for number in listed
                  if choice.first <= number <= choice.last]
        if not chosen:
            return arr.AddResult(False, _outside(name, listed, choice))
        asked = seasons.Seasons(seasons.RANGE, chosen[0], chosen[-1])
        _monitor_only(body, set(chosen))
        # A stretch is those seasons. One that starts airing later was not
        # among them.
        body["monitorNewItems"] = "none"
    elif choice.choice == seasons.NEW:
        if (found.get("status") or "").lower() == "ended":
            return arr.AddResult(
                False, f"{name} has ended, so no new episodes will air. "
                       "Nothing was added.")
        asked = choice
        body["addOptions"]["monitor"] = "future"
        body["addOptions"]["searchForMissingEpisodes"] = False
        body["monitorNewItems"] = "all"
    else:
        # Which season is the latest to have aired is not in the catalogue
        # record, which lists announced seasons as readily as aired ones (The
        # Simpsons: 38 listed, 37 aired, 2026-09-25). So nothing is monitored
        # or searched until Sonarr has the episode list to decide from.
        _monitor_only(body, set())
        body["addOptions"]["searchForMissingEpisodes"] = False
        body["monitorNewItems"] = "all"
        asked = choice

    result = tool.add(body)
    log.info("add tvdb=%s seasons=%s monitor=%s ok=%s message=%s",
             tvdb_id, asked.encode(), body["addOptions"]["monitor"],
             result.ok, result.message)
    if not result.ok or not result.created:
        return result
    if asked.choice == seasons.LATEST:
        latest = _monitor_latest(tool, result.backend_id, listed)
        if latest is None:
            # Left in place it would be a series Sonarr holds and never
            # fetches, and the request would wait on it for good.
            tool.delete(result.backend_id)
            return arr.AddResult(
                False, f"Sonarr took {name} but could not be told which "
                       "season to fetch, so it was taken back out. Try again "
                       "in a minute.")
        asked = seasons.Seasons(seasons.LATEST, 0, latest)
    return result._replace(message=_asked_message(asked),
                           seasons=asked.encode())


#: Injected by the tests, so waiting for Sonarr takes no real time there.
_sleep = time.sleep
_clock = time.monotonic


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _listed_seasons(row: dict) -> list[int]:
    """Real season numbers, lowest first. Sonarr numbers the specials zero."""
    return sorted({number for number in
                   (season.get("seasonNumber") for season in row.get("seasons") or [])
                   if type(number) is int and number > 0})


def _monitor_only(body: dict, chosen: set[int]) -> None:
    """Monitor exactly these seasons, and tell Sonarr to go by the seasons.

    `skip` is what makes Sonarr take the per-season flags as given rather
    than re-deriving them from a monitor mode (verified 2026-09-25: Blackadder
    with seasons 2 and 3 monitored 12 of its 54 episodes, specials off).
    """
    body["seasons"] = [
        {**season, "monitored": season.get("seasonNumber") in chosen}
        for season in body.get("seasons") or []]
    body["addOptions"]["monitor"] = "skip"


def _outside(name: str, listed: list[int], choice: seasons.Seasons) -> str:
    if not listed:
        return f"Sonarr lists no seasons of {name} yet. Nothing was added."
    held = (f"season {listed[0]}" if len(listed) == 1
            else f"seasons {listed[0]} to {listed[-1]}")
    return (f"{name} has {held}, so {choice.phrase()} would fetch nothing. "
            "Nothing was added.")


def _asked_message(asked: seasons.Seasons) -> str:
    sentence = f"Asked for {asked.phrase()}."
    if asked.choice == seasons.LATEST:
        sentence += " New episodes will follow as they air."
    return sentence


def _monitor_latest(tool: arr.Arr, backend_id: str,
                    listed: list[int]) -> int | None:
    """Monitor the latest season to have aired, and every season listed after
    it, then search. The season, or None when Sonarr could not be told.

    The seasons after it are announced ones. They are not new to Sonarr when
    they start airing, so the new-season setting would not pick them up, and
    somebody who asked for the latest season means to keep up.
    """
    episodes = _wait_for_episodes(tool, backend_id)
    now = _now()
    aired = {episode["seasonNumber"] for episode in episodes
             if type(episode.get("seasonNumber")) is int
             and episode["seasonNumber"] > 0 and _aired(episode, now)}
    if aired:
        latest = max(aired)
    elif listed:
        # No episode list within the wait: the catalogue's highest season,
        # which is what Sonarr's own "last season" means. Said in the log,
        # because it may be a season that has not started.
        latest = listed[-1]
        log.warning("latest season for series=%s decided without an episode "
                    "list: season %d", backend_id, latest)
    else:
        return None
    try:
        with tool.client() as client:
            row = client.get(f"/series/{backend_id}").raise_for_status().json()
            for season in row.get("seasons") or []:
                number = season.get("seasonNumber")
                season["monitored"] = type(number) is int and number >= latest
            client.put(f"/series/{backend_id}", json=row).raise_for_status()
            client.post("/command", json={
                "name": "SeriesSearch",
                "seriesId": int(backend_id)}).raise_for_status()
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        log.error("latest season for series=%s could not be set (%s)",
                  backend_id, exc)
        return None
    log.info("latest season for series=%s is %d", backend_id, latest)
    return latest


def _wait_for_episodes(tool: arr.Arr, backend_id: str) -> list[dict]:
    """The series' episodes once Sonarr has listed them, or [] after the wait.

    Settled means two reads in a row agreeing on a count above zero. Usually a
    few seconds (877 episodes of The Simpsons in 3.4), but the refresh is a
    queued command and can wait behind others.
    """
    deadline = _clock() + config.SONARR_LATEST_WAIT_SECONDS
    previous = -1
    while True:
        episodes = []
        try:
            with tool.client() as client:
                response = client.get("/episode", params={"seriesId": backend_id})
                response.raise_for_status()
                episodes = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("episode list for series=%s not read yet (%s)",
                        backend_id, exc)
        if not isinstance(episodes, list):
            episodes = []
        if episodes and len(episodes) == previous:
            return episodes
        previous = len(episodes)
        if _clock() >= deadline:
            return episodes
        _sleep(1)


def _aired(episode: dict, now: datetime) -> bool:
    stamp = episode.get("airDateUtc")
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        return datetime.fromisoformat(stamp) <= now
    except ValueError:
        return False


def _lookup_one(tool: arr.Arr, tvdb_id: str) -> dict | None:
    for row in tool.lookup(f"tvdb:{tvdb_id}", limit=1):
        if str(row.get("tvdbId")) == str(tvdb_id):
            return row
    return None


def cancel(backend_id: str) -> bool:
    return backend().delete(backend_id, preserve_downloaded=True)


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
