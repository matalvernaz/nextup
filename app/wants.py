"""Asking for something, and reporting what became of the request.

One code path, used by the browser pages and the JSON API alike. Everything
that guards an acquisition -- the daily allowance, the duplicate check, the
ledger entry -- lives here, so a second caller cannot be added later that
quietly skips half of it.
"""
import time

from . import (buskarr, config, jellyfin, logs, media, radarr, seasons,
               sonarr, store)
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


def daily_cap(user: jellyfin.User, medium: str) -> int | None:
    """What this account is allowed in a day on one medium, None if uncapped.

    The account's own number where somebody has set one, and the configured
    cap otherwise. An override is stored only where it differs from the
    setting, so raising the setting still raises it for everybody who has not
    been given a number of their own.

    Every reader goes through here -- the allowance arithmetic, the refusal
    message and the figure published to clients -- because a lowered cap that
    only half the callers know about refuses a request and then explains it in
    terms of a limit the person has not got.
    """
    if user.is_admin:
        return None
    found = media.get(medium)
    if found is None:
        return 0
    return store.daily_cap(user.key, medium, found.daily_cap)


def allowance(user: jellyfin.User, medium: str) -> int | None:
    """Requests this account has left today on one medium, or None if uncapped.

    Jellyfin administrators are uncapped. The cap exists so that ten phones
    cannot saturate one line between them, not to ration the person who owns
    the server.
    """
    cap = daily_cap(user, medium)
    if cap is None:
        return None
    spent = store.spent_today(user.key, medium, time.time() - DAY_SECONDS)
    return max(0, cap - spent)


