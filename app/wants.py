"""Asking for something, and reporting what became of the request.

One code path, used by the browser pages and the JSON API alike. Everything
that guards an acquisition -- the daily allowance, the duplicate check, the
ledger entry -- lives here, so a second caller cannot be added later that
quietly skips half of it.
"""
import time

from . import buskarr, config, jellyfin, logs, media, radarr, sonarr, store

log = logs.get("wants")

#: Asked for, not here yet, and not yet waiting long enough that saying so
#: would be misleading.
ON_ITS_WAY = "on_its_way"
#: Waiting long enough that "on its way" would be a lie. Not a failure: it
#: stays monitored and the acquisition tool's own sweep keeps retrying.
STILL_LOOKING = "still_looking"
#: It is in the library. For a series, at least one episode of it is.
IN_LIBRARY = "in_library"

DAY_SECONDS = 24 * 3600


class Denied(Exception):
    """The request was refused before anything was acquired."""


def allowance(user: jellyfin.User, medium: str) -> int | None:
    """Requests this account has left today on one medium, or None if uncapped.

    Jellyfin administrators are uncapped. The cap exists so that ten phones
    cannot saturate one line between them, not to ration the person who owns
    the server.
    """
    if user.is_admin:
        return None
    found = media.get(medium)
    if found is None:
        return 0
    spent = store.spent_today(user.key, medium, time.time() - DAY_SECONDS)
    return max(0, found.daily_cap - spent)


def search(query: str, medium: str, unit: str = "") -> list[dict]:
    """Catalogue hits for one medium, marked with what the library already has."""
    if media.get(medium) is None:
        return []
    limit = config.SEARCH_LIMIT
    if medium == media.MOVIE:
        return radarr.search(query, limit, media.owned().movie_tmdb)
    if medium == media.SERIES:
        index = media.owned()
        return sonarr.search(query, limit, index.series_tvdb, index.series_episodes)
    return buskarr.search(query, unit or "track", limit)


def want(user: jellyfin.User, medium: str, item_key: str, unit: str = "",
         hit: dict | None = None) -> tuple[str, str]:
    """Ask for one thing. Returns (state, message). Raises Denied if refused.

    Ordered so nothing is charged against the allowance until the acquisition
    tool has accepted it, and so a repeated tap is free: the ledger is keyed on
    (account, medium, item), and asking twice neither restarts the clock nor
    spends a second request.
    """
    found = media.get(medium)
    if found is None:
        raise Denied("That kind of thing cannot be asked for on this server.")
    unit = unit or found.units[0]
    if unit not in found.units:
        raise Denied(f"{unit!r} is not something that can be asked for.")

    if (existing := store.get(user.key, medium, item_key)) is not None:
        state = _state(existing, medium)
        log.info("want repeat user=%s medium=%s key=%s state=%s "
                 "(no allowance spent)", user.key, medium, item_key, state)
        return state, "Already asked for."

    price = media.cost(medium, unit)
    remaining = allowance(user, medium)
    if remaining is not None and remaining < price:
        log.warning("want denied user=%s medium=%s key=%s reason=daily-cap "
                    "cost=%d remaining=%d", user.key, medium, item_key,
                    price, remaining)
        raise Denied(_cap_message(found, unit, price, remaining))

    log.info("want user=%s medium=%s unit=%s key=%s cost=%d remaining=%s",
             user.key, medium, unit, item_key, price,
             "uncapped" if remaining is None else remaining)
    result = _add(medium, unit, item_key, hit or {}, user)
    if not result.ok:
        log.warning("want refused user=%s key=%s reason=%s",
                    user.key, item_key, result.message)
        raise Denied(result.message)

    hit = hit or {}
    store.record(
        user.key, medium, item_key, unit,
        # What the backend resolved it to is preferred over what the caller
        # sent: it is the spelling the library will carry when it lands.
        result.title or hit.get("title") or "",
        result.year or str(hit.get("year") or ""),
        price, result.backend_id)
    log.info("want accepted user=%s key=%s backend_id=%s message=%r",
             user.key, item_key, result.backend_id, result.message)
    return ON_ITS_WAY, result.message


def _add(medium: str, unit: str, item_key: str, hit: dict,
         user: jellyfin.User):
    """Hand one thing to whichever tool acquires that medium."""
    if medium == media.MOVIE:
        return radarr.add(_provider_id(item_key), hit.get("title", ""),
                          str(hit.get("year") or ""))
    if medium == media.SERIES:
        return sonarr.add(_provider_id(item_key), hit.get("title", ""))
    return buskarr.add(unit, hit, user.key)


def _provider_id(item_key: str) -> str:
    """The bare id out of a prefixed ledger key: `tmdb:1234` -> `1234`."""
    return item_key.split(":", 1)[1] if ":" in item_key else item_key


def _cap_message(found: media.Medium, unit: str, price: int,
                 remaining: int) -> str:
    """Why the request was refused, in terms of what was actually asked for."""
    if price > found.daily_cap:
        return (f"One {unit} is more than a day's allowance for "
                f"{found.label.lower()}. Ask for an album or a track instead.")
    if remaining <= 0:
        return (f"That is all the {found.label.lower()} for today. "
                "The allowance frees up again as the day rolls on.")
    return (f"A{'n' if unit[0] in 'aeiou' else ''} {unit} costs {price} of "
            f"today's allowance and {remaining} is left.")


