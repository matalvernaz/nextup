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

from . import config, jellyfin, logs

log = logs.get("recommendations")

SERIES_RANKER_VERSION = "series-owned-v2"
MOVIE_RANKER_VERSION = "movie-owned-v1"
SUPPORTED_MEDIA = ("series", "movie")

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
    return ("films you've watched" if medium == "movie"
            else "shows you've watched")


def _genre_reason(values: list[str], medium: str) -> str:
    noun = "genre" if len(values) == 1 else "genres"
    return f"shares the {_joined(values)} {noun} with {_watched_label(medium)}"


def _score_candidates(
    library: list[dict],
    seeds: list[tuple[dict, float]],
    medium: str,
) -> list[dict]:
    profiles, displays = _profiles(seeds)

    rows = []
    for item in library:
        # Partially watched media belong to Jellyfin's resume surfaces. This
        # shelf answers the different question: what should I start?
        data = item.get("UserData") or {}
        if (_seed_weight(item) > 0 or _progress(item) > 0
                or _rating(item) is not None or data.get("IsFavorite")):
            continue

        genre_matches = _matches(
            _genres(item), profiles["genres"], displays["genres"])
        people_matches = _matches(
            _people(item), profiles["people"], displays["people"])
        studio_matches = _matches(
            _studios(item), profiles["studios"], displays["studios"])

        evidence = _evidence(item, profiles)
        if evidence <= 0:
            continue

        community = item.get("CommunityRating")
        quality_tiebreak = 0.0
        if isinstance(community, (int, float)) and math.isfinite(community):
            quality_tiebreak = (
                max(0.0, min(10.0, float(community)) - 5.0) / 20.0)
        reasons: list[str] = []
        if people_matches:
            source_kind = "film" if medium == "movie" else "show"
            name = people_matches[0][0]
            reasons.append(
                f"{_credit(item, name)} {name}, from a {source_kind} "
                f"you've watched")
        if genre_matches:
            names = [name for name, _ in genre_matches[:2]]
            reasons.append(_genre_reason(names, medium))
        if studio_matches and len(reasons) < 2:
            reasons.append(
                f"from {studio_matches[0][0]}, whose {_watched_label(medium)}")

        source = (
            "person" if people_matches
            else "genre" if genre_matches
            else "studio"
        )
        rows.append({
            "id": item["Id"],
            "title": item.get("Name") or "",
            "score": round(evidence + quality_tiebreak, 2),
            "reason": reasons[:2],
            "source": source,
            "_genres": [name for name, _ in genre_matches[:2]],
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

    for row in selected:
        row.pop("_genres", None)
    return selected


def _recent(
    library: list[dict],
    medium: str,
    negative_seeds: list[tuple[dict, float]],
) -> list[dict]:
    candidates = [
        item for item in library
        if _seed_weight(item) <= 0
        and _progress(item) <= 0
        and _rating(item) is None
        and not (item.get("UserData") or {}).get("IsFavorite")
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
    return (config.MOVIE_RECOMMENDATION_LIMIT if medium == "movie"
            else config.SERIES_RECOMMENDATION_LIMIT)


def _cache_seconds(medium: str) -> int:
    return (config.MOVIE_RECOMMENDATION_CACHE_SECONDS if medium == "movie"
            else config.SERIES_RECOMMENDATION_CACHE_SECONDS)


def ranker_version(medium: str) -> str:
    return MOVIE_RANKER_VERSION if medium == "movie" else SERIES_RANKER_VERSION


def build(library: list[dict], medium: str = "series") -> dict:
    """Build one owned shelf from an already user-scoped library."""
    if medium not in SUPPORTED_MEDIA:
        raise ValueError(f"unsupported recommendation medium {medium!r}")
    seeds = [(item, weight) for item in library
             if (weight := _seed_weight(item)) != 0]
    positive = any(weight > 0 for _, weight in seeds)
    rows = (_score_candidates(library, seeds, medium)
            if positive else _recent(library, medium, seeds))
    return {
        "ranker_version": ranker_version(medium),
        "seed_count": len(seeds),
        "recommendations": rows,
    }


@dataclass(frozen=True, slots=True)
class _Cached:
    built_at: float
    value: dict


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
                time.monotonic() - cached.built_at
                <= _cache_seconds(medium)):
            return cached.value

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
                return cached.value
        library = jellyfin.recommendation_items_for_user(
            medium, user.id, libraries)
        built = build(library, medium)
        with _guard:
            _cache[key] = _Cached(time.monotonic(), built)
    log.info("%s recommendations user=%s libraries=%d seeds=%d rows=%d",
             medium, user.key, len(libraries), built["seed_count"],
             len(built["recommendations"]))
    return built


#: What a caller waiting on an out-of-band build is given to say.
BUILDING = ("Working out what to watch. This takes a moment the first time; "
            "reload the page and it will be here.")

#: Shelves being built where nobody is waiting. A page will not sit through a
#: cold film build -- twelve seconds of Jellyfin on this library -- so it
#: starts one and says so, and this is what stops the reload that follows from
#: starting a second.
_building: set[tuple] = set()

#: Why the last out-of-band build for a key failed. Kept because without it a
#: build that failed twenty minutes ago still reads as "working it out",
#: which is a page that never finishes and never says why.
_failures: dict[tuple, str] = {}


def shelf_or_start(user: jellyfin.User, library_id: str = "", *,
                   medium: str = "series") -> tuple[dict | None, str]:
    """A shelf already in hand, or nothing and one sentence about why not.

    For a caller that cannot wait. A shelf past its lifetime is served anyway
    while the rebuild runs behind it, which is what the book shelves do and
    for the same reason: an hour-old ranking is a better answer than a
    twelve-second wait for a slightly newer one.

    A failed build is not retried on its own. Reloading would otherwise start
    a fresh build every time, so the sentence stands until something calls
    `expire`.
    """
    if medium not in SUPPORTED_MEDIA:
        raise ValueError(f"unsupported recommendation medium {medium!r}")
    key = _cache_key(user, medium, _libraries_for(medium, library_id))
    with _guard:
        cached = _cache.get(key)
        if cached is not None and (time.monotonic() - cached.built_at
                                   <= _cache_seconds(medium)):
            return cached.value, ""
        if cached is None and key in _failures:
            return None, _failures[key]
        starting = key not in _building
        if starting:
            _building.add(key)
    if starting:
        threading.Thread(target=_build_behind, args=(user, library_id, medium, key),
                         name=f"shelf-{medium}-{user.id}",
                         daemon=True).start()
    if cached is not None:
        return cached.value, ""
    return None, BUILDING


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
        _failures[key] = detail


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
