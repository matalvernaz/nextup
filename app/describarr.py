"""Audio description, via describarr.

Sonarr and Radarr already hand describarr everything they download, so anything
newly acquired is described without anybody asking. What is missing is the
backlog: titles that were in the library before that wiring existed, or whose
description was not available at the time. This module is the way to ask for
one of those by hand.

It queues, it does not describe. describarr holds a persistent queue and works
through it one title at a time behind a single lock, so a request here is a
request, and the result turns up as a new audio track some time later.

Nothing is stored on this side. describarr's queue is the record of what was
asked for, and duplicating it here would be a second answer to the same
question that could disagree with the first.
"""
import httpx

from . import config, logs

log = logs.get("describarr")

#: describarr's own alignment work is measured in hours; this is only the time
#: to accept the job onto its queue.
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: The Jellyfin types that can be asked for, and how each is handed over.
#:
#: A film is one file, so describarr is given the file. A series and a season
#: are folders, and its directory retry walks a folder recursively and skips
#: what its own ledger says is finished -- which is exactly the backlog
#: semantics wanted here, at either level.
DESCRIBABLE_TYPES = ("Movie", "Series", "Season")


def configured() -> bool:
    return bool(config.DESCRIBARR_URL)


def _request_params(item: dict) -> dict[str, str]:
    """What describarr's `/retry` needs for this item.

    `title` is passed explicitly rather than left to be inferred from the path.
    describarr can infer one from a Sonarr- or Radarr-shaped layout, but it
    answers 400 when it cannot, and Jellyfin already knows the name -- so a
    folder named in some other way is a title this service simply supplies
    instead of a request that fails.
    """
    kind = item.get("Type", "")
    path = (item.get("Path") or "").strip()
    if kind == "Movie":
        params = {"path": path, "title": item.get("Name", "")}
        year = item.get("ProductionYear")
        if year:
            params["year"] = str(year)
        return params
    # A season names its series, not itself: "Season 1" is not a title, and it
    # is the series name describarr searches its sources for.
    title = item.get("SeriesName") if kind == "Season" else item.get("Name")
    return {"dir": path, "title": title or item.get("Name", "")}


#: The types whose outcome can be asked about: one file each. A series or a
#: season is a folder of them, and would have an answer per episode.
OUTCOME_TYPES = ("Movie", "Episode")

#: What describarr's /outcome may say. Anything else is passed on as unknown
#: rather than as a word a client was never written to read.
OUTCOME_STATES = ("working", "queued", "described", "already_described",
                  "no_match", "rejected", "error", "unknown")


def outcome(item: dict) -> dict:
    """What became of the last request for this file, in describarr's words.

    Asked of describarr rather than kept here, for the reason the module says:
    its queue and its record are the answer, and a second copy could disagree.
    Raises `httpx.HTTPError` when describarr cannot be reached. A describarr
    that predates /outcome answers 404, which is read as knowing nothing.
    """
    path = (item.get("Path") or "").strip()
    url = config.DESCRIBARR_URL.rstrip("/") + "/outcome"
    headers = {}
    if config.DESCRIBARR_API_KEY:
        headers["X-Api-Key"] = config.DESCRIBARR_API_KEY
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(url, params={"path": path}, headers=headers)
    if response.status_code == 404:
        return {"state": "unknown", "detail": "", "at": ""}
    response.raise_for_status()
    data = response.json()
    state = data.get("state")
    return {"state": state if state in OUTCOME_STATES else "unknown",
            "detail": str(data.get("detail") or ""),
            "at": str(data.get("at") or "")}


class DescribeRefused(Exception):
    """describarr took the request but would not queue it, with its reason."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def request(item: dict) -> str:
    """Queue one film, series or season for description. Returns what it said.

    Raises `DescribeRefused` when describarr answers anything but 202, and
    `httpx.HTTPError` when it cannot be reached at all. The caller turns those
    into something a listener hears -- they are different situations and a
    single "it did not work" would hide which.
    """
    params = _request_params(item)
    url = config.DESCRIBARR_URL.rstrip("/") + "/retry"
    headers = {}
    # Optional by describarr's own design: unset means it relies on the docker
    # network being private, which is the historical and current deployment.
    key = config.DESCRIBARR_API_KEY
    if key:
        headers["X-Api-Key"] = key
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(url, params=params, headers=headers)
    body = (response.text or "").strip()
    if response.status_code != 202:
        log.warning("describe refused status=%s params=%s detail=%s",
                    response.status_code, params, body[:200])
        detail = ("This title is already queued or is being described."
                  if response.status_code == 409 else
                  "The description service could not accept this title. Try again later.")
        raise DescribeRefused(response.status_code, detail)
    log.info("describe queued type=%s title=%s",
             item.get("Type"), params.get("title"))
    return body
