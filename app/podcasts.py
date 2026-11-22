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

from . import config, itunes, jellyfin, logs, recommendations, store, taste

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
            rows, built_from = _unpack(cached)
            # Young, but older than a mark made since: served as it stands
            # against the marks while a rebuild that reads them runs behind.
            if built_from < taste.revision(user.key, MEDIUM):
                rebuild_catalogue_after_mark(user)
            return present_catalogue(user, mark(rows, user))
    # One build per account at a time. A pull to refresh and a rebuild a mark
    # asked for could otherwise overlap and the older one write last.
    with _catalogue_lock(user.key):
        if not force:
            cached = store.get_external(key, config.PODCAST_SUGGESTIONS_TTL_HOURS)
            if cached is not None and _unpack(cached)[1] >= taste.revision(
                    user.key, MEDIUM):
                return present_catalogue(user, mark(_unpack(cached)[0], user))
        rows = _build_catalogue(user, key)
    return present_catalogue(user, mark(rows, user))


def _build_catalogue(user: jellyfin.User, key: str) -> list[dict]:
    """Search the catalogue for this account and keep what was found."""
    # Read before the marks are, so a mark landing during the build leaves
    # this shelf reading as older than it.
    revision = taste.revision(user.key, MEDIUM)
    marks = taste.marks(user.key, MEDIUM)
    libraries = jellyfin.library_ids(MEDIUM)
    if not libraries:
        return []
    library = jellyfin.recommendation_items_for_user(MEDIUM, user.id, libraries)
    names_by_id = {recommendations._key(item): recommendations._podcast_names(item)
                   for item in library}
    by_id, unheld = recommendations.place_marks(MEDIUM, marks, names_by_id)
    profile = _Profile()
    for item in library:
        base = recommendations.seed_weight(item)
        found = by_id.get(recommendations._key(item))
        if found is not None and not found.active:
            # Taken back: as if no mark had ever been made.
            found = None
        if found is None:
            # As it always was: a podcast rated low here is not held against
            # its genre. Only a mark somebody made says "less like this".
            if base <= 0:
                continue
            weight = base
        else:
            weight = taste.weight(base, found)
        asked = found is not None and found.asked_only and base <= 0
        profile.add_item(item, weight, asked)
    for found in unheld:
        weight = taste.weight(0.0, found)
        if weight:
            profile.add_mark(found, weight)
    try:
        candidates = owned()
    except jellyfin.JellyfinUnavailable:
        candidates = []
    exclude_keys = {c.feed_key for c in candidates if c.feed_key}
    # Had, or "less like this", from this list: never offered again.
    exclude_keys |= {found.item_key for found in marks
                     if found.surface == taste.CATALOGUE and found.hides}
    # Only what the marks add, and nothing at all without any: the search is
    # then the one it always was.
    signed = {name: value for name, value in (
        ("avoid_genres", profile.avoided_genres()),
        ("avoid_makers", profile.avoided_makers()),
        ("asked_genres", profile.asked_only_genres()),
        ("asked_makers", profile.asked_only_makers())) if value}
    # The shows marked "more like this" themselves, scored without their own
    # mark if the search finds them again.
    asked_keys = {found.item_key for found in marks
                  if found.surface == taste.CATALOGUE and found.asked_only}
    if asked_keys:
        signed.update(asked_keys=asked_keys, own_genres=profile.own_genres(),
                      own_makers=profile.own_makers())
    rows = itunes.suggestions(profile.ranked_genres(), profile.ranked_makers(),
                              exclude_keys,
                              {c.name for c in candidates},
                              config.PODCAST_RECOMMENDATION_LIMIT,
                              **signed)
    # A show marked "more like this" the search did not bring back this time,
    # still not held or hidden: kept from what the catalogue said when it was
    # marked, so it and its way back stay on the list.
    found = {row.get("itemKey") for row in rows}
    held_titles = {itunes.normalise_title(c.name) for c in candidates}
    for mark in unheld:
        row = mark.details.get("row")
        if (mark.asked_only and mark.item_key not in found
                and mark.item_key not in exclude_keys and isinstance(row, dict)
                and itunes.normalise_title(row.get("title")) not in held_titles):
            rows.append({**row, "itemKey": mark.item_key, "score": 0.0,
                         "reason": ["you asked for more like this"]})
    store.put_external(key, {"rows": rows, "revision": revision})
    log.info("catalogue shelf user=%s seeds=%d genres=%s rows=%d marks=%d", user.key,
             sum(1 for item in library if recommendations.seed_weight(item) > 0),
             profile.ranked_genres()[:3], len(rows), len(marks))
    return rows


def _unpack(cached) -> tuple[list[dict], int]:
    """A cached catalogue shelf and the mark revision it was built from.

    One cached before marks existed is a bare list, and reads as built from
    none, which is what it was.
    """
    if isinstance(cached, dict):
        return list(cached.get("rows") or []), int(cached.get("revision") or 0)
    return list(cached or []), 0