def states(user: jellyfin.User, medium: str | None = None) -> list[dict]:
    """This account's requests and what has become of each.

    Arrival is settled here rather than polled by the client: there is no
    status to ask a backend for that Jellyfin does not already answer better,
    and the index this reads is the same one the search marked its hits with.
    """
    window = time.time() - config.ARRIVED_VISIBLE_HOURS * 3600
    rows = store.active(user.key, medium, window)
    if not rows:
        return []

    index = media.owned()
    newly_arrived: dict[str, set[str]] = {}
    out = []
    for row in rows:
        state = _state(row, row["medium"], index)
        if state == IN_LIBRARY and row["fulfilled_at"] is None:
            newly_arrived.setdefault(row["medium"], set()).add(row["item_key"])
        out.append(_described(row, state, index))

    for medium_key, keys in newly_arrived.items():
        store.mark_arrived(user.key, medium_key, keys)
        log.info("arrived user=%s medium=%s count=%d",
                 user.key, medium_key, len(keys))
    return out


def _state(row, medium: str, index: jellyfin.Owned | None = None) -> str:
    """One request's state.

    A row already marked fulfilled stays fulfilled. Re-deriving it would make
    a request flap back to "on its way" the moment a library scan is mid-run
    and its item is briefly absent.
    """
    if row["fulfilled_at"] is not None:
        return IN_LIBRARY
    index = index if index is not None else media.owned()
    if _arrived(row, medium, index):
        return IN_LIBRARY
    waited = time.time() - row["requested_at"]
    if waited > config.STILL_LOOKING_AFTER_HOURS * 3600:
        return STILL_LOOKING
    return ON_ITS_WAY


def _arrived(row, medium: str, index: jellyfin.Owned) -> bool:
    key = row["item_key"]
    if medium == media.MOVIE:
        return bool(radarr.arrived({key}, index.movie_tmdb))
    if medium == media.SERIES:
        return bool(sonarr.arrived({key}, index.series_tvdb,
                                   index.series_episodes))
    # Music asks buskarr, which placed the file and holds its exact identity.
    # An unreachable buskarr answers None, which is "unknown" and must not be
    # read as "not here" -- the row simply keeps waiting.
    reported = buskarr.state(row["backend_id"])
    return bool(reported and reported.get("state") == "have")


def _described(row, state: str, index: jellyfin.Owned) -> dict:
    """One request as a client shows it."""
    described = {
        "itemKey": row["item_key"],
        "medium": row["medium"],
        "unit": row["unit"],
        "title": row["title"],
        "year": row["year"],
        "state": state,
        "requestedAt": row["requested_at"],
    }
    if row["medium"] == media.SERIES:
        # A series arrives in pieces, so how much of it is here is part of
        # what its state means.
        described["episodesInLibrary"] = sonarr.progress(
            row["item_key"], index.series_episodes)
    elif row["medium"] == media.MUSIC and state != IN_LIBRARY:
        reported = buskarr.state(row["backend_id"])
        if reported:
            described["tracksInLibrary"] = reported.get("have")
            described["tracksTotal"] = reported.get("total")
            described["detail"] = reported.get("message")
    return described


def cancel(user: jellyfin.User, medium: str, item_key: str) -> tuple[bool, str]:
    """Take one thing off this account's list and stop looking for it.

    Three things it has to get right, all learned from the audiobook side:

    * the backend's row belongs to the household, so it is only deleted when
      nobody else is still waiting on the same thing;
    * the ledger row goes even when the backend will not answer, because
      refusing would strand it on screen for as long as that tool is down;
    * the day's allowance is refunded, which does mean a capped account can
      cancel and re-ask around the cap. That is the cheaper mistake.
    """
    row = store.get(user.key, medium, item_key)
    if row is None:
        return False, "That is not on your list."

    others = store.others_waiting(user.key, medium, item_key)
    if others:
        store.forget(user.key, medium, item_key)
        log.info("cancel user=%s key=%s kept: %d other(s) still waiting",
                 user.key, item_key, len(others))
        return True, ("Taken off your list. Somebody else is still waiting "
                      "for it, so it is still being looked for.")

    stopped = _stop(medium, row)
    store.forget(user.key, medium, item_key)
    log.info("cancel user=%s medium=%s key=%s backend_stopped=%s",
             user.key, medium, item_key, stopped)
    if not stopped:
        return True, ("Taken off your list. The acquisition tool could not be "
                      "reached, so it may still be looking.")
    return True, "Taken off your list, and no longer being looked for."


def _stop(medium: str, row) -> bool:
    """Call the acquisition off. Never removes anything already downloaded."""
    backend_id = row["backend_id"]
    if medium == media.MOVIE:
        return radarr.cancel(backend_id)
    if medium == media.SERIES:
        return sonarr.cancel(backend_id)
    return buskarr.cancel(backend_id)
