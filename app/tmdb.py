"""TMDb's own recommendation graph, for the film and series shelves.

**What this adds, and what it deliberately does not.** TMDb's `vote_average`
is already in play: Jellyfin's TMDb plugin writes it onto every item as
`CommunityRating`, and `recommendations._score_candidates` already uses it as a
quality tiebreak. Fetching it again here would be a second copy of a number the
library already holds. What the library does *not* hold is TMDb's
**"people who liked this also liked"** graph, which is the film and television
equivalent of the Audible similar-products list the book engine has always had
-- and the one signal a Jellyfin-only ranker cannot derive, because it is about
other people's viewing rather than this library's metadata.

**Dormant without a key.** `TMDB_API_KEY` is free but has to be registered, so
an installation without one gets exactly the shelf it got before: every entry
point here answers empty rather than raising, and the ranker adds no votes.

**Never fatal.** A shelf build that cannot reach TMDb is a shelf with one fewer
signal, not a failed run -- the same rule `books.audible` follows.
"""
import httpx

from . import config, logs, store

log = logs.get("tmdb")

_BASE = "https://api.themoviedb.org/3"

#: Same shape as the Audible client's. A catalogue that takes twenty seconds to
#: answer is one a shelf build should stop waiting for.
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

#: TMDb's path segment per medium. Their word for a television series is "tv".
_PATH = {"movie": "movie", "series": "tv"}

#: How many neighbours one seed contributes. TMDb answers 20 per page and the
#: tail of that list is weak evidence -- the vote is already rank-weighted, so
#: the cut is about payload and cache size rather than about correctness.
NEIGHBOURS_PER_SEED = 10


def configured() -> bool:
    return bool(config.TMDB_API_KEY)


def _cache_key(medium: str, tmdb_id: str) -> str:
    return f"tmdb:rec:{medium}:{tmdb_id}"


def recommendations(tmdb_id: str, medium: str) -> list[str]:
    """TMDb ids recommended alongside one title, best first.

    Empty on anything at all going wrong, including no key and an unknown
    medium. Cached even when empty: "TMDb has no neighbours for this" is an
    answer, and asking again on every page load is what the cache exists to
    stop.
    """
    path = _PATH.get(medium)
    if not path or not tmdb_id or not configured():
        return []

    key = _cache_key(medium, tmdb_id)
    cached = store.get_external(key, config.TMDB_TTL_HOURS)
    if cached is not None:
        return cached

    found: list[str] = []
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                f"{_BASE}/{path}/{tmdb_id}/recommendations",
                params={"api_key": config.TMDB_API_KEY, "page": 1})
            resp.raise_for_status()
            results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        # Not cached. A network failure is not an answer, and remembering it
        # for the TTL would keep the signal off long after the cause was gone.
        log.warning("tmdb recommendations failed %s/%s (%s)",
                    path, tmdb_id, exc)
        return []

    for row in results[:NEIGHBOURS_PER_SEED]:
        neighbour = row.get("id")
        if neighbour is not None:
            found.append(str(neighbour))

    store.put_external(key, found)
    return found
