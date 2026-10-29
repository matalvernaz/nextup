"""The podcast medium: what the library holds, and what to add next.

Podcasts are added through Jellyfin, never asked for here (Matt, 2026-10-27:
"podcasts shouldn't go through nextup at all, they're a jellyfin feature").
What stays is recommending them: the owned shelf, and suggestions of shows the
library does not hold yet, which a listener adds in EchoFin.

Three ways to identify one podcast, tried in order everywhere a match is made:
the feed address (as a key, a digest of it), Apple's catalogue id, and finally
the title reduced to its letters. The first two are exact and the Jellyfin
fork writes both onto a podcast it identified; the title is for a podcast that
arrived before it was identified, or that no catalogue lists.
"""
import threading
import time
from typing import NamedTuple

from . import config, itunes, jellyfin, logs, recommendations, store

log = logs.get("podcasts")

#: The medium's name everywhere: the recommendation block, the shelves, the
#: library index.
MEDIUM = "podcast"

#: How long the list of owned podcasts is kept. The same lifetime as the film
#: and series index, for the same reason: arrival can only be as fresh as this.
OWNED_TTL_SECONDS = 900


class OwnedPodcast(NamedTuple):
    """One podcast the library holds, by every name it can be matched on."""

    id: str
    name: str
    feed_key: str
    itunes_id: str
    normalised: str


# --- what the library holds -------------------------------------------------

_owned: list[OwnedPodcast] | None = None
_owned_at = 0.0
_owned_guard = threading.Lock()


def owned(force: bool = False) -> list[OwnedPodcast]:
    """Every podcast in the podcast libraries, matchable three ways.

    Raises `jellyfin.JellyfinUnavailable` only when there is nothing older to
    fall back on: a quarter-hour-old list is a far better answer than an
    empty one, which would read every podcast as askable and every request as
    not yet arrived.
    """
    global _owned, _owned_at
    with _owned_guard:
        fresh = (_owned is not None and not force
                 and time.monotonic() - _owned_at <= OWNED_TTL_SECONDS)
        if fresh:
            return _owned
    try:
        rows = jellyfin.podcasts_owned()
    except jellyfin.JellyfinUnavailable:
        with _owned_guard:
            previous = _owned
        if previous is None:
            raise
        log.error("keeping the previous podcast index: Jellyfin could not be asked")
        return previous
    built = []
    for row in rows:
        provider_ids = row.get("ProviderIds") or {}
        feed = str(provider_ids.get("PodcastFeed") or "").strip()
        built.append(OwnedPodcast(
            id=str(row.get("Id") or ""),
            name=str(row.get("Name") or ""),
            feed_key=itunes.item_key(feed) if feed else "",
            itunes_id=str(provider_ids.get("iTunes") or "").strip(),
            normalised=itunes.normalise_title(row.get("Name"))))
    with _owned_guard:
        _owned = built
        _owned_at = time.monotonic()
    log.info("owned index podcasts=%d", len(built))
    return built


def forget() -> None:
    """Drop the owned index, after a connection setting changed."""
    global _owned, _owned_at
    with _owned_guard:
        _owned = None
        _owned_at = 0.0


def match(candidates: list[OwnedPodcast], feed_key: str = "",
          itunes_id: str = "", title: str = "") -> OwnedPodcast | None:
    """The owned podcast one of these names refers to, exact ids before the title."""
    if feed_key:
        for found in candidates:
            if found.feed_key and found.feed_key == feed_key:
                return found
    if itunes_id:
        for found in candidates:
            if found.itunes_id and found.itunes_id == itunes_id:
                return found
    wanted = itunes.normalise_title(title)
    if wanted:
        for found in candidates:
            if found.normalised == wanted:
                return found
    return None


def match_hit(candidates: list[OwnedPodcast], hit: dict) -> OwnedPodcast | None:
    return match(candidates, hit.get("itemKey") or "", hit.get("itunesId") or "",
                 hit.get("title") or "")


def mark(hits: list[dict], user: jellyfin.User | None) -> list[dict]:
    """The owned flag on a list of hits.

    Nothing is read about asking: a podcast is added through Jellyfin, and one
    that has been is in the library, which is what this flag says.
    """
    try:
        candidates = owned()
    except jellyfin.JellyfinUnavailable:
        candidates = []
    out = []
    for hit in hits:
        found = match_hit(candidates, hit)
        out.append({**hit, "owned": found is not None})
    return out


# --- what to add ----------------------------------------------------------------


def _cache_key(user: jellyfin.User) -> str:
    return f"podcasts:catalogue:{user.key}"


def catalogue_shelf(user: jellyfin.User, force: bool = False) -> list[dict]:
    """Podcasts the library does not hold, near what this account listens to.

    Built from the same read the owned shelf is built from: every podcast in
    the libraries with this account's play state, weighted the way the film
    and series ranker weights a seed, into a profile of genres and makers.
    Apple's catalogue is then searched by the top genres. Nothing to go on --
    an account that has not listened to a podcast here -- is an empty shelf,
    said plainly rather than filled with a chart of what everybody else likes.
    """
    key = _cache_key(user)
    if not force:
        cached = store.get_external(key, config.PODCAST_SUGGESTIONS_TTL_HOURS)
        if cached is not None:
            return mark(cached, user)
    libraries = jellyfin.library_ids(MEDIUM)
    if not libraries:
        return []
    library = jellyfin.recommendation_items_for_user(MEDIUM, user.id, libraries)
    genres: dict[str, float] = {}
    studios: dict[str, float] = {}
    for item in library:
        weight = recommendations.seed_weight(item)
        if weight <= 0:
            continue
        for genre in item.get("Genres") or []:
            genres[genre] = genres.get(genre, 0.0) + weight
        for studio in item.get("Studios") or []:
            name = str(studio.get("Name") or "").strip()
            if name:
                studios[name] = studios.get(name, 0.0) + weight
    ranked_genres = [g for g, _ in sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))]
    ranked_studios = [s for s, _ in sorted(studios.items(), key=lambda kv: (-kv[1], kv[0]))]
    try:
        candidates = owned()
    except jellyfin.JellyfinUnavailable:
        candidates = []
    exclude_keys = {c.feed_key for c in candidates if c.feed_key}
    rows = itunes.suggestions(ranked_genres, ranked_studios, exclude_keys,
                              {c.name for c in candidates},
                              config.PODCAST_RECOMMENDATION_LIMIT)
    store.put_external(key, rows)
    log.info("catalogue shelf user=%s seeds=%d genres=%s rows=%d", user.key,
             sum(1 for item in library if recommendations.seed_weight(item) > 0),
             ranked_genres[:3], len(rows))
    return mark(rows, user)
