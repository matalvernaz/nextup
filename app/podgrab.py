"""Podcasts, via podgrab.

podgrab subscribes to a feed, checks it on a schedule and downloads episodes
into a folder per podcast under the library path Jellyfin reads. Nothing here
treats it as a catalogue of what is owned: a podcast that was downloaded by
hand or by something else is in Jellyfin and not in podgrab, and ownership
questions go to Jellyfin.

podgrab has no key. It is reached over the Docker network and nowhere else,
the same way the arrs' admin keys are kept off the phone.

How much of a podcast is fetched is this service's decision, not podgrab's:
podgrab is configured to download nothing on its own when a podcast is added
(`downloadOnAdd` off, `initialDownloadCount` 0), and `add` then asks for the
whole back catalogue, the newest few, or nothing yet, according to the choice
the person made. New episodes are fetched by podgrab's own sweep either way.
"""
import os

import httpx

from . import arr, config, episodes, itunes, logs

log = logs.get("podgrab")

MEDIUM = "podcast"
UNIT = "podcast"
UNITS = (UNIT,)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: podgrab's own numbering of an episode's download state.
NOT_DOWNLOADED = 0
DOWNLOADING = 1
DOWNLOADED = 2
DELETED = 3


def configured() -> bool:
    return bool(config.PODGRAB_URL)


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.PODGRAB_URL.rstrip("/"),
                        headers={"Accept": "application/json"},
                        timeout=_TIMEOUT)


def _field(row: dict, *names: str, default=None):
    """One field of a podgrab row by any of its spellings.

    podgrab's structs carry JSON tags on some fields and not others, so one
    answer mixes `id` with `Title` with `download_status`. Asked for by every
    spelling rather than guessed at once.
    """
    for name in names:
        if name in row:
            return row[name]
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return default


def row_id(row: dict) -> str:
    return str(_field(row, "ID", "id", "Id", default="") or "")


def row_title(row: dict) -> str:
    return str(_field(row, "Title", "title", default="") or "")


def row_url(row: dict) -> str:
    return str(_field(row, "URL", "url", "Url", default="") or "")


def podcasts() -> list[dict] | None:
    """Every podcast podgrab holds, or None when it did not answer."""
    if not configured():
        return None
    try:
        with _client() as c:
            resp = c.get("/podcasts")
            resp.raise_for_status()
            rows = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("podgrab could not list podcasts (%s)", exc)
        return None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def find(feed_url: str) -> dict | None:
    """podgrab's row for a feed, or None when it has no such podcast (or did not answer)."""
    wanted = itunes.canonical_feed(feed_url)
    for row in podcasts() or []:
        if itunes.canonical_feed(row_url(row)) == wanted:
            return row
    return None


def add(feed_url: str, title: str = "",
        choice: episodes.Episodes | None = None) -> arr.AddResult:
    """Subscribe podgrab to a feed, then fetch as much of it as was asked for.

    Subscribing and downloading are two steps on purpose. A podcast with a
    twelve-year back catalogue is hundreds of files, and whether somebody wants
    them or only what comes out from now on is theirs to say; podgrab's own
    add would decide that for the whole household with one setting.
    """
    if not configured():
        return arr.AddResult(False, "Podcasts are not available on this server.")
    feed_url = (feed_url or "").strip()
    if not feed_url:
        return arr.AddResult(False, "That podcast has no feed address, so it cannot be asked for.")

    created = True
    try:
        with _client() as c:
            resp = c.post("/podcasts", json={"url": feed_url})
            if resp.status_code == 409:
                created = False
                row = find(feed_url)
                if row is None:
                    return arr.AddResult(
                        False, "podgrab says it already has this podcast, but "
                               "could not say which one.")
            elif resp.status_code >= 400:
                detail = arr._detail(resp)[:180]
                log.error("add rejected url=%s status=%d body=%s",
                          feed_url, resp.status_code, detail)
                return arr.AddResult(False, f"podgrab refused it: {detail}")
            else:
                try:
                    row = resp.json()
                except ValueError:
                    row = {}
                if not isinstance(row, dict) or not row_id(row):
                    # Added, but the answer did not carry the row; look it up.
                    row = find(feed_url) or {}
    except httpx.HTTPError as exc:
        log.error("add failed url=%s: podgrab unreachable (%s)", feed_url, exc)
        return arr.AddResult(False, "podgrab could not be reached.")

    backend_id = row_id(row)
    if not backend_id:
        return arr.AddResult(False, "podgrab added the podcast but gave it no id.")

    fetched = _fetch(backend_id, choice) if created else "It was already subscribed to."
    log.info("add url=%s id=%s created=%s choice=%s", feed_url, backend_id,
             created, choice.encode() if choice else "-")
    return arr.AddResult(
        True,
        ("Subscribed. " + fetched).strip(),
        backend_id,
        row_title(row) or title,
        created=created,
        image_url=str(_field(row, "Image", "image", default="") or ""),
        overview=_summary(row))


def _summary(row: dict) -> str:
    from . import podcastfeed
    return podcastfeed.strip_html(str(_field(row, "Summary", "summary", default="") or ""))


