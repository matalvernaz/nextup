"""Owned TV recommendations from one Jellyfin user's watching history.

This is a vertical slice, not a shared recommendation framework. It proves the
TV signal and API shape beside Nextread's audiobook implementation; common
machinery moves only after both implementations have earned the same shape.
"""
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass

from . import config, jellyfin, logs

log = logs.get("recommendations")

RANKER_VERSION = "series-owned-v1"

# Cast is useful evidence, but a full TV cast is large enough that incidental
# guest overlap drowns everything else. Jellyfin preserves billing order, so
# the first six actors are the stable main-cast approximation available here.
MAIN_CAST_LIMIT = 6
CREATIVE_ROLES = frozenset({"Creator", "Director", "Writer"})


class UnknownLibrary(ValueError):
    """The caller asked for a library outside the configured TV libraries."""


def _normalise(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _progress(item: dict) -> float:
    """Series completion from Jellyfin, normalised to 0.0 through 1.0."""
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
    """How strongly one watched or explicitly liked show shapes the profile."""
    data = item.get("UserData") or {}
    progress = _progress(item)
    rating = _rating(item)

    # An explicit low rating must never promote its neighbours. Negative taste
    # is a later slice; zero influence is the honest first implementation.
    if rating is not None and rating < 5.0:
        return 0.0

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


def _joined(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return ", ".join(values[:-1]) + f", and {values[-1]}"


def _genre_reason(values: list[str]) -> str:
    noun = "genre" if len(values) == 1 else "genres"
    return f"shares the {_joined(values)} {noun} with shows you've watched"


def _score_candidates(library: list[dict], seeds: list[tuple[dict, float]]) -> list[dict]:
    profiles = {name: Counter() for name in ("genres", "people", "studios")}
    displays: dict[str, dict[str, str]] = {
        name: {} for name in profiles
    }
    for seed, weight in seeds:
        _add(profiles["genres"], displays["genres"], _genres(seed), weight)
        _add(profiles["people"], displays["people"], _people(seed), weight)
        _add(profiles["studios"], displays["studios"], _studios(seed), weight)

    rows = []
    for item in library:
        # Partially watched series belong to Jellyfin's Next Up surface. This
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

        evidence = (
            3.0 * sum(weight for _, weight in genre_matches)
            + 4.0 * sum(weight for _, weight in people_matches)
            + 1.5 * sum(weight for _, weight in studio_matches)
        )
        if evidence <= 0:
            continue

        community = item.get("CommunityRating")
        quality_tiebreak = 0.0
        if isinstance(community, (int, float)) and math.isfinite(community):
            quality_tiebreak = (
                max(0.0, min(10.0, float(community)) - 5.0) / 20.0)
        reasons: list[str] = []
        if people_matches:
            reasons.append(
                f"features {people_matches[0][0]} from a show you've watched")
        if genre_matches:
            names = [name for name, _ in genre_matches[:2]]
            reasons.append(_genre_reason(names))
        if studio_matches and len(reasons) < 2:
            reasons.append(
                f"from {studio_matches[0][0]}, whose shows you've watched")

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
    while remaining and len(selected) < config.SERIES_RECOMMENDATION_LIMIT:
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


def _recent(library: list[dict]) -> list[dict]:
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
    return [{
        "id": item["Id"],
        "title": item.get("Name") or "",
        "score": 0.0,
        "reason": ["recently added to your library"],
        "source": "recent",
    } for item in candidates[:config.SERIES_RECOMMENDATION_LIMIT]]


def build(library: list[dict]) -> dict:
    """Build one owned-series shelf from an already user-scoped library."""
    seeds = [(item, weight) for item in library
             if (weight := _seed_weight(item)) > 0]
    rows = _score_candidates(library, seeds) if seeds else _recent(library)
    return {
        "ranker_version": RANKER_VERSION,
        "seed_count": len(seeds),
        "recommendations": rows,
    }


@dataclass(frozen=True, slots=True)
class _Cached:
    built_at: float
    value: dict


_cache: dict[tuple[str, tuple[str, ...]], _Cached] = {}
_guard = threading.Lock()


def library_ids() -> tuple[str, ...]:
    """TV libraries eligible for this owned-only recommendation surface."""
    return tuple(jellyfin.library_ids("series"))


def result(user: jellyfin.User, library_id: str = "", force: bool = False) -> dict:
    """One user's shelf, optionally narrowed to a specific TV library."""
    available = library_ids()
    requested = jellyfin.normalise_id(library_id)
    if requested:
        by_normalised = {jellyfin.normalise_id(value): value for value in available}
        if requested not in by_normalised:
            raise UnknownLibrary(library_id)
        libraries = (by_normalised[requested],)
    else:
        libraries = available

    key = (user.id, tuple(jellyfin.normalise_id(value) for value in libraries))
    with _guard:
        cached = _cache.get(key)
        if cached and not force and (
                time.monotonic() - cached.built_at
                <= config.SERIES_RECOMMENDATION_CACHE_SECONDS):
            return cached.value

    library = jellyfin.series_for_user(user.id, libraries)
    built = build(library)
    with _guard:
        _cache[key] = _Cached(time.monotonic(), built)
    log.info("series recommendations user=%s libraries=%d seeds=%d rows=%d",
             user.key, len(libraries), built["seed_count"],
             len(built["recommendations"]))
    return built


def forget() -> None:
    """Drop every cached shelf. Used after configuration changes and in tests."""
    with _guard:
        _cache.clear()
