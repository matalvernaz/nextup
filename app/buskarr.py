"""Music, via buskarr.

The odd one out, and deliberately. Films and series decide arrival from
Jellyfin's provider ids, because Radarr and Sonarr know only what they
themselves fetched. Music decides it from buskarr, because buskarr *placed the
file* and holds the exact `(artist, title, duration)` identity it placed it
under -- where the only route back from a track to a Jellyfin item is matching
text, which is the near-miss this service otherwise avoids everywhere.

Music also has three units where the others have one. An artist is a
discography, an album a release, a track one song, and they cost very
different amounts of somebody's line.
"""
import hashlib

import httpx

from . import arr, config, logs

log = logs.get("buskarr")

MEDIUM = "music"
UNITS = ("artist", "album", "track")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: What each unit spends out of the daily music allowance.
COSTS = {
    "artist": config.MUSIC_ARTIST_COST,
    "album": config.MUSIC_ALBUM_COST,
    "track": config.MUSIC_TRACK_COST,
}


def configured() -> bool:
    return bool(config.BUSKARR_URL and config.BUSKARR_API_KEY)


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=config.BUSKARR_URL.rstrip("/") + "/api/v1",
        headers={"X-Api-Key": config.BUSKARR_API_KEY,
                 "Accept": "application/json"},
        timeout=_TIMEOUT)


def item_key(unit: str, source: str = "", ref: str = "",
             artist: str = "", title: str = "") -> str:
    """The ledger key for one piece of music.

    An artist and an album are identified by the catalogue that listed them.
    A track has no such id -- buskarr's whole model is that identity is the
    credit and the title, not an MBID -- so it is keyed on a digest of those,
    which makes a second request for the same song the same request however it
    was found. The readable title is kept in the ledger beside it.
    """
    if unit == "track":
        seed = f"{artist.casefold().strip()}\n{title.casefold().strip()}"
        return f"bk:track:{hashlib.sha256(seed.encode()).hexdigest()[:16]}"
    return f"bk:{unit}:{source}:{ref}"


def search(query: str, unit: str, limit: int) -> list[dict]:
    """Catalogue hits for one unit."""
    if not configured():
        return []
    try:
        with _client() as c:
            resp = c.get("/search", params={"q": query, "unit": unit,
                                            "limit": limit})
    except httpx.HTTPError as exc:
        log.warning("search unreachable q=%r unit=%s (%s)", query, unit, exc)
        return []
    if resp.status_code >= 400:
        log.warning("search refused q=%r unit=%s status=%d",
                    query, unit, resp.status_code)
        return []
    try:
        rows = resp.json().get("results", [])
    except ValueError:
        return []
    return [_result(row, unit) for row in rows]


def _result(row: dict, unit: str) -> dict:
    """One buskarr hit, in this service's shape.

    `owned` is absent rather than false for music. buskarr answers "do I have
    this" only for a track it has already been asked about, and a search hit
    has not been; claiming false would say the library lacks something it may
    well hold.
    """
    if unit == "artist":
        name = row.get("name") or ""
        return {
            "itemKey": item_key("artist", row.get("source", ""), row.get("ref", "")),
            "medium": MEDIUM, "unit": "artist",
            "title": name, "artist": name,
            "overview": row.get("hint") or "",
            "releases": row.get("releases"),
            "source": row.get("source"), "ref": row.get("ref"),
        }
    if unit == "album":
        return {
            "itemKey": item_key("album", row.get("source", ""), row.get("ref", "")),
            "medium": MEDIUM, "unit": "album",
            "title": row.get("title") or "", "artist": row.get("artist") or "",
            "overview": "", "trackCount": row.get("tracks"),
            "source": row.get("source"), "ref": row.get("ref"),
        }
    return {
        "itemKey": item_key("track", artist=row.get("artist", ""),
                            title=row.get("title", "")),
        "medium": MEDIUM, "unit": "track",
        "title": row.get("title") or "", "artist": row.get("artist") or "",
        "album": row.get("album") or "", "overview": "",
        "durationSeconds": row.get("duration"),
        "source": (row.get("sources") or [None])[0],
    }


def add(unit: str, hit: dict, requested_by: str) -> arr.AddResult:
    """Ask buskarr for one artist, album or track.

    `hit` is the search result being asked for. buskarr needs the catalogue
    ref for a bulk unit and the credit and title for a track, and passing the
    row back rather than re-deriving it is what keeps the two ends agreeing
    about which of several same-named artists was meant.
    """
    if not configured():
        return arr.AddResult(False, "Music is not available on this server.")
    body = {
        "unit": unit,
        "ref": str(hit.get("ref") or ""),
        "source": hit.get("source") or "deezer",
        "artist": hit.get("artist") or "",
        "title": hit.get("title") or "",
        "album": hit.get("album") or "",
        "requestedBy": requested_by,
    }
    try:
        with _client() as c:
            resp = c.post("/add", json=body)
    except httpx.HTTPError as exc:
        log.error("add failed unit=%s: buskarr unreachable (%s)", unit, exc)
        return arr.AddResult(False, "buskarr could not be reached.")
    if resp.status_code >= 400:
        detail = resp.text[:180]
        log.error("add rejected unit=%s status=%d body=%s",
                  unit, resp.status_code, detail)
        return arr.AddResult(False, f"buskarr refused it: {detail}")
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    log.info("add unit=%s ref=%s reference=%s", unit, body["ref"],
             payload.get("reference"))
    return arr.AddResult(True, payload.get("message") or "Sent to buskarr.",
                         payload.get("reference") or "",
                         body["title"] or body["artist"])


def state(backend_id: str) -> dict | None:
    """How far along one music request is, or None when buskarr cannot say.

    None is not "not arrived": it means unknown, and a caller that treats it
    as either has invented an answer. The request keeps its last state.
    """
    if not configured() or not backend_id:
        return None
    try:
        with _client() as c:
            resp = c.get("/state", params={"reference": backend_id})
    except httpx.HTTPError as exc:
        log.warning("state unreachable reference=%s (%s)", backend_id, exc)
        return None
    if resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def cancel(backend_id: str) -> bool:
    """Stop looking. Tracks already downloaded stay in the library."""
    if not configured() or not backend_id:
        return False
    try:
        with _client() as c:
            resp = c.post("/cancel", json={"reference": backend_id})
    except httpx.HTTPError as exc:
        log.warning("cancel unreachable reference=%s (%s)", backend_id, exc)
        return False
    return resp.status_code < 400
