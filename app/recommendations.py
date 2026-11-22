"""Owned film and TV recommendations from one Jellyfin user's history.

Films and series share Jellyfin's descriptive metadata and user-data shape,
so they share ranking primitives here. Catalogue search and acquisition stay
in their medium-specific modules; this is not a universal media model.
"""
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass

from . import config, itunes, jellyfin, logs, taste, tmdb

log = logs.get("recommendations")

# Bumped for the marks (app/taste.py): with none, a shelf is what it was.
SERIES_RANKER_VERSION = "series-owned-v4"
MOVIE_RANKER_VERSION = "movie-owned-v3"
PODCAST_RANKER_VERSION = "podcast-owned-v2"
SUPPORTED_MEDIA = ("series", "movie", "podcast")

#: What one of each medium is called in a reason, and how having had it is put.
_KIND_NOUN = {"movie": "film", "series": "show", "podcast": "podcast"}
_HAD_LABEL = {"movie": "films you've watched", "series": "shows you've watched",
              "podcast": "podcasts you've listened to"}
_HAD_ONE = {"movie": "a film you've seen", "series": "a show you've seen",
            "podcast": "a podcast you've listened to"}
#: The same, for evidence that comes only from "more like this" on something
#: nobody here has seen. Saying "you've seen" about it would be untrue.
_ASKED_LABEL = {"movie": "films you asked for more of",
                "series": "shows you asked for more of",
                "podcast": "podcasts you asked for more of"}
_ASKED_ONE = {"movie": "a film you asked for more of",
              "series": "a show you asked for more of",
              "podcast": "a podcast you asked for more of"}

#: Where evidence came from: something this account has had, or only something
#: it asked for more of.
HAD = "had"
ASKED = "asked"

# What one TMDb neighbour vote is worth against the metadata evidence beside
# it.
#
# Sized to matter without deciding. `_evidence` weights a shared person at 4.0
# and a shared genre at 3.0 per seed, so a title carrying a full cap of
# neighbour votes is worth about one strong cast overlap: enough to lift
# something the metadata alone would not have found, not enough to reorder a
# shelf around a single external opinion. Capped for the same reason
# `books.engine` caps its Audible votes -- one prolific franchise should not own
# every position.
W_TMDB_VOTE = 2.5
MAX_TMDB_WEIGHT = 3.0

# Position one in TMDb's list is evidence; position ten is weaker evidence. The
# same rank discount the book engine applies to an Audible neighbour.
TMDB_RANK_OFFSET = 1

# How many seeds may contribute neighbours to one build.
#
# A household account can have watched hundreds of things, and a cold cache
# would then be hundreds of requests in front of somebody waiting for a page.
# The heaviest-weighted seeds are the ones whose neighbours are worth having,
# so the cut is by weight rather than arbitrary. Warm, the whole thing is free.
MAX_TMDB_SEEDS = 20

# Cast is useful evidence, but a full TV cast is large enough that incidental
# guest overlap drowns everything else. Jellyfin preserves billing order, so
# the first six actors are the stable main-cast approximation available here.
MAIN_CAST_LIMIT = 6

#: How one person's part in an item is worded, most visible part first. A
#: person credited twice -- an actor who also wrote it -- is described by the
#: first of these that applies, because that is the part a viewer would name.
CREDIT_PHRASES = (
    ("Actor", "features"),
    ("Director", "directed by"),
    ("Creator", "created by"),
    ("Writer", "written by"),
)
CREATIVE_ROLES = frozenset(
    role for role, _ in CREDIT_PHRASES if role != "Actor")


class UnknownLibrary(ValueError):
    """The caller asked for a library outside one medium's configured views."""


