"""Podcasts, fetched by the Jellyfin fork itself.

The fork's podcasts library knows every podcast's feed, and a scheduled task
on the server downloads what each podcast's folder is missing, into that
folder, named the way the folder names its files. Nothing here downloads
anything: this service places the order and reads back how far it has got.

The fork answers `POST /Podcasts` to place an order (creating the folder and
the item when the library lacks them), `GET /Podcasts/{id}/Subscription` for
the standing and progress, and `POST /Podcasts/{id}/Subscription` to change
the order. A stock Jellyfin has none of these; the podcast medium is offered
only once the fork's fetch task has been seen in the server's task list.

How much of a podcast is fetched is the person's choice, carried with the
order as a *backfill*: `all` for the whole back catalogue, `latest:N` for the
newest few, and nothing for new episodes only. New episodes are fetched as
they come out either way, until the order is cancelled.
"""
import threading
import time

import httpx

from . import arr, config, episodes, jellyfin, logs

log = logs.get("podfetch")

MEDIUM = "podcast"
UNIT = "podcast"
UNITS = (UNIT,)

#: The key of the fork's fetch task in Jellyfin's task list. Its presence is
#: how this service tells the fork from a stock server.
FETCH_TASK_KEY = "FetchPodcastEpisodes"

#: How long a verdict on the fork's presence is reused. It changes at a
#: deploy, which is rare, and asking the task list on every capabilities call
#: would be a round trip per client start.
PRESENCE_TTL_SECONDS = 900

#: How long an *absent* verdict is kept. Shorter, so a Jellyfin that was
#: merely down when first asked is asked again soon.
ABSENCE_TTL_SECONDS = 60

_presence: tuple[bool, float] | None = None
_presence_guard = threading.Lock()


def configured() -> bool:
    """Whether podcasts can be ordered: Jellyfin is set up and it is the fork."""
    if not config.JELLYFIN_URL:
        return False
    return present()


def present(force: bool = False) -> bool:
    """Whether the server's task list carries the fork's fetch task."""
    global _presence
    with _presence_guard:
        if _presence is not None and not force:
            seen, at = _presence
            ttl = PRESENCE_TTL_SECONDS if seen else ABSENCE_TTL_SECONDS
            if time.monotonic() - at < ttl:
                return seen
    seen = _probe()
    with _presence_guard:
        _presence = (seen, time.monotonic())
    return seen


def forget() -> None:
    """Drop the verdict on the fork's presence, after a connection setting changed."""
    global _presence
    with _presence_guard:
        _presence = None


def _probe() -> bool:
    try:
        with jellyfin._client() as c:
            resp = c.get("/ScheduledTasks")
            resp.raise_for_status()
            tasks = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not read Jellyfin's task list (%s)", exc)
        return False
    return any(isinstance(task, dict) and task.get("Key") == FETCH_TASK_KEY
               for task in (tasks if isinstance(tasks, list) else []))


def backfill_of(choice: episodes.Episodes | None) -> str:
    """The fork's spelling of a choice of episodes."""
    if choice is None or choice.choice == episodes.NEW:
        return ""
    if choice.choice == episodes.ALL:
        return "all"
    return f"latest:{choice.count or episodes.DEFAULT_LATEST_COUNT}"


def add(feed_url: str, title: str = "",
        choice: episodes.Episodes | None = None) -> arr.AddResult:
    """Place an order for a feed. The fork makes the folder and starts fetching."""
    if not configured():
        return arr.AddResult(False, "Podcasts are not available on this server.")
    feed_url = (feed_url or "").strip()
    if not feed_url:
        return arr.AddResult(False, "That podcast has no feed address, so it cannot be asked for.")
    backfill = backfill_of(choice)
    body = {"feedUrl": feed_url, "backfill": backfill}
    if title:
        body["title"] = title
    try:
        with jellyfin._client() as c:
            resp = c.post("/Podcasts", json=body)
    except httpx.HTTPError as exc:
        log.error("add failed url=%s: Jellyfin unreachable (%s)", feed_url, exc)
        return arr.AddResult(False, "Jellyfin could not be reached.")
    if resp.status_code >= 400:
        detail = arr._detail(resp)[:180]
        log.error("add rejected url=%s status=%d body=%s", feed_url, resp.status_code, detail)
        if resp.status_code == 400 and detail:
            return arr.AddResult(False, detail)
        return arr.AddResult(False, f"Jellyfin refused it: {detail or resp.status_code}")
    try:
        status = resp.json()
    except ValueError:
        status = {}
    if not isinstance(status, dict) or not status.get("ItemId"):
        return arr.AddResult(False, "Jellyfin took the order but gave it no id.")
    created = bool(status.get("Created", True))
    log.info("add url=%s id=%s created=%s backfill=%s", feed_url, status["ItemId"], created, backfill or "-")
    return arr.AddResult(
        True, _sentence(backfill, created), str(status["ItemId"]),
        str(status.get("Name") or title), created=created, image_url="", overview="")


def _sentence(backfill: str, created: bool) -> str:
    if backfill == "all":
        fetched = "Every episode is being fetched, and new ones as they come out."
    elif backfill.startswith("latest:"):
        count = backfill.partition(":")[2]
        fetched = (f"The newest {count} episode{'' if count == '1' else 's'} "
                   f"{'is' if count == '1' else 'are'} being fetched, and new ones as they come out.")
    else:
        fetched = "New episodes will be fetched as they come out."
    lead = "Subscribed. " if created else "It was already in the library; it is followed from now on. "
    return lead + fetched


def state(backend_id: str) -> dict | None:
    """How far along one podcast is, or None when Jellyfin cannot say.

    None is "unknown", never "nothing yet": a caller that reads it as either
    has invented an answer.
    """
    if not configured() or not backend_id:
        return None
    try:
        with jellyfin._client() as c:
            resp = c.get(f"/Podcasts/{backend_id}/Subscription")
            if resp.status_code >= 400:
                return None
            status = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("state unreachable id=%s (%s)", backend_id, exc)
        return None
    if not isinstance(status, dict):
        return None
    on_disk = int(status.get("OnDisk") or 0)
    pending = int(status.get("Pending") or 0)
    return {
        "downloaded": on_disk,
        "downloading": 1 if status.get("Fetching") else 0,
        "pending": pending,
        "total": on_disk + pending,
        "in_feed": int(status.get("InFeed") or 0),
        "paused": not status.get("Subscribed", True),
        "title": str(status.get("Name") or ""),
        "url": str(status.get("FeedUrl") or ""),
        "error": str(status.get("LastError") or ""),
    }


def cancel(backend_id: str) -> bool:
    """Stop following a podcast. Episodes already fetched stay in the library."""
    if not configured() or not backend_id:
        return False
    try:
        with jellyfin._client() as c:
            resp = c.post(f"/Podcasts/{backend_id}/Subscription",
                          json={"subscribed": False, "backfill": "new"})
    except httpx.HTTPError as exc:
        log.warning("cancel unreachable id=%s (%s)", backend_id, exc)
        return False
    # Gone from the library is stopped too: the fetch task only visits podcasts
    # the library still holds.
    return resp.status_code < 400 or resp.status_code == 404
