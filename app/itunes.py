"""Apple's podcast catalogue, which needs no key.

Two questions are asked of it: what podcasts a title might mean, for the
search box, and what podcasts sit near the ones somebody already listens to,
for the Discover shelf. Both answer with the feed address, which is the only
identity a podcast really has -- the same feed is what podgrab subscribes to
and what the Jellyfin fork reads the podcast's own description from.

Never fatal. A catalogue that does not answer is a search that says so and a
shelf with one section fewer, never a failed page.
"""
import hashlib
import re
import unicodedata
from datetime import datetime, timezone

import httpx

from . import artwork, config, logs, store

log = logs.get("itunes")

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"

#: One search while somebody waits on a search box.
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

#: The genre Apple files every podcast under, which says nothing about it.
EVERY_PODCAST_GENRE = "Podcasts"

#: How many hits one catalogue search asks for.
SEARCH_LIMIT = 25

#: How many genres of somebody's own listening the shelf searches by, and how
#: many candidates each of those searches brings back. Three genres at fifty
#: is a hundred and fifty rows to score, which is a page's worth of choice
#: without turning one shelf build into a dozen requests.
SUGGESTION_GENRES = 3
SUGGESTION_SEARCH_LIMIT = 50

#: How long a catalogue answer is kept. Search results change slowly and a
#: shelf is rebuilt far more often than the catalogue moves.
CACHE_HOURS = 24

_NOT_WORD = re.compile(r"[^A-Z0-9]")


def normalise_title(title: str | None) -> str:
    """A title reduced to what two spellings of it share: letters and digits, no accents, no case.

    The same rule the Jellyfin fork applies, so a podcast the fork identified
    and a request made here agree on what "the same title" means: "SCP
    Archives", "SCPArchives" and "scp-archives" are one podcast.
    """
    if not title:
        return ""
    decomposed = unicodedata.normalize("NFD", title)
    kept = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return _NOT_WORD.sub("", kept.upper())


def item_key(feed_url: str) -> str:
    """The ledger key for a podcast: a digest of its feed address.

    The feed, not Apple's id, because a podcast added from a pasted address
    has no Apple id and the same show reached both ways must be one request.
    Digested so the key is short and safe in a form field, with the readable
    title kept beside it in the ledger.
    """
    return "feed:" + hashlib.sha256(canonical_feed(feed_url).encode()).hexdigest()[:16]


def canonical_feed(url: str) -> str:
    """One spelling of a feed address, so trailing slashes and case in the host do not make two keys."""
    url = (url or "").strip()
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}".rstrip("/")