def _normalise(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _progress(item: dict) -> float:
    """Playback progress from Jellyfin, normalised to 0.0 through 1.0."""
    data = item.get("UserData") or {}
    if data.get("Played"):
        return 1.0
    value = data.get("PlayedPercentage")
    if isinstance(value, (int, float)) and math.isfinite(value):
        return min(1.0, max(0.0, value / 100.0))
    return 0.0


def _rating(item: dict) -> float | None:
    value = (item.get("UserData") or {}).get("Rating")
    if isinstance(value, (int, float)) and math.isfinite(value):
        return min(10.0, max(0.0, float(value)))
    return None


def _seed_weight(item: dict) -> float:
    """How strongly one watched or rated item shapes the taste profile."""
    data = item.get("UserData") or {}
    progress = _progress(item)
    rating = _rating(item)

    # Explicit dislike outranks implicit evidence from having watched. The
    # distance below five controls the penalty so a four is not treated like a
    # one, while five remains neutral enough for playback progress to speak.
    if rating is not None and rating < 5.0:
        return -(5.0 - rating) / 4.0

    weight = 0.35 + 0.65 * math.sqrt(progress) if progress > 0 else 0.0
    if data.get("IsFavorite"):
        weight = max(weight, 1.25)
    if rating is not None and rating > 5.0:
        weight = max(weight, 0.5 + rating / 10.0)
    return weight


def _genres(item: dict) -> list[str]:
    return [str(value).strip() for value in item.get("Genres") or []
            if str(value).strip()]


def _studios(item: dict) -> list[str]:
    return [str(value.get("Name") or "").strip()
            for value in item.get("Studios") or []
            if str(value.get("Name") or "").strip()]


def _people(item: dict) -> list[str]:
    names: list[str] = []
    actors = 0
    for person in item.get("People") or []:
        name = str(person.get("Name") or "").strip()
        role = person.get("Type")
        if not name:
            continue
        if role in CREATIVE_ROLES:
            names.append(name)
        elif role == "Actor" and actors < MAIN_CAST_LIMIT:
            names.append(name)
            actors += 1
    return names


def _credit(item: dict, name: str) -> str:
    """How this item credits one person: "features", "directed by", and so on.

    Read from the item being described rather than from the seed that matched
    it. The two are frequently different parts -- a name reaches the taste
    profile for acting in something and turns up again directing this -- and
    only this side of it is known here.
    """
    wanted = _normalise(name)
    roles = {person.get("Type") for person in item.get("People") or []
             if _normalise(str(person.get("Name") or "")) == wanted}
    for role, phrase in CREDIT_PHRASES:
        if role in roles:
            return phrase
    return "features"


def _add(profile: Counter, display: dict[str, str], values: list[str],
         weight: float) -> None:
    seen: set[str] = set()
    for value in values:
        key = _normalise(value)
        if key and key not in seen:
            profile[key] += weight
            display.setdefault(key, value)
            seen.add(key)


def _matches(values: list[str], profile: Counter,
             display: dict[str, str]) -> list[tuple[str, float]]:
    matched = [
        (display[key], profile[key])
        for key in {_normalise(value) for value in values}
        if key and profile.get(key, 0.0) > 0
    ]
    return sorted(matched, key=lambda row: (-row[1], row[0].casefold()))


def _profile_total(values: list[str], profile: Counter) -> float:
    """Signed evidence shared with a candidate, counted once per feature."""
    keys = {_normalise(value) for value in values}
    return sum(profile.get(key, 0.0) for key in keys if key)


def _profiles(
    seeds: list[tuple[dict, float]],
) -> tuple[dict[str, Counter], dict[str, dict[str, str]]]:
    profiles = {name: Counter() for name in ("genres", "people", "studios")}
    displays: dict[str, dict[str, str]] = {
        name: {} for name in profiles
    }
    for seed, weight in seeds:
        _add(profiles["genres"], displays["genres"], _genres(seed), weight)
        _add(profiles["people"], displays["people"], _people(seed), weight)
        _add(profiles["studios"], displays["studios"], _studios(seed), weight)
    return profiles, displays


def _evidence(item: dict, profiles: dict[str, Counter]) -> float:
    return (
        3.0 * _profile_total(_genres(item), profiles["genres"])
        + 4.0 * _profile_total(_people(item), profiles["people"])
        + 1.5 * _profile_total(_studios(item), profiles["studios"])
    )


def _joined(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return ", ".join(values[:-1]) + f", and {values[-1]}"


def _watched_label(medium: str) -> str:
    return _HAD_LABEL.get(medium, "things you've watched")


def _genre_reason(values: list[str], medium: str, asked: bool = False) -> str:
    noun = "genre" if len(values) == 1 else "genres"
    label = (_ASKED_LABEL.get(medium, "things you asked for more of") if asked
             else _watched_label(medium))
    return f"shares the {_joined(values)} {noun} with {label}"


def _tmdb_id(item: dict) -> str:
    value = (item.get("ProviderIds") or {}).get("Tmdb")
    return str(value).strip() if value else ""


def _tmdb_votes(
    seeds: list[tuple[dict, float]],
    medium: str,
    asked_ids: frozenset[str] = frozenset(),
) -> tuple[Counter, dict[str, list[tuple[str, str]]], Counter]:
    """Neighbour votes from TMDb, and which title earned each one, and how.

    Each voter is kept with where it came from -- watched, or only asked for
    more of -- so the reason can name one somebody has actually seen whenever
    there is one, and never call the other kind "a film you've seen". The
    third answer is the votes from seen titles alone, which is all a title
    marked "more like this" is scored by.

    The signal a Jellyfin-only ranker cannot derive: genre, cast and studio all
    come out of this library's own metadata, and every one of them is a
    statement about what a title *is*. TMDb's list is a statement about what
    other people went on to watch, which is a different question and the one a
    recommendation is actually asking.

    Empty without a key, which is the shipped state, so the shelf is exactly
    what it was before.
    """
    votes: Counter = Counter()
    had_votes: Counter = Counter()
    because: dict[str, list[tuple[str, str]]] = {}
    # TMDb knows films and television; a podcast has no TMDb id to ask about.
    if not tmdb.configured() or medium not in ("movie", "series"):
        return votes, because, had_votes

    ranked = sorted(
        ((seed, weight) for seed, weight in seeds if weight > 0),
        key=lambda pair: (-pair[1], _normalise(pair[0].get("Name") or "")))
    for seed, weight in ranked[:MAX_TMDB_SEEDS]:
        seed_id = _tmdb_id(seed)
        if not seed_id:
            continue
        neighbours = tmdb.recommendations(seed_id, medium)
        title = seed.get("Name") or ""
        origin = ASKED if seed.get("Id") in asked_ids else HAD
        for position, neighbour in enumerate(neighbours, start=1):
            votes[neighbour] += weight / (position + TMDB_RANK_OFFSET)
            if origin == HAD:
                had_votes[neighbour] += weight / (position + TMDB_RANK_OFFSET)
            voters = because.setdefault(neighbour, [])
            if title and all(title != named for named, _ in voters):
                voters.append((title, origin))
    return votes, because, had_votes


def _score_candidates(
    library: list[dict],
    seeds: list[tuple[dict, float]],
    medium: str,
    marks: dict[str, "taste.Mark"] | None = None,
    asked_ids: frozenset[str] = frozenset(),
) -> list[dict]:
    marks = marks or {}
    profiles, displays = _profiles(seeds)
    # The same evidence without anything only asked for more of. Two jobs: a
    # thing marked "more like this" is scored against it, so it cannot vote
    # for itself, and a reason only says "you've watched" about evidence that
    # is in here. With no such marks it is the same profile.
    if asked_ids:
        had_profiles, _ = _profiles(
            [(seed, weight) for seed, weight in seeds
             if seed.get("Id") not in asked_ids])
    else:
        had_profiles = profiles
    votes, vote_because, had_votes = _tmdb_votes(seeds, medium, asked_ids)

    def had_evidence(kind: str, name: str) -> bool:
        return had_profiles[kind].get(_normalise(name), 0.0) > 0

    rows = []
    for item in library:
        # Partially watched media belong to Jellyfin's resume surfaces. This
        # shelf answers the different question: what should I start?
        data = item.get("UserData") or {}
        if (_seed_weight(item) > 0 or _progress(item) > 0
                or _rating(item) is not None or data.get("IsFavorite")):
            continue
        mark = marks.get(_key(item))
        # Already had, or "less like this": never offered again.
        if mark is not None and mark.hides:
            continue
        asked_for = mark is not None and mark.asked_only
        scoring = had_profiles if asked_for else profiles

        genre_matches = _matches(
            _genres(item), scoring["genres"], displays["genres"])
        people_matches = _matches(
            _people(item), scoring["people"], displays["people"])
        studio_matches = _matches(
            _studios(item), scoring["studios"], displays["studios"])

        evidence = _evidence(item, scoring)
        # A neighbour vote is evidence in its own right, so it can carry a
        # title the metadata says nothing about -- which is the point of having
        # a source outside the library. Added before the gate, not after it.
        # Only votes from seen titles for one asked about: another title only
        # asked about is not evidence that people who liked it liked this.
        neighbour = (had_votes if asked_for else votes).get(_tmdb_id(item), 0.0)
        if neighbour:
            evidence += W_TMDB_VOTE * min(MAX_TMDB_WEIGHT, neighbour)
        if evidence <= 0 and not asked_for:
            continue

        community = item.get("CommunityRating")
        quality_tiebreak = 0.0
        if isinstance(community, (int, float)) and math.isfinite(community):
            quality_tiebreak = (
                max(0.0, min(10.0, float(community)) - 5.0) / 20.0)
        reasons: list[str] = []
        # First, where there is one. It is the most specific thing that can be
        # said about a candidate -- a named title somebody actually watched --
        # and the two metadata reasons below are true of dozens of rows each.
        if neighbour and (voters := vote_because.get(_tmdb_id(item))):
            watched = [title for title, origin in voters if origin == HAD]
            if watched:
                reasons.append(
                    f"watched by people who liked {watched[0]}, "
                    f"{_HAD_ONE.get(medium, 'one you have seen')}")
            elif not asked_for:
                # A title only asked about cannot explain another one asked
                # about: it was not counted for it.
                reasons.append(
                    f"watched by people who liked {voters[0][0]}, "
                    f"which you asked for more of")
        if people_matches:
            name = people_matches[0][0]
            one = (_HAD_ONE.get(medium, 'one you have seen')
                   if had_evidence("people", name)
                   else _ASKED_ONE.get(medium, "one you asked for more of"))
            reasons.append(f"{_credit(item, name)} {name}, from {one}")
        if genre_matches:
            names = [name for name, _ in genre_matches[:2]]
            had_names = [name for name in names if had_evidence("genres", name)]
            if had_names:
                reasons.append(_genre_reason(had_names, medium))
            else:
                reasons.append(_genre_reason(names, medium, asked=True))
        if studio_matches and len(reasons) < 2:
            studio = studio_matches[0][0]
            label = (_watched_label(medium) if had_evidence("studios", studio)
                     else _ASKED_LABEL.get(medium, "things you asked for more of"))
            reasons.append(f"from {studio}, whose {label}")
        if not reasons and asked_for:
            # Kept, though nothing else here points at it: this account asked
            # for more like it, and taking it away for that would be the app
            # arguing with them.
            reasons.append("you asked for more like this")

        source = (
            "neighbour" if neighbour
            else "person" if people_matches
            else "genre" if genre_matches
            else "studio" if studio_matches
            else "asked"
        )
        rows.append({
            "id": item["Id"],
            "title": item.get("Name") or "",
            "score": round(max(0.0, evidence) + quality_tiebreak, 2),
            "reason": reasons[:2],
            "source": source,
            "_genres": [name for name, _ in genre_matches[:2]],
            **({"_asked": True} if asked_for else {}),
        })

    # A small greedy penalty prevents one broad genre from occupying the whole
    # shelf while preserving the underlying score for attribution and tuning.
    selected: list[dict] = []
    genre_counts: Counter = Counter()
    remaining = rows[:]
    while remaining and len(selected) < limit(medium):
        remaining.sort(key=lambda row: (
            -(row["score"] - 0.75 * sum(
                genre_counts[_normalise(genre)] for genre in row["_genres"])),
            _normalise(row["title"]),
            row["id"],
        ))
        chosen = remaining.pop(0)
        selected.append(chosen)
        for genre in chosen["_genres"]:
            genre_counts[_normalise(genre)] += 1
    # Anything marked "more like this" stays past the cut: the mark is
    # standing, and its row is where "Undo more like this" lives.
    selected += [row for row in remaining if row.get("_asked")]

    for row in selected:
        row.pop("_genres", None)
        row.pop("_asked", None)
    return selected


def _recent(
    library: list[dict],
    medium: str,
    negative_seeds: list[tuple[dict, float]],
    marks: dict[str, "taste.Mark"] | None = None,
) -> list[dict]:
    marks = marks or {}
    candidates = [
        item for item in library
        if _seed_weight(item) <= 0
        and _progress(item) <= 0
        and _rating(item) is None
        and not (item.get("UserData") or {}).get("IsFavorite")
        and not ((mark := marks.get(_key(item))) is not None and mark.hides)
    ]
    candidates.sort(key=lambda item: (
        _normalise(item.get("Name") or ""), item.get("Id") or ""))
    candidates.sort(key=lambda item: item.get("DateCreated") or "", reverse=True)
    if negative_seeds:
        profiles, _ = _profiles(negative_seeds)
        # With no positive history, recency remains the honest reason for this
        # shelf. Signed evidence still pushes items resembling an explicit
        # dislike behind equally viable alternatives instead of ignoring the
        # only taste signal the account has supplied.
        candidates.sort(key=lambda item: _evidence(item, profiles), reverse=True)
    return [{
        "id": item["Id"],
        "title": item.get("Name") or "",
        "score": 0.0,
        "reason": ["recently added to your library"],
        "source": "recent",
    } for item in candidates[:limit(medium)]]


def limit(medium: str) -> int:
    if medium == "movie":
        return config.MOVIE_RECOMMENDATION_LIMIT
    if medium == "podcast":
        return config.PODCAST_RECOMMENDATION_LIMIT
    return config.SERIES_RECOMMENDATION_LIMIT


def _cache_seconds(medium: str) -> int:
    if medium == "movie":
        return config.MOVIE_RECOMMENDATION_CACHE_SECONDS
    if medium == "podcast":
        return config.PODCAST_RECOMMENDATION_CACHE_SECONDS
    return config.SERIES_RECOMMENDATION_CACHE_SECONDS


def ranker_version(medium: str) -> str:
    if medium == "movie":
        return MOVIE_RANKER_VERSION
    if medium == "podcast":
        return PODCAST_RANKER_VERSION
    return SERIES_RANKER_VERSION


#: The seed weighting, for the podcast catalogue shelf, which builds its taste
#: profile the same way this module builds its own.
def seed_weight(item: dict) -> float:
    return _seed_weight(item)


def _key(item: dict) -> str:
    """A library item's id, the one way marks and rows are compared."""
    return jellyfin.normalise_id(item.get("Id") or "")


def _podcast_names(item: dict) -> dict[str, str]:
    """The three names a podcast can be recognised by: feed, Apple id, title.

    The order `podcasts.match` tries them in. A podcast taken from the
    catalogue shelf is marked under its feed, and once it has been added it is
    a library item the fork wrote the same feed onto.
    """
    provider_ids = item.get("ProviderIds") or {}
    feed = str(provider_ids.get("PodcastFeed") or "").strip()
    return {
        "feed": itunes.item_key(feed) if feed else "",
        "itunes": str(provider_ids.get("iTunes") or "").strip(),
        "title": itunes.normalise_title(item.get("Name")),
    }


def _podcast_holding(names_by_id: dict[str, dict[str, str]],
                     mark: "taste.Mark") -> str | None:
    """Which held podcast a catalogue mark is about, exact ids before the title."""
    itunes_id = str(mark.details.get("itunesId") or "").strip()
    title = itunes.normalise_title(mark.title or mark.details.get("title"))
    for field, wanted in (("feed", mark.item_key), ("itunes", itunes_id),
                          ("title", title)):
        if not wanted:
            continue
        for item_id, names in names_by_id.items():
            if names[field] and names[field] == wanted:
                return item_id
    return None


def _catalogue_seed(mark: "taste.Mark") -> dict:
    """A podcast the library does not hold, as the ranker reads a library item.

    What the catalogue said about it when it was marked: its genres, and its
    maker as the studio, which is where a podcast's publisher lives here.
    """
    details = mark.details
    artist = str(details.get("artist") or "").strip()
    return {
        "Id": f"catalogue:{mark.item_key}",
        "Name": mark.title or str(details.get("title") or ""),
        "Genres": [str(genre) for genre in details.get("genres") or []
                   if str(genre).strip()],
        "People": [],
        "Studios": [{"Name": artist}] if artist else [],
        "UserData": {},
    }


def place_marks(
    medium: str,
    marks: list["taste.Mark"],
    names_by_id: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, "taste.Mark"], list["taste.Mark"]]:
    """Each mark against the held item it is about, and the marks about nothing held.

    `names_by_id` is how a held podcast is recognised (see `_podcast_names`),
    by normalised id. Films and shows are only ever marked from the library,
    so for them a mark's key is the item's id and nothing else.
    """
    by_id: dict[str, taste.Mark] = {}
    unheld: list[taste.Mark] = []
    for mark in marks:
        if mark.surface == taste.OWNED:
            key = jellyfin.normalise_id(mark.item_key)
            by_id[key] = taste.newest(by_id.get(key), mark)
            continue
        found = (_podcast_holding(names_by_id, mark)
                 if medium == "podcast" and names_by_id else None)
        if found is None:
            unheld.append(mark)
        else:
            by_id[found] = taste.newest(by_id.get(found), mark)
    return by_id, unheld


def build(library: list[dict], medium: str = "series",
          marks: list["taste.Mark"] | None = None) -> dict:
    """Build one owned shelf from an already user-scoped library.

    `marks` are what this account has said about its suggestions. With none,
    the shelf is exactly what it was before they existed.
    """
    if medium not in SUPPORTED_MEDIA:
        raise ValueError(f"unsupported recommendation medium {medium!r}")
    names_by_id = ({_key(item): _podcast_names(item) for item in library}
                   if medium == "podcast" else {})
    by_id, unheld = place_marks(medium, marks or [], names_by_id)

    seeds: list[tuple[dict, float]] = []
    asked: set[str] = set()
    for item in library:
        base = _seed_weight(item)
        mark = by_id.get(_key(item))
        weight = taste.weight(base, mark)
        if weight == 0:
            continue
        seeds.append((item, weight))
        # Asked-only evidence: "more like this" on something with nothing
        # else to say this account has had it.
        if mark is not None and mark.asked_only and base <= 0:
            asked.add(item["Id"])
    # Podcasts this account marked on the catalogue shelf that the library
    # still does not hold. Evidence about taste all the same.
    if medium == "podcast":
        for mark in unheld:
            weight = taste.weight(0.0, mark)
            if weight == 0:
                continue
            seed = _catalogue_seed(mark)
            seeds.append((seed, weight))
            if mark.asked_only:
                asked.add(seed["Id"])

    positive = any(weight > 0 for _, weight in seeds)
    rows = (_score_candidates(library, seeds, medium, by_id, frozenset(asked))
            if positive else _recent(library, medium, seeds, by_id))
    built = {
        "ranker_version": ranker_version(medium),
        "seed_count": len(seeds),
        "recommendations": rows,
    }
    if medium == "podcast":
        # Kept beside the shelf, never sent: what lets a later read recognise
        # a row by the catalogue mark made on it before it was added.
        offered = {jellyfin.normalise_id(row["id"]) for row in rows}
        built["names"] = {key: names for key, names in names_by_id.items()
                          if key in offered}
    return built


def present(user: jellyfin.User, medium: str, built: dict) -> dict:
    """A shelf as it stands against this account's marks right now.

    Applied on every read, however the shelf was got: a shelf built a minute
    before somebody said "already watched" must not offer it to them again
    while the rebuild runs. Rows marked "more like this" say so.
    """
    marks = taste.marks(user.key, medium)
    if not marks:
        return built
    by_id, _ = place_marks(medium, marks, built.get("names"))
    rows = []
    for row in built["recommendations"]:
        mark = by_id.get(jellyfin.normalise_id(row["id"]))
        if mark is not None and mark.hides:
            continue
        if mark is not None and mark.opinion == taste.MORE:
            row = {**row, "opinion": taste.MORE}
        rows.append(row)
    return {**built, "recommendations": rows}


@dataclass(frozen=True, slots=True)
class _Cached:
    built_at: float
    value: dict
    #: The account's newest mark when this was built. A shelf built from an
    #: older one is out of date however young it is.
    revision: int = 0


_cache: dict[tuple[str, str, tuple[str, ...]], _Cached] = {}
_guard = threading.Lock()
#: One lock per cache key, so a cold build for one account does not hold up a
#: cached read for another.
_build_locks: dict[tuple[str, str, tuple[str, ...]], threading.Lock] = {}


def _build_lock(key) -> threading.Lock:
    with _guard:
        return _build_locks.setdefault(key, threading.Lock())


def library_ids(medium: str = "series") -> tuple[str, ...]:
    """Libraries eligible for one owned-only recommendation surface."""
    return (tuple(jellyfin.library_ids(medium))
            if medium in SUPPORTED_MEDIA else ())


#: How long a settled list of rankable media is kept. Every page's navigation
#: asks for it, and it answers a question about the deployment -- which
#: Jellyfin libraries exist -- rather than one about the request.
OFFERED_TTL_SECONDS = 900

#: And how long an unsettled one is kept: Jellyfin could not be asked, or has
#: no library of either kind yet. Short, because creating the library ought to
#: be enough on its own to make the shelf appear, and an empty answer is what
#: takes it out of the navigation altogether.
OFFERED_PROVISIONAL_SECONDS = 60

_offered: tuple[float, tuple[str, ...], bool] | None = None


def offered() -> tuple[str, ...]:
    """Media this server can rank.

    A Jellyfin library of that kind is the whole test. These shelves are built
    from what the server already holds, so an installation with no Radarr
    still has a film shelf, and gating it on the acquisition tool would take
    away the half that works without one.

    An empty answer is barely cached at all. It is what takes the shelf out
    of the navigation altogether, so an outage, or a library created a minute
    after the container started, must not be allowed to settle it.
    """
    global _offered
    with _guard:
        if _offered is not None:
            at, value, settled = _offered
            lifetime = (OFFERED_TTL_SECONDS if settled
                        else OFFERED_PROVISIONAL_SECONDS)
            if time.monotonic() - at <= lifetime:
                return value
    found = []
    asked = True
    for medium in SUPPORTED_MEDIA:
        try:
            if library_ids(medium):
                found.append(medium)
        except jellyfin.JellyfinUnavailable as exc:
            log.warning("cannot tell whether %s can be ranked: %s", medium, exc)
            asked = False
    value = tuple(found)
    with _guard:
        _offered = (time.monotonic(), value, asked and bool(value))
    return value


def _libraries_for(medium: str, library_id: str) -> tuple[str, ...]:
    """The libraries one request covers, narrowed to one where asked."""
    available = library_ids(medium)
    requested = jellyfin.normalise_id(library_id)
    if not requested:
        return available
    by_normalised = {jellyfin.normalise_id(value): value for value in available}
    if requested not in by_normalised:
        raise UnknownLibrary(library_id)
    return (by_normalised[requested],)


def _cache_key(user: jellyfin.User, medium: str,
               libraries: tuple[str, ...]) -> tuple:
    return (medium, user.id,
            tuple(jellyfin.normalise_id(value) for value in libraries))


def result(
    user: jellyfin.User,
    library_id: str = "",
    force: bool = False,
    *,
    medium: str = "series",
) -> dict:
    """One user's shelf, optionally narrowed to one configured library."""
    if medium not in SUPPORTED_MEDIA:
        raise ValueError(f"unsupported recommendation medium {medium!r}")
    libraries = _libraries_for(medium, library_id)
    key = _cache_key(user, medium, libraries)
    with _guard:
        cached = _cache.get(key)
    if cached and not force and (
            time.monotonic() - cached.built_at <= _cache_seconds(medium)):
        # Young, but older than a mark made since: served as it stands against
        # the marks, while a rebuild that reads them runs behind.
        if cached.revision < taste.revision(user.key, medium):
            _rebuild_behind(user, medium, key)
        return present(user, medium, cached.value)

    # One build per key at a time. Without this, every request that arrives
    # during a cold build starts its own: the same Jellyfin listing of the same
    # library, several times over, on the one occasion the server is already
    # busy. The book shelves have had this since they were measured at twelve
    # seconds; these are the same shape of work.
    with _build_lock(key):
        with _guard:
            cached = _cache.get(key)
            if cached and not force and (
                    time.monotonic() - cached.built_at
                    <= _cache_seconds(medium)):
                return present(user, medium, cached.value)
        # Read before the marks are, so a mark landing during the build leaves
        # this shelf reading as older than it and rebuilt again.
        revision = taste.revision(user.key, medium)
        marks = taste.marks(user.key, medium)
        library = jellyfin.recommendation_items_for_user(
            medium, user.id, libraries)
        built = build(library, medium, marks)
        with _guard:
            _cache[key] = _Cached(time.monotonic(), built, revision)
    log.info("%s recommendations user=%s libraries=%d seeds=%d rows=%d marks=%d",
             medium, user.key, len(libraries), built["seed_count"],
             len(built["recommendations"]), len(marks))
    return present(user, medium, built)


#: Rebuilds a mark asked for, one at a time per shelf. See `taste.BehindRebuilds`.
_after_marks = taste.BehindRebuilds("shelf")


def _rebuild_behind(user: jellyfin.User, medium: str, key: tuple) -> None:
    """Rebuild one cached shelf behind the answer, if a mark has outdated it."""
    library_ids = key[2]
    # Every library, or the one asked for: `_libraries_for` gives one of those
    # two, and a single id names either the same way.
    library_id = library_ids[0] if len(library_ids) == 1 else ""

    def rebuild() -> None:
        with _guard:
            cached = _cache.get(key)
        # A pull to refresh may have rebuilt it from these marks already.
        if cached is not None and cached.revision >= taste.revision(user.key, medium):
            return
        result(user, library_id, force=True, medium=medium)

    _after_marks.request(key, rebuild)


def rebuild_after_mark(user: jellyfin.User, medium: str) -> None:
    """Rebuild, behind the answer, every shelf of this account a mark has outdated.

    Only shelves already built: one nobody has asked for yet is built from the
    marks as they stand when somebody does.
    """
    if medium not in SUPPORTED_MEDIA:
        return
    with _guard:
        keys = [key for key in _cache if key[0] == medium and key[1] == user.id]
    for key in keys:
        _rebuild_behind(user, medium, key)


#: What a caller waiting on an out-of-band build is given to say.
BUILDING = ("Working out what to watch. This takes a moment the first time; "
            "reload the page and it will be here.")

#: Shelves being built where nobody is waiting. A page will not sit through a
#: cold film build -- twelve seconds of Jellyfin on this library -- so it
#: starts one and says so, and this is what stops the reload that follows from
#: starting a second.
_building: set[tuple] = set()

#: How long a failed build is left alone before another one is allowed. A
#: page that started a build on every load would ask a Jellyfin that is
#: properly down once per reload; one that never retried would need somebody
#: to press a button before a restarted Jellyfin made any difference.
RETRY_AFTER_SECONDS = 120

#: When the last out-of-band build for a key failed, and why. Kept because
#: without it a build that failed twenty minutes ago still reads as "working
#: it out", which is a page that never finishes and never says why.
_failures: dict[tuple, tuple[float, str]] = {}


def shelf_or_start(user: jellyfin.User, library_id: str = "", *,
                   medium: str = "series") -> tuple[dict | None, str]:
    """A shelf already in hand, or nothing and one sentence about why not.

    For a caller that cannot wait. A shelf past its lifetime is served anyway
    while the rebuild runs behind it, which is what the book shelves do and
    for the same reason: an hour-old ranking is a better answer than a
    twelve-second wait for a slightly newer one.

    A build that has just failed is not retried for `RETRY_AFTER_SECONDS`,
    however stale the answer being served is. Reloading would otherwise start
    a fresh build every time, so a Jellyfin that is down would be asked once
    per page load and the sentence would never change.
    """
    if medium not in SUPPORTED_MEDIA:
        raise ValueError(f"unsupported recommendation medium {medium!r}")
    key = _cache_key(user, medium, _libraries_for(medium, library_id))
    with _guard:
        cached = _cache.get(key)
    if cached is not None and (time.monotonic() - cached.built_at
                               <= _cache_seconds(medium)):
        if cached.revision < taste.revision(user.key, medium):
            _rebuild_behind(user, medium, key)
        return present(user, medium, cached.value), ""
    with _guard:
        failed_at, detail = _failures.get(key, (0.0, ""))
        waiting = detail and (
            time.monotonic() - failed_at <= RETRY_AFTER_SECONDS)
        starting = not waiting and key not in _building
        if starting:
            _building.add(key)
    if starting:
        threading.Thread(target=_build_behind, args=(user, library_id, medium, key),
                         name=f"shelf-{medium}-{user.id}",
                         daemon=True).start()
    if cached is not None:
        # The stale shelf, and -- while a rebuild is failing -- why it is the
        # newest one there is. Silence here would read as a current answer.
        return present(user, medium, cached.value), detail if waiting else ""
    return None, detail if waiting else BUILDING


def _build_behind(user: jellyfin.User, library_id: str, medium: str,
                  key: tuple) -> None:
    """Build one shelf where nobody is waiting, and record what happened."""
    try:
        result(user, library_id, force=True, medium=medium)
        with _guard:
            _failures.pop(key, None)
    except jellyfin.JellyfinUnavailable as exc:
        log.warning("%s shelf build failed user=%s: %s", medium, user.key, exc)
        _remember_failure(
            key, "Jellyfin could not be reached, so this could not be worked "
                 "out. Try again once it is back.")
    except Exception as exc:  # noqa: BLE001 - a build with no caller must not
        # take the process down, and the page has no other way to learn that
        # the thing it is waiting for is never going to arrive.
        log.warning("%s shelf build failed user=%s: %s", medium, user.key, exc)
        _remember_failure(
            key, "Working this out did not succeed. The log says why.")
    finally:
        with _guard:
            _building.discard(key)


def _remember_failure(key: tuple, detail: str) -> None:
    with _guard:
        _failures[key] = (time.monotonic(), detail)


def expire(user: jellyfin.User, library_id: str = "", *,
           medium: str = "series") -> None:
    """Mark one shelf due for a rebuild, and let a failed one be tried again."""
    if medium not in SUPPORTED_MEDIA:
        raise ValueError(f"unsupported recommendation medium {medium!r}")
    key = _cache_key(user, medium, _libraries_for(medium, library_id))
    with _guard:
        _cache.pop(key, None)
        _failures.pop(key, None)
    log.info("%s shelf expired user=%s", medium, user.key)


def forget() -> None:
    """Drop every cached shelf. Used after configuration changes and in tests."""
    global _offered
    with _guard:
        _cache.clear()
        _failures.clear()
        _offered = None
