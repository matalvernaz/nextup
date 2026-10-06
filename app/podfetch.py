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

from . import arr, config, episodes, jellyfin, logs, podcastfeed

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


def organize_of(value, default: bool = True) -> bool:
    """Whether an ask wants its podcast sorted into folders.

    The ask arrives as JSON (a boolean), as a form ("yes" from a ticked box),
    or from a request kept since before the choice existed (nothing at all),
    which is sorted, as every new podcast is unless the asker says not.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "true", "1", "on")


def add(feed_url: str, title: str = "",
        choice: episodes.Episodes | None = None,
        organize: bool = True) -> arr.AddResult:
    """Place an order for a feed. The fork makes the folder and starts fetching.

    `organize` asks the fork to sort a podcast new to the library into
    folders by season and series as its episodes arrive. A fork from before
    that choice existed ignores the field, and one already here keeps its own
    folders whatever is asked.
    """
    if not configured():
        return arr.AddResult(False, "Podcasts are not available on this server.")
    feed_url = (feed_url or "").strip()
    if not feed_url:
        return arr.AddResult(False, "That podcast has no feed address, so it cannot be asked for.")
    # The address arrives from the client as it was typed, not only from a
    # preview this server made, so it is checked here as well as there: the
    # fork fetches whatever it is handed, with the server's own reach.
    problem = podcastfeed.public_address_problem(feed_url)
    if problem:
        log.info("add refused url=%s: %s", feed_url, problem)
        return arr.AddResult(False, problem)
    backfill = backfill_of(choice)
    body = {"feedUrl": feed_url, "backfill": backfill, "organize": bool(organize)}
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
    organized = bool(status.get("Organized", False))
    log.info("add url=%s id=%s created=%s backfill=%s organized=%s",
             feed_url, status["ItemId"], created, backfill or "-", organized)
    return arr.AddResult(
        True, _sentence(backfill, created, organized), str(status["ItemId"]),
        str(status.get("Name") or title), created=created, image_url="", overview="")


def _sentence(backfill: str, created: bool, organized: bool = False) -> str:
    if backfill == "all":
        fetched = "Every episode is being fetched, and new ones as they come out."
    elif backfill.startswith("latest:"):
        count = backfill.partition(":")[2]
        fetched = (f"The newest {count} episode{'' if count == '1' else 's'} "
                   f"{'is' if count == '1' else 'are'} being fetched, and new ones as they come out.")
    else:
        fetched = "New episodes will be fetched as they come out."
    lead = "Subscribed. " if created else "It was already in the library; it is followed from now on. "
    sorted_ = " Episodes are sorted into folders by season and series." if organized else ""
    return lead + fetched + sorted_


def folders(feed_url: str) -> tuple[dict | None, str, str]:
    """The folders a podcast would be sorted into if it were asked for now.

    Returns the preview, a sentence saying why there is none, and what kind of
    none it is: "" for a preview, "refused" for an address or feed that will
    not do, "unreachable" and "unsupported" (a fork from before folders). The
    address is checked as `add` checks it, before the fork is asked: the fork
    fetches whatever it is handed, with the server's own reach.
    """
    if not configured():
        return None, "Podcasts are not available on this server.", "unsupported"
    feed_url = (feed_url or "").strip()
    if not feed_url:
        return None, "That podcast has no feed address.", "refused"
    problem = podcastfeed.public_address_problem(feed_url)
    if problem:
        log.info("folders refused url=%s: %s", feed_url, problem)
        return None, problem, "refused"
    try:
        with jellyfin._client() as c:
            resp = c.get("/Podcasts/Organize", params={"feedUrl": feed_url})
    except httpx.HTTPError as exc:
        log.error("folders failed url=%s: Jellyfin unreachable (%s)", feed_url, exc)
        return None, "Jellyfin could not be reached.", "unreachable"
    if resp.status_code == 404:
        return None, "This server does not sort podcasts into folders yet.", "unsupported"
    if resp.status_code >= 400:
        detail = arr._detail(resp)[:180]
        log.info("folders rejected url=%s status=%d body=%s", feed_url, resp.status_code, detail)
        kind = "refused" if resp.status_code == 400 else "unreachable"
        return None, detail or f"Jellyfin could not preview it: {resp.status_code}", kind
    try:
        raw = resp.json()
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        return None, "Jellyfin's preview could not be read.", "unreachable"
    shown = []
    for folder in raw.get("Folders") or []:
        if not isinstance(folder, dict):
            continue
        shown.append({"folder": str(folder.get("Folder") or ""),
                      "episodes": int(folder.get("Episodes") or 0),
                      "first": str(folder.get("First") or ""),
                      "last": str(folder.get("Last") or ""),
                      "examples": [str(e) for e in folder.get("Examples") or []]})
    return {"name": str(raw.get("Name") or ""),
            "episodes": int(raw.get("Episodes") or 0),
            "skipped": int(raw.get("Skipped") or 0),
            "bySeason": bool(raw.get("BySeason")),
            "series": [str(s) for s in raw.get("Series") or []],
            "folders": shown}, "", ""


def folders_sentence(preview: dict) -> str:
    """The preview as one sentence a screen reader can say in one go."""
    parts = []
    for folder in preview.get("folders") or []:
        name = folder["folder"] or "the podcast's own folder"
        count = folder["episodes"]
        parts.append(f"{name}, {count} episode{'' if count == 1 else 's'}")
    if not parts:
        return "Nothing in the feed to sort."
    sentence = "; ".join(parts) + "."
    skipped = preview.get("skipped") or 0
    if skipped:
        sentence += f" {skipped} trailer{'' if skipped == 1 else 's'} or announcement{'' if skipped == 1 else 's'} left out."
    return sentence


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