class _Profile:
    """Genre and maker weights for the catalogue search, signed by marks.

    Keyed the way they always were, by the label as written, so an account
    with no marks gets exactly the search terms, order and scores it always
    did. A mark's genre is added to an existing label with the same letters in
    another case (the library's spelling wins), and the sign is decided on the
    total across every spelling, which is how the catalogue compares them: one
    maker can never earn the bonus and the penalty at once.
    """

    def __init__(self) -> None:
        self.genres: dict[str, float] = {}
        self.makers: dict[str, float] = {}
        # The same, without anything only asked for more of: what a show
        # marked "more like this" is scored against, so it is never like
        # itself.
        self._own_genres: dict[str, float] = {}
        self._own_makers: dict[str, float] = {}
        # Casefolded names with positive evidence from something had, and from
        # something only asked for more of.
        self._had: dict[str, set[str]] = {"genres": set(), "makers": set()}
        self._asked: dict[str, set[str]] = {"genres": set(), "makers": set()}

    def add_item(self, item: dict, weight: float, asked: bool) -> None:
        for genre in item.get("Genres") or []:
            self._add("genres", genre, weight, asked, fold=False)
        for studio in item.get("Studios") or []:
            name = str(studio.get("Name") or "").strip()
            if name:
                self._add("makers", name, weight, asked, fold=False)

    def add_mark(self, found: "taste.Mark", weight: float) -> None:
        details = found.details
        for genre in details.get("genres") or []:
            genre = str(genre).strip()
            if genre:
                self._add("genres", genre, weight, found.asked_only, fold=True)
        artist = str(details.get("artist") or "").strip()
        if artist:
            self._add("makers", artist, weight, found.asked_only, fold=True)

    def _add(self, kind: str, label: str, weight: float, asked: bool,
             fold: bool) -> None:
        weights = self.genres if kind == "genres" else self.makers
        if fold:
            label = next((known for known in weights
                          if known.casefold() == label.casefold()), label)
        weights[label] = weights.get(label, 0.0) + weight
        if not asked:
            own = self._own_genres if kind == "genres" else self._own_makers
            own[label] = own.get(label, 0.0) + weight
        if weight > 0:
            (self._asked if asked else self._had)[kind].add(label.casefold())

    def _net(self, weights: dict[str, float]) -> dict[str, float]:
        net: dict[str, float] = {}
        for label, weight in weights.items():
            net[label.casefold()] = net.get(label.casefold(), 0.0) + weight
        return net

    def _ranked(self, weights: dict[str, float]) -> list[str]:
        net = self._net(weights)
        return [label for label, weight in sorted(
                    weights.items(), key=lambda kv: (-kv[1], kv[0]))
                if weight > 0 and net[label.casefold()] > 0]

    def ranked_genres(self) -> list[str]:
        return self._ranked(self.genres)

    def ranked_makers(self) -> list[str]:
        return self._ranked(self.makers)

    def avoided_genres(self) -> set[str]:
        return {name for name, weight in self._net(self.genres).items() if weight < 0}

    def avoided_makers(self) -> set[str]:
        return {name for name, weight in self._net(self.makers).items() if weight < 0}

    def own_genres(self) -> list[str]:
        return self._ranked(self._own_genres)

    def own_makers(self) -> list[str]:
        return self._ranked(self._own_makers)

    def asked_only_genres(self) -> set[str]:
        return self._asked["genres"] - self._had["genres"]

    def asked_only_makers(self) -> set[str]:
        return self._asked["makers"] - self._had["makers"]


_catalogue_locks: dict[str, threading.Lock] = {}
_catalogue_guard = threading.Lock()


def _catalogue_lock(user_key: str) -> threading.Lock:
    with _catalogue_guard:
        return _catalogue_locks.setdefault(user_key, threading.Lock())


def present_catalogue(user: jellyfin.User, rows: list[dict]) -> list[dict]:
    """The catalogue shelf as it stands against this account's marks right now."""
    marks = {found.item_key: found for found in taste.marks(user.key, MEDIUM)
             if found.surface == taste.CATALOGUE}
    if not marks:
        return rows
    shown = []
    for row in rows:
        found = marks.get(row.get("itemKey") or "")
        if found is not None and found.hides:
            continue
        if found is not None and found.opinion == taste.MORE:
            row = {**row, "opinion": taste.MORE}
        shown.append(row)
    return shown


def catalogue_details(user: jellyfin.User, item_key: str) -> dict:
    """What the catalogue said about one podcast this account was offered.

    Kept with a mark so the mark can count for its genres and maker while the
    library does not hold it. Read from the account's own cached shelf, which
    is where the row came from; nothing is asked of Apple.
    """
    cached = store.get_external(_cache_key(user), _ANY_AGE_HOURS)
    for row in _unpack(cached)[0] if cached is not None else []:
        if row.get("itemKey") == item_key:
            return {
                "title": row.get("title") or "",
                "artist": row.get("artist") or "",
                "genres": list(row.get("genres") or []),
                "itunesId": row.get("itunesId") or "",
                # The row itself, to show again should no search bring it back.
                "row": {key: value for key, value in row.items()
                        if key not in ("score", "reason", "owned", "opinion")},
            }
    return {}


#: Long enough to find any cached shelf, however old.
_ANY_AGE_HOURS = 24 * 365 * 10

_catalogue_after_marks = taste.BehindRebuilds("podcast-catalogue")


def rebuild_catalogue_after_mark(user: jellyfin.User) -> None:
    """Rebuild this account's catalogue shelf behind the answer, if it has one."""
    key = _cache_key(user)
    if store.get_external(key, _ANY_AGE_HOURS) is None:
        return

    def rebuild() -> None:
        cached = store.get_external(key, _ANY_AGE_HOURS)
        # A pull to refresh may have rebuilt it from these marks already.
        if cached is not None and _unpack(cached)[1] >= taste.revision(user.key, MEDIUM):
            return
        catalogue_shelf(user, force=True)

    _catalogue_after_marks.request(user.key, rebuild)