def _fetch(backend_id: str, choice: episodes.Episodes | None) -> str:
    """Start the downloads the choice calls for. Returns a sentence about it."""
    if choice is None or choice.choice == episodes.NEW:
        return "New episodes will be fetched as they come out."
    try:
        with _client() as c:
            if choice.choice == episodes.ALL:
                c.get(f"/podcasts/{backend_id}/download").raise_for_status()
                return "Every episode is being fetched."
            wanted = choice.count or episodes.DEFAULT_LATEST_COUNT
            resp = c.get(f"/podcasts/{backend_id}/items")
            resp.raise_for_status()
            items = [item for item in resp.json() if isinstance(item, dict)]
            items.sort(key=lambda item: str(_field(item, "PubDate", "pub_date", default="")),
                       reverse=True)
            started = 0
            for item in items[:wanted]:
                item_id = row_id(item)
                if item_id:
                    c.get(f"/podcastitems/{item_id}/download").raise_for_status()
                    started += 1
            return (f"The newest {started} episode{'' if started == 1 else 's'} "
                    f"{'is' if started == 1 else 'are'} being fetched, and new "
                    "ones as they come out.")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not start downloads id=%s choice=%s (%s)",
                    backend_id, choice.encode(), exc)
        return ("podgrab took the subscription but the first downloads could "
                "not be started; new episodes will still be fetched.")


def state(backend_id: str) -> dict | None:
    """How far along one podcast is, or None when podgrab cannot say.

    Counted from its episodes rather than read off the podcast row: podgrab
    fills the counts on that row only when listing every podcast, and answers
    zero for one asked about alone (measured 2026-09-30, 309 downloaded
    episodes reported as 0).

    None is "unknown", never "nothing yet": a caller that reads it as either
    has invented an answer.
    """
    if not configured() or not backend_id:
        return None
    try:
        with _client() as c:
            resp = c.get(f"/podcasts/{backend_id}")
            if resp.status_code >= 400:
                return None
            row = resp.json()
            items_resp = c.get(f"/podcasts/{backend_id}/items")
            items_resp.raise_for_status()
            items = items_resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("state unreachable id=%s (%s)", backend_id, exc)
        return None
    if not isinstance(row, dict):
        return None
    statuses = [int(_field(item, "DownloadStatus", "download_status", default=0) or 0)
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, dict)]
    return {
        "downloaded": sum(1 for status in statuses if status == DOWNLOADED),
        "downloading": sum(1 for status in statuses if status == DOWNLOADING),
        "total": len(statuses),
        "paused": bool(_field(row, "IsPaused", "is_paused", default=False)),
        "title": row_title(row),
        "url": row_url(row),
    }


def folder(backend_id: str) -> str | None:
    """The folder (as podgrab sees it) one podcast's episodes land in, or None until one has."""
    if not configured() or not backend_id:
        return None
    try:
        with _client() as c:
            resp = c.get(f"/podcasts/{backend_id}/items")
            resp.raise_for_status()
            items = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not list episodes id=%s (%s)", backend_id, exc)
        return None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        path = str(_field(item, "DownloadPath", "download_path", default="") or "")
        if path and int(_field(item, "DownloadStatus", "download_status", default=0) or 0) == DOWNLOADED:
            return os.path.dirname(path)
    return None


def cancel(backend_id: str) -> bool:
    """Stop following a podcast. Episodes already downloaded stay in the library."""
    if not configured() or not backend_id:
        return False
    try:
        with _client() as c:
            resp = c.delete(f"/podcasts/{backend_id}/podcast")
    except httpx.HTTPError as exc:
        log.warning("cancel unreachable id=%s (%s)", backend_id, exc)
        return False
    return resp.status_code < 400


def remove(backend_id: str) -> bool:
    """Drop a podcast podgrab holds, files and all. For a podcast already deleted from the library."""
    if not configured() or not backend_id:
        return False
    try:
        with _client() as c:
            resp = c.delete(f"/podcasts/{backend_id}")
    except httpx.HTTPError as exc:
        log.warning("remove unreachable id=%s (%s)", backend_id, exc)
        return False
    return resp.status_code < 400


#: How many of the newest-released downloaded episodes one sweep reads. New
#: downloads are almost always recent releases; a back catalogue arriving in
#: bulk is nudged by the request that asked for it instead.
RECENT_ITEMS = 200


def folders_downloaded_since(since_iso: str) -> set[str]:
    """Podcast folders (as podgrab sees them) with an episode downloaded after a moment.

    `since_iso` compares as text against podgrab's own timestamps, which are
    ISO 8601 in UTC; both sides come from podgrab, so no parsing is needed.
    """
    if not configured():
        return set()
    try:
        with _client() as c:
            resp = c.get("/podcastitems", params={
                "isDownloaded": "true", "sorting": "release_desc",
                "count": RECENT_ITEMS, "page": 1})
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not list downloaded episodes (%s)", exc)
        return set()
    items = payload.get("podcastItems") if isinstance(payload, dict) else payload
    folders: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        when = str(_field(item, "DownloadDate", "download_date", default="") or "")
        path = str(_field(item, "DownloadPath", "download_path", default="") or "")
        if path and when and when >= since_iso:
            folders.add(os.path.dirname(path))
    return folders


def library_path(podgrab_path: str) -> str:
    """A path podgrab wrote, as the Jellyfin container sees it."""
    inside = config.PODGRAB_ASSETS_PATH.rstrip("/") or "/"
    outside = config.PODCAST_LIBRARY_PATH.rstrip("/")
    if podgrab_path == inside:
        return outside
    if podgrab_path.startswith(inside + "/"):
        return outside + podgrab_path[len(inside):]
    return podgrab_path
