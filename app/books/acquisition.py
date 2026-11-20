"""Optional acquisition explanations, without slowing library or request reads."""
import json
import threading
import time

import httpx

from .. import config, listenarr, logs

log = logs.get("books.acquisition")

# One safe snapshot serves every account. Network work never holds the cache lock.
_cache: tuple[str, float, dict[str, str]] | None = None
_guard = threading.Lock()
_refreshing: threading.Thread | None = None
_CACHE_SECONDS = 60
_FAILURE_SECONDS = 15
_BUDGET_SECONDS = 5
_MAX_BYTES = 16 * 1024 * 1024
_TIMEOUT = httpx.Timeout(1, connect=1)


def enrich(rows: list[dict], *, key: str = "asin") -> list[dict]:
    """Opt in only where a response actually displays acquisition explanations."""
    pending = {row[key] for row in rows
               if row.get("medium", "book") == "book" and row.get(key)
               and row.get("state") in ("on_its_way", "still_looking")}
    explanations = details(pending)
    return [{**row, "detail": explanations[row[key]]}
            if row.get(key) in explanations
            and row.get("medium", "book") == "book"
            and row.get("state") in ("on_its_way", "still_looking")
            else row for row in rows]


def details(asins: set[str]) -> dict[str, str]:
    """Return cached details immediately, even on a cold cache or during an outage."""
    if not asins or not listenarr.configured():
        return {}
    warm()
    with _guard:
        snapshot = _cache[2] if _cache and _cache[0] == config.LISTENARR_URL else {}
        return {asin: snapshot[asin] for asin in asins if asin in snapshot}


def warm() -> threading.Thread | None:
    """Start at most one refresh; callers never wait for Listenarr to answer."""
    global _refreshing
    if not listenarr.configured():
        return None
    address = config.LISTENARR_URL
    with _guard:
        if _refreshing is not None:
            return _refreshing
        if _cache and _cache[0] == address and time.monotonic() < _cache[1]:
            return None
        worker = threading.Thread(target=_refresh, args=(address,),
                                  name="book-acquisition-details", daemon=True)
        _refreshing = worker
        worker.start()
        return worker


def _client(address: str) -> httpx.Client:
    return httpx.Client(base_url=address, timeout=_TIMEOUT)


def _read(client: httpx.Client, path: str, deadline: float):
    # A read timeout only bounds inactivity. Check the shared deadline between raw
    # chunks too, so a backend trickling bytes cannot occupy this worker forever.
    body = bytearray()
    with client.stream("GET", path, headers={"Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        for chunk in response.iter_raw():
            if time.monotonic() >= deadline or len(body) + len(chunk) > _MAX_BYTES:
                raise ValueError("acquisition snapshot exceeded its read budget")
            body.extend(chunk)
    if time.monotonic() >= deadline:
        raise ValueError("acquisition snapshot exceeded its read budget")
    return json.loads(body)


def _refresh(address: str) -> None:
    global _cache, _refreshing
    snapshot = {}
    lifetime = _FAILURE_SECONDS
    try:
        deadline = time.monotonic() + _BUDGET_SECONDS
        with _client(address) as client:
            books = _read(client, "/api/v1/library", deadline)
            downloads = _read(client, "/api/v1/downloads", deadline)
        if not isinstance(books, list) or not isinstance(downloads, list):
            raise ValueError("unreadable acquisition snapshot")
        snapshot = _describe(books, downloads)
        lifetime = _CACHE_SECONDS
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("acquisition details unavailable (%s); request states are unchanged", type(exc).__name__)
    finally:
        with _guard:
            if config.LISTENARR_URL == address:
                _cache = (address, time.monotonic() + lifetime, snapshot)
            _refreshing = None


def _describe(books: list, downloads: list) -> dict[str, str]:
    """Keep safe sentences, not backend error text, paths or download credentials."""
    latest = {}
    for download in sorted(
            (row for row in downloads if isinstance(row, dict)),
            key=lambda row: str(row.get("startedAt") or ""), reverse=True):
        identifier = download.get("audiobookId")
        if isinstance(identifier, int):
            latest.setdefault(identifier, download)
    described = {}
    for book in books:
        if not isinstance(book, dict) or not isinstance(book.get("asin"), str):
            continue
        asin = book["asin"]
        release = book.get("publishedDate")
        if isinstance(release, str) and listenarr.not_yet_published(release):
            if release.startswith("2200-"):
                described[asin] = "This book has been announced without a confirmed release date."
            else:
                described[asin] = f"Not released yet. Its release date is {release[:10]}."
            continue
        status = latest.get(book.get("id"), {}).get("status")
        if isinstance(book.get("fileCount"), int) and book["fileCount"] > 0:
            described[asin] = "Waiting for this book to appear in the library."
        elif status == "ImportBlocked":
            described[asin] = ("The download finished but could not be added to the library. "
                               "A keyholder needs to check this download.")
        elif status == "Failed":
            described[asin] = "The download failed. This book has not reached the library."
    return described