def reset_allowance(user: jellyfin.User, medium: str) -> int | None:
    """Give one account its day's requests back on one medium.

    Returns what it has left afterwards, which is `None` for an account that
    was never capped. The lock is the same one a request takes, so a reset
    cannot land between another caller's allowance check and its ledger write
    and leave that request charged against a day it was forgiven.
    """
    if media.get(medium) is None:
        raise LookupError(f"this server does not serve {medium!r}")
    with store.key_lock(user.key, medium):
        store.reset_allowance(user.key, medium)
    log.info("allowance reset user=%s medium=%s", user.key, medium)
    return allowance(user, medium)


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
         hit: dict | None = None, choice: seasons.Seasons | None = None,
         remember: bool = False) -> tuple[str, str]:
    """Ask for one thing. Returns (state, message). Raises Denied if refused.

    `choice` is which seasons of a series to ask for this once, and `remember`
    keeps it as this account's usual choice as well. That is the answer to the
    question a client puts before somebody's first series, kept before the ask
    is tried so a refused ask (a spent allowance) does not lose it.

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
        raise Denied(f"{unit} is not something that can be asked for.")
    if medium == media.SERIES:
        if choice is not None and remember:
            store.put_user_setting(user.key, seasons.SETTING, choice.encode())
            log.info("seasons remembered user=%s choice=%s",
                     user.key, choice.encode())
        choice = choice or usual_seasons(user)
    else:
        choice = None

    # Everything from here to the ledger write is one decision. Read as four
    # separate steps it let two taps arriving together both find the allowance
    # unspent, so a cap of one bought two -- and a request the backend had
    # accepted could be written twice.
    # Two locks, always in this order. The per-account one makes the allowance
    # arithmetic a single decision. The household one -- keyed on the thing
    # rather than the asker -- makes admitting this item and calling it off
    # mutually exclusive across accounts: cancel counts the remaining waiters
    # and then stops the backend, and a request admitted between those two
    # steps used to be stopped by somebody else's cancellation while its ledger
    # row and its spent allowance survived.
    #
    # Cancel takes only the household lock, so there is no pair of locks it can
    # hold in the opposite order and no cycle to deadlock on.
    with store.key_lock(user.key, medium):
        with store.key_lock("item", medium, item_key):
            return _admit(user, found, medium, item_key, unit, hit or {},
                          choice)


def usual_seasons(user: jellyfin.User) -> seasons.Seasons | None:
    """Which seasons this account gets when an ask says nothing.

    Their own choice, then the server's default, then None, which leaves the
    add to `SONARR_MONITOR` as it was before anybody could choose.
    """
    own = seasons.decode(store.user_setting(user.key, seasons.SETTING))
    return own or seasons.decode(config.SERIES_SEASONS_DEFAULT)


def _admit(user: jellyfin.User, found: media.Medium, medium: str,
           item_key: str, unit: str, hit: dict,
           choice: seasons.Seasons | None = None) -> tuple[str, str]:
    """The guarded half of `want`. Never called without its lock held."""
    if (existing := store.get(user.key, medium, item_key)) is not None:
        if existing["fulfilled_at"] is None or _still_held(existing, medium):
            state = _state(existing, medium)
            log.info("want repeat user=%s medium=%s key=%s state=%s "
                     "(no allowance spent)", user.key, medium, item_key, state)
            return state, "Already asked for."
        # It arrived once and has since left the library. That is a new
        # request, not a repeat: the closed row goes, and everything below
        # charges and records this one from scratch. Without this a film
        # deleted from Jellyfin could never be asked for again by the account
        # that had it, on either surface, with "Already asked for." as the
        # only explanation.
        store.forget(user.key, medium, item_key)
        log.info("want reopened user=%s medium=%s key=%s: it arrived once and "
                 "is no longer in the library", user.key, medium, item_key)

    if medium in (media.MOVIE, media.SERIES):
        index = media.owned()
        owned = index.movie_tmdb if medium == media.MOVIE else index.series_tvdb
        if _provider_id(item_key) in owned:
            return IN_LIBRARY, "Already in the library."

    price = media.cost(medium, unit)
    remaining = allowance(user, medium)
    if remaining is not None and remaining < price:
        cap = daily_cap(user, medium)
        log.warning("want denied user=%s medium=%s key=%s reason=daily-cap "
                    "cost=%d remaining=%d cap=%d", user.key, medium, item_key,
                    price, remaining, cap)
        raise Denied(_cap_message(found, unit, price, remaining, cap))

    log.info("want user=%s medium=%s unit=%s key=%s cost=%d remaining=%s",
             user.key, medium, unit, item_key, price,
             "uncapped" if remaining is None else remaining)
    result = _add(medium, unit, item_key, hit, user, choice)
    if not result.ok:
        log.warning("want refused user=%s key=%s reason=%s",
                    user.key, item_key, result.message)
        raise Denied(result.message)

    backend_id = result.backend_id
    if not result.created:
        # Only our own ledger can prove that an existing household row is ours.
        backend_id = store.backend_for(medium, item_key)
        price = 0

    store.record(
        user.key, medium, item_key, unit,
        # What the backend resolved it to is preferred over what the caller
        # sent: it is the spelling the library will carry when it lands.
        result.title or hit.get("title") or "",
        result.year or str(hit.get("year") or ""),
        price, backend_id, seasons=result.seasons)
    log.info("want accepted user=%s key=%s backend_id=%s message=%r",
             user.key, item_key, result.backend_id, result.message)
    return ON_ITS_WAY, result.message


def _still_held(row, medium: str) -> bool:
    """Whether a fulfilled request's media are still in the library.

    Presence, and deliberately not `_arrived`. That one asks whether a
    whole-series request has *finished*, which needs Sonarr's aired total and
    answers no whenever that total cannot be had -- so asking it here would
    read every fulfilled series as gone and hand the lot back to Sonarr.

    Unknown counts as held, everywhere. Reopening on an unknown re-acquires
    something the library still has; refusing on one costs a second tap once
    the answer is available again. The second is much the cheaper mistake.
    """
    key = row["item_key"]
    provider_id = key.split(":", 1)[1] if ":" in key else ""
    if medium in (media.MOVIE, media.SERIES):
        if not provider_id:
            return True
        try:
            index = media.owned()
        except jellyfin.JellyfinUnavailable:
            return True
        return provider_id in (index.movie_tmdb if medium == media.MOVIE
                               else index.series_tvdb)
    # Music is buskarr's to answer: it placed the file and holds the exact
    # identity it placed it under, which is the only handle on it there is.
    reported = buskarr.state(row["backend_id"])
    return reported is None or reported.get("state") == "have"


def _add(medium: str, unit: str, item_key: str, hit: dict,
         user: jellyfin.User, choice: seasons.Seasons | None = None):
    """Hand one thing to whichever tool acquires that medium."""
    if medium == media.MOVIE:
        return radarr.add(_provider_id(item_key), hit.get("title", ""),
                          str(hit.get("year") or ""))
    if medium == media.SERIES:
        return sonarr.add(_provider_id(item_key), hit.get("title", ""),
                          choice=choice)
    # The name, not the ledger key: buskarr renders `requested_by` in its own
    # queue table for a person to read, and an account id says nothing there.
    return buskarr.add(unit, hit, user.name)


def _provider_id(item_key: str) -> str:
    """The bare id out of a prefixed ledger key: `tmdb:1234` -> `1234`."""
    return item_key.split(":", 1)[1] if ":" in item_key else item_key


def _cap_message(found: media.Medium, unit: str, price: int,
                 remaining: int, cap: int) -> str:
    """Why the request was refused, in terms of what was actually asked for.

    `cap` is this account's allowance, which is not always the configured one.
    Told "ask for an album instead" by a message reading the setting, somebody
    on a lowered cap would be refused for that too.
    """
    if cap <= 0:
        return (f"This account cannot ask for {found.label.lower()}. "
                "A keyholder can give it an allowance.")
    if price > cap:
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


#: "No one has asked buskarr about this row yet", which is a different thing from
#: buskarr having been asked and been unable to say. ``state()`` returns None for
#: the second, so using None for both meant an unreachable buskarr was probed twice
#: per music row -- once by the caller, once again inside ``_arrived`` -- and each
#: probe carries its own timeout. Three rows cost six waits during an outage.
NOT_ASKED = object()


def _state(row, medium: str, index: jellyfin.Owned | None = None,
           reported: dict | None | object = NOT_ASKED,
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
             reported: dict | None | object = NOT_ASKED,
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
    if reported is NOT_ASKED:
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
        # Which seasons were asked for, so the list can say it. Absent on a
        # row from before the choice existed, which was asked for whole.
        asked = seasons.decode(row["seasons"] if "seasons" in row.keys() else "")
        if asked is not None:
            described["seasons"] = asked.as_json()
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
    # The household lock for this item, held across the count and the stop.
    # release() is atomic in itself, but _stop ran after its transaction closed,
    # so a request admitted in that gap was killed by this cancellation while
    # keeping its ledger row and the allowance it had spent -- invisible to
    # whoever asked, and unexplainable.
    with store.key_lock("item", medium, item_key):
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
    return True, "Taken off your list. Anything already downloaded is kept."


def _stop(medium: str, row) -> bool:
    """Call the acquisition off. Never removes anything already downloaded."""
    backend_id = row["backend_id"]
    if not backend_id:
        return True
    if medium == media.MOVIE:
        return radarr.cancel(backend_id)
    if medium == media.SERIES:
        return sonarr.cancel(backend_id)
    return buskarr.cancel(backend_id)
