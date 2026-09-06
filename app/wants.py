"""Asking for something, and reporting what became of the request.

One code path, used by the browser pages and the JSON API alike. Everything
that guards an acquisition -- the daily allowance, the duplicate check, the
ledger entry -- lives here, so a second caller cannot be added later that
quietly skips half of it.
"""
import time

from . import buskarr, config, jellyfin, logs, media, radarr, sonarr, store
from .books import adapter as books

log = logs.get("wants")

#: Asked for, not here yet, and not yet waiting long enough that saying so
#: would be misleading.
ON_ITS_WAY = "on_its_way"
#: Waiting long enough that "on its way" would be a lie. Not a failure: it
#: stays monitored and the acquisition tool's own sweep keeps retrying.
STILL_LOOKING = "still_looking"
#: It is in the library. A whole-series request has every currently aired
#: episode there; future episodes remain Sonarr's ongoing responsibility.
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


def search(query: str, medium: str, unit: str = "",
           user: jellyfin.User | None = None) -> list[dict]:
    """Catalogue hits for one medium, marked with what the library already has.

    The caller is optional because three of the four media do not need one:
    what a household owns in films, series and music is the same answer for
    everybody, and it comes from a shared index. Books are per-account -- the
    audiobook library read carries `userId`, because play state and ratings are
    what its shelf is built from -- so the book path is given the account and
    the others ignore it.
    """
    if media.get(medium) is None:
        return []
    limit = config.SEARCH_LIMIT
    if medium == media.MOVIE:
        return radarr.search(query, limit, media.owned().movie_tmdb)
    if medium == media.SERIES:
        return sonarr.search(query, limit, media.owned().series_tvdb)
    if medium == media.BOOK:
        return books.search_hits(user, query, unit or "book")
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
    if medium == media.BOOK:
        # Delegated whole rather than branch by branch. A book does not arrive
        # under the ASIN it was asked for, a series is a bounded batch of
        # requests rather than one, and both of those were got right once
        # already -- reimplementing them here as four more `if medium ==` arms
        # would be a second chance to get them wrong.
        try:
            return books.want(user, item_key, unit or "book", hit or {})
        except books.Denied as denied:
            # The adapter cannot import this module -- that is the cycle -- so
            # it raises its own refusal and this is where the two names meet.
            # Without it a capped account's fourth book, and an unresolvable
            # series name, both leave as a 500.
            raise Denied(str(denied)) from denied
    unit = unit or found.units[0]
    if unit not in found.units:
        raise Denied(f"{unit!r} is not something that can be asked for.")

    # Everything from here to the ledger write is one decision. Read as four
    # separate steps it let two taps arriving together both find the allowance
    # unspent, so a cap of one bought two -- and a request the backend had
    # accepted could be written twice.
    with store.key_lock(user.key, medium):
        return _admit(user, found, medium, item_key, unit, hit or {})