def search(query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    """Podcasts whose titles match some words, as search hits.

    Cached by query for a day. Raises nothing: an unreachable catalogue is an
    empty list and a log line, and the caller says "nothing matched" -- which
    is true of what this service could find, if not of the world.
    """
    query = " ".join((query or "").split())
    if not query:
        return []
    key = f"itunes:search:{limit}:{query.casefold()}"
    cached = store.get_external(key, CACHE_HOURS)
    if cached is not None:
        return cached
    rows = _get(SEARCH_URL, {"media": "podcast", "entity": "podcast",
                             "limit": limit, "term": query,
                             "country": config.ITUNES_COUNTRY})
    if rows is None:
        return []
    hits = [hit for hit in (_hit(row) for row in rows) if hit]
    store.put_external(key, hits)
    return hits


def lookup(itunes_id: str) -> dict | None:
    """One podcast by Apple's id, as a search hit, or None."""
    itunes_id = str(itunes_id or "").strip()
    if not itunes_id.isdigit():
        return None
    key = f"itunes:lookup:{itunes_id}"
    cached = store.get_external(key, CACHE_HOURS)
    if cached is not None:
        return cached or None
    rows = _get(LOOKUP_URL, {"entity": "podcast", "id": itunes_id,
                             "country": config.ITUNES_COUNTRY})
    if rows is None:
        return None
    found = next((hit for hit in (_hit(row) for row in rows) if hit), None)
    store.put_external(key, found or {})
    return found


def _get(url: str, params: dict) -> list[dict] | None:
    """The `results` of one catalogue call, or None when it did not answer."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("apple catalogue failed %s %s (%s)", url, params.get("term") or params.get("id"), exc)
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    return results if isinstance(results, list) else []


def _hit(row: dict) -> dict | None:
    """One catalogue row in the shape every other medium's search hits take."""
    if not isinstance(row, dict):
        return None
    feed = artwork.https_url(row.get("feedUrl")) or (row.get("feedUrl") or "").strip()
    title = (row.get("collectionName") or row.get("trackName") or "").strip()
    if not feed or not title:
        return None
    genres = [g for g in (row.get("genres") or [])
              if isinstance(g, str) and g.strip()
              and g.strip().casefold() != EVERY_PODCAST_GENRE.casefold()]
    released = _date(row.get("releaseDate"))
    hit = {
        "itemKey": item_key(feed),
        "medium": "podcast",
        "unit": "podcast",
        "title": title,
        "year": str(released.year) if released else "",
        "artist": (row.get("artistName") or "").strip(),
        "feedUrl": feed,
        "itunesId": str(row.get("collectionId") or ""),
        "overview": "",
        **({"genres": genres} if genres else {}),
        **artwork.art(row.get("artworkUrl600") or row.get("artworkUrl100")),
    }
    count = row.get("trackCount")
    if isinstance(count, int) and count > 0:
        hit["episodeCount"] = count
    if released:
        hit["latestEpisodeAt"] = released.isoformat()
    return hit


def _date(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def suggestions(genres: list[str], studios: list[str],
                exclude_keys: set[str], exclude_titles: set[str],
                limit: int) -> list[dict]:
    """Podcasts near somebody's listening that the library does not hold.

    `genres` and `studios` are what the listener's own podcasts carry, best
    first. Each of the top genres is searched by name -- Apple's search reads
    a genre word well enough to fill a page with that kind of show -- and
    every candidate is scored on how many of the listener's genres it shares,
    with a bonus for the same maker. Owned podcasts and ones already asked for
    are left out by feed key, and by title as well for a podcast that arrived
    without its feed being known.
    """
    wanted = [g for g in genres if g and g.casefold() != EVERY_PODCAST_GENRE.casefold()]
    if not wanted:
        return []
    profile = {g.casefold(): len(wanted) - position for position, g in enumerate(wanted)}
    makers = {s.casefold() for s in studios if s}
    excluded_titles = {normalise_title(t) for t in exclude_titles}
    seen: dict[str, dict] = {}
    for genre in wanted[:SUGGESTION_GENRES]:
        for hit in search(genre, SUGGESTION_SEARCH_LIMIT):
            key = hit["itemKey"]
            if key in exclude_keys or normalise_title(hit["title"]) in excluded_titles:
                continue
            shared = [g for g in hit.get("genres", []) if g.casefold() in profile]
            if not shared:
                continue
            score = sum(profile[g.casefold()] for g in shared)
            reasons = [_genre_reason(shared[:2])]
            if hit.get("artist", "").casefold() in makers:
                score += len(wanted)
                reasons.insert(0, f"from {hit['artist']}, whose podcasts you listen to")
            row = seen.get(key)
            if row is None or score > row["score"]:
                seen[key] = {**hit, "score": round(score, 2), "reason": reasons}
    ranked = sorted(seen.values(), key=lambda row: (-row["score"], row["title"].casefold()))
    return ranked[:limit]


def _genre_reason(shared: list[str]) -> str:
    noun = "genre" if len(shared) == 1 else "genres"
    joined = shared[0] if len(shared) == 1 else f"{shared[0]} and {shared[1]}"
    return f"shares the {joined} {noun} with podcasts you listen to"