def _admit(user: jellyfin.User, found: media.Medium, medium: str,
           item_key: str, unit: str, hit: dict) -> tuple[str, str]:
    """The guarded half of `want`. Never called without its lock held."""
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
    result = _add(medium, unit, item_key, hit, user)
    if not result.ok:
        log.warning("want refused user=%s key=%s reason=%s",
                    user.key, item_key, result.message)
        raise Denied(result.message)

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
    # The name, not the ledger key: buskarr renders `requested_by` in its own
    # queue table for a person to read, and an account id says nothing there.
    return buskarr.add(unit, hit, user.name)


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

    Arrival is settled here rather than polled by the client. Jellyfin remains
    authoritative for playable media; Sonarr contributes the aired total and
    queued work needed to describe a whole-series request honestly.
    """
    window = time.time() - config.ARRIVED_VISIBLE_HOURS * 3600
    rows = store.active(user.key, medium, window)
    if not rows:
        return []

    # Books are settled by the book path, not by the chain below. That chain's
    # last arm is music, so a book row would otherwise be asked about by
    # buskarr under an empty backend id -- which answers nothing, forever, and
    # leaves an arrived book reading "on its way" for good.
    book_rows = [row for row in rows if row["medium"] == media.BOOK]
    rows = [row for row in rows if row["medium"] != media.BOOK]
    # Asked for only when there is a book row to ask about: it costs a Jellyfin
    # listing of the whole audiobook library.
    book_states = books.states(user) if book_rows else []
    if not rows:
        return book_states

    index = media.owned()
    # Counted once, for exactly the series on this list. Jellyfin says what is
    # playable; Sonarr supplies the currently aired total and queued work.
    episodes = media.episode_counts({
        row["item_key"].split(":", 1)[1] for row in rows
        if row["medium"] == media.SERIES and ":" in row["item_key"]})
    series_rows = [row for row in rows if row["medium"] == media.SERIES]
    progress_by_backend = sonarr.acquisition_progress({
        str(row["backend_id"]) for row in series_rows if row["backend_id"]})
    series_progress = {
        row["item_key"]: progress_by_backend.get(str(row["backend_id"]))
        for row in series_rows
    }
    newly_arrived: dict[str, set[str]] = {}
    out = []
    for row in rows:
        # Asked for once and passed to both callers. Each music row costs a
        # round trip to buskarr, and deriving the state and describing it are
        # two questions about the same answer.
        reported = (buskarr.state(row["backend_id"])
                    if row["medium"] == media.MUSIC
                    and row["fulfilled_at"] is None else None)
        state = _state(
            row, row["medium"], index, reported, episodes, series_progress)
        if state == IN_LIBRARY and row["fulfilled_at"] is None:
            newly_arrived.setdefault(row["medium"], set()).add(row["item_key"])
        out.append(_described(
            row, state, episodes, reported, series_progress))

    for medium_key, keys in newly_arrived.items():
        store.mark_arrived(user.key, medium_key, keys)
        log.info("arrived user=%s medium=%s count=%d",
                 user.key, medium_key, len(keys))
    if not book_states:
        return out
    # One list, newest first, however many paths settled it.
    return sorted(out + book_states, key=lambda entry: entry["requestedAt"],
                  reverse=True)


def _state(row, medium: str, index: jellyfin.Owned | None = None,
           reported: dict | None = None,
           episodes: dict[str, int] | None = None,
           series_progress: dict[str, sonarr.AcquisitionProgress | None]
           | None = None) -> str:
    """One request's state.

    A row already marked fulfilled stays fulfilled. Re-deriving it would make
    a request flap back to "on its way" the moment a library scan is mid-run
    and its item is briefly absent.
    """
    if row["fulfilled_at"] is not None:
        return IN_LIBRARY
    index = index if index is not None else media.owned()
    if _arrived(
            row, medium, index, reported, episodes, series_progress):
        return IN_LIBRARY
    waited = time.time() - row["requested_at"]
    if waited > config.STILL_LOOKING_AFTER_HOURS * 3600:
        return STILL_LOOKING
    return ON_ITS_WAY


def _arrived(row, medium: str, index: jellyfin.Owned,
             reported: dict | None = None,
             episodes: dict[str, int] | None = None,
             series_progress: dict[str, sonarr.AcquisitionProgress | None]
             | None = None) -> bool:
    key = row["item_key"]
    if medium == media.MOVIE:
        return bool(radarr.arrived({key}, index.movie_tmdb))
    if medium == media.SERIES:
        if episodes is None:
            provider_id = key.split(":", 1)[1] if ":" in key else ""
            episodes = media.episode_counts({provider_id})
        count = sonarr.progress(key, episodes)
        progress = (series_progress or {}).get(key)
        # One episode proves that a series has started arriving, not that the
        # whole-series request is complete. If Sonarr cannot supply a total,
        # keep waiting and report the known library count rather than making a
        # completion claim that cannot be substantiated.
        return bool(
            progress is not None
            and progress.episodes_total is not None
            and progress.episodes_total > 0
            and count is not None
            and count >= progress.episodes_total)
    # Music asks buskarr, which placed the file and holds its exact identity.
    # An unreachable buskarr answers None, which is "unknown" and must not be
    # read as "not here" -- the row simply keeps waiting.
    if reported is None:
        reported = buskarr.state(row["backend_id"])
    return bool(reported and reported.get("state") == "have")


def _described(
    row,
    state: str,
    episodes: dict[str, int],
    reported: dict | None = None,
    series_progress: dict[str, sonarr.AcquisitionProgress | None] | None = None,
) -> dict:
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
        # what its state means. Absent rather than zero where Jellyfin could
        # not be asked: nothing is known, and a zero would say otherwise.
        count = sonarr.progress(row["item_key"], episodes)
        if count is not None:
            described["episodesInLibrary"] = count
        progress = (series_progress or {}).get(row["item_key"])
        if progress is not None:
            if progress.episodes_total is not None:
                described["episodesTotal"] = progress.episodes_total
            if progress.episodes_queued is not None:
                described["episodesQueued"] = progress.episodes_queued
    elif row["medium"] == media.MUSIC and state != IN_LIBRARY:
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
    if medium == media.BOOK:
        return books.cancel(user, item_key)
    row = store.get(user.key, medium, item_key)
    if row is None:
        return False, "That is not on your list."

    # Dropped and counted in one transaction. Two accounts cancelling the same
    # film at the same moment each used to read the other as still waiting, so
    # neither called the acquisition off and both rows went -- leaving a
    # download running that nothing pointed at.
    existed, others = store.release(user.key, medium, item_key)
    if not existed:
        return False, "That is not on your list."
    if others:
        log.info("cancel user=%s key=%s kept: %d other(s) still waiting",
                 user.key, item_key, len(others))
        return True, ("Taken off your list. Somebody else is still waiting "
                      "for it, so it is still being looked for.")

    stopped = _stop(medium, row)
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
