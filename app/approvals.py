"""A keyholder's answers to the asks waiting for them.

Approving asks for the thing again on the asker's behalf, through the same
request path as everything else, with the allowance set aside and nothing
charged. So every rule that guards an acquisition -- already in the library,
already asked for, the household's one row in the acquisition tool -- applies
to an approval exactly as it would have to the ask. Anybody else waiting on
the same thing is settled by that one ask; see `held.ride_along`.

Declining keeps the ask on the asker's list with the answer, for as long as an
arrival stays there, so a no is something they hear rather than a request
that silently vanished.

Leaving it is an answer too. Once the asker's allowance has room for it, a
waiting ask goes through by itself, as an ordinary ask of theirs; see
`release_when_room`.
"""
import threading
import time

from . import config, held, jellyfin, logs, media, seasons, store, wants
from .books import adapter as books
from .books import series as book_series
from .books import wants as book_wants

log = logs.get("approvals")

#: How long after starting the first pass runs, so a restart is settled first.
RELEASE_FIRST_DELAY_SECONDS = 120

#: How long an ask its acquisition tool refused is left before it is tried
#: again by itself. Kept in memory: a restart tries it once more, which costs
#: one more refusal and nothing else.
REFUSED_RETRY_SECONDS = 6 * 3600

_refused: dict[tuple[str, str, str], float] = {}
_refused_guard = threading.Lock()


class NothingWaiting(LookupError):
    """Nothing is waiting under that name: answered already, or withdrawn."""


def _names() -> dict[str, str]:
    """Account key to display name, or nothing when Jellyfin cannot say."""
    try:
        return {account.key: account.name for account in jellyfin.accounts()}
    except jellyfin.JellyfinUnavailable:
        return {}


def waiting() -> list[dict]:
    """Everything waiting, one entry per thing, with everybody who asked.

    The earliest ask first: it has been waiting longest. Each entry is shaped
    like a request on a list, plus `askedBy`, and for a book series the
    titles that would be asked for. Each asker says whether their ask goes
    through by itself once their allowance frees up, which tells a keyholder
    what can be left.
    """
    accounts = _accounts() or {}
    names = {key: account.name for key, account in accounts.items()}
    grouped: dict[tuple[str, str], dict] = {}
    for row in store.all_waiting():
        key = (row["medium"], row["item_key"])
        entry = grouped.get(key)
        if entry is None:
            entry = held.describe(row)
            entry.pop("state", None)
            entry["askedAt"] = entry.pop("requestedAt")
            entry["askedBy"] = []
            titles = held.hit_of(row).get("titles")
            if row["unit"] == books.SERIES_UNIT and isinstance(titles, list):
                entry["bookTitles"] = [t for t in titles if isinstance(t, str)]
            grouped[key] = entry
        asker = accounts.get(row["user_key"])
        entry["askedBy"].append({
            "id": row["user_key"],
            "name": names.get(row["user_key"], "Somebody no longer here"),
            "askedAt": row["asked_at"],
            "releasesOnItsOwn": asker is not None and _goes_through(asker, row),
        })
    return list(grouped.values())


def approve(keyholder: jellyfin.User, medium: str, item_key: str,
            seasons_choice: seasons.Seasons | None = None) -> dict:
    """Say yes to everybody waiting on one thing. Raises NothingWaiting.

    The earliest asker's ask is made again for them; that is what hands the
    thing to its acquisition tool, and it settles everybody else waiting on
    it. Where it did not -- the first asker turned out to have it already --
    the next one's is made, until nobody is left waiting. `seasons_choice`
    replaces what a series was asked for with, for a keyholder who would
    rather start with less.

    Raises `wants.Denied` (or `TryLater`) when the acquisition tool refuses,
    and the asks go on waiting: a yes that did not reach anything has not
    been given.
    """
    first = store.waiting_on(medium, item_key)
    if not first:
        raise NothingWaiting("Nothing is waiting for that any more.")
    names = _names()
    state, message = "", ""
    answered: list[str] = []
    while rows := store.waiting_on(medium, item_key):
        row = rows[0]
        try:
            asker = jellyfin.account(row["user_key"])
        except LookupError:
            # The account has gone from Jellyfin. Nobody is left to give it to.
            store.drop_held(row["user_key"], medium, item_key)
            log.info("held ask dropped user=%s medium=%s key=%s: no such "
                     "account", row["user_key"], medium, item_key)
            continue
        state, message = _ask_again(asker, row, seasons_choice)
        if store.held(row["user_key"], medium, item_key) is not None:
            # Every way through asking again answers the asker's own row; one
            # still here would be asked again for ever.
            log.warning("approval left a held ask in place user=%s medium=%s "
                        "key=%s", row["user_key"], medium, item_key)
            break
    still = {r["user_key"] for r in store.waiting_on(medium, item_key)}
    answered = [r["user_key"] for r in first if r["user_key"] not in still]
    approved_for = [names.get(key, "Somebody no longer here") for key in answered]
    log.info("approved by=%s medium=%s key=%s for=%s state=%s",
             keyholder.key, medium, item_key, answered, state)
    said = f"Approved for {_listed(approved_for)}." if approved_for else ""
    return {"medium": medium, "itemKey": item_key, "state": state,
            "message": " ".join(part for part in (said, message) if part),
            "approvedFor": approved_for}


def _ask_again(asker: jellyfin.User, row,
               seasons_choice: seasons.Seasons | None = None,
               approved: bool = True) -> tuple[str, str]:
    """One held ask, made again on the asker's behalf.

    With the keyholder's yes, free. Without it, as the asker's own ask past
    the limit would be: charged where the allowance has room, and otherwise
    still waiting, exactly as it was.
    """
    medium, item_key, unit = row["medium"], row["item_key"], row["unit"]
    hit = held.hit_of(row)
    how = {"approved": True} if approved else {"over_limit": True}
    if medium == media.SERIES:
        choice = (seasons_choice if seasons_choice is not None
                  else seasons.decode(row["choice"]))
        return wants.want(asker, medium, item_key, unit, hit, choice=choice,
                          **how)
    return wants.want(asker, medium, item_key, unit, hit, **how)


def release_when_room() -> list[dict]:
    """Ask again for every waiting ask its asker's allowance now has room for.

    An ask waits for a keyholder only because it was past the allowance when
    it was made, and the allowance comes back a day after each request it
    counted. Asked for then, the same thing would simply go through, so once
    it fits it does: on the asker's behalf, charged like any ask of theirs,
    whether or not a keyholder ever got to it. A no stays a no; only waiting
    asks go.

    Oldest first for each account and kind of thing, and the first one that
    does not fit holds back the ones behind it, so an artist is not overtaken
    for ever by single songs. An ask no day's allowance could cover is the
    keyholder's alone, so it holds nothing back.

    Returns one entry per ask that went, for the log and the tests.
    """
    if not held.enabled():
        return []
    rows = store.all_waiting()
    if not rows:
        # The usual case, and no reason to ask Jellyfin for anything.
        return []
    accounts = _accounts()
    if accounts is None:
        # Nobody's allowance can be read without Jellyfin. Next pass.
        return []
    went: list[dict] = []
    full: set[tuple[str, str]] = set()
    for row in rows:
        user_key, medium = row["user_key"], row["medium"]
        if (user_key, medium) in full or _resting(row):
            continue
        asker = accounts.get(user_key)
        if asker is None or not _goes_through(asker, row):
            # Gone from Jellyfin, which approving tidies away; or the
            # keyholder's to decide whatever happens.
            continue
        room = _room(asker, medium)
        if room is not None and room < _price(medium, row["unit"]):
            full.add((user_key, medium))
            continue
        try:
            outcome = _release(asker, row)
        except wants.TryLater as exc:
            log.info("held ask not released user=%s medium=%s key=%s, trying "
                     "again next pass: %s", user_key, medium, row["item_key"], exc)
            continue
        except wants.Denied as exc:
            _rest(row)
            log.warning("held ask refused on release user=%s medium=%s key=%s, "
                        "left for a keyholder: %s", user_key, medium,
                        row["item_key"], exc)
            continue
        except jellyfin.JellyfinUnavailable as exc:
            # Nothing after it would get any further. Next pass.
            log.info("held asks pass stopped, Jellyfin is not answering: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 - one ask failing in a way
            # nobody foresaw must not hold back every ask behind it for good.
            _rest(row)
            log.warning("held ask failed on release user=%s medium=%s key=%s, "
                        "left for a keyholder: %r", user_key, medium,
                        row["item_key"], exc)
            continue
        if outcome is None:
            # Something else spent the room between the check and the ask.
            full.add((user_key, medium))
            continue
        log.info("held ask went through user=%s medium=%s key=%s: the "
                 "allowance had room (%s)", user_key, medium, row["item_key"],
                 outcome)
        went.append({"userKey": user_key, "medium": medium,
                     "itemKey": row["item_key"], "title": row["title"],
                     **outcome})
    return went


def _release(asker: jellyfin.User, row) -> dict | None:
    """Ask again for one waiting ask, charged. None when nothing went."""
    if row["medium"] == media.BOOK and row["unit"] == books.SERIES_UNIT:
        return _release_series(asker, row)
    state, _ = _ask_again(asker, row, approved=False)
    # Every way through answers the asker's row: asked for, already in the
    # library, or settled by somebody else's ask. Still there means it was a
    # repeat, which is what an allowance with no room left answers.
    if store.held(row["user_key"], row["medium"], row["item_key"]) is not None:
        return None
    return {"state": state}


def _release_series(asker: jellyfin.User, row) -> dict | None:
    """As many books of a waiting series as the allowance has room for.

    Asking for the series stops by itself where the allowance does. The rest
    go on waiting as the same ask, in the same place, carrying the smaller
    count, so the series never asks for more books than the tap that started
    it.
    """
    hit = held.hit_of(row)
    count = hit.get("count")
    if not isinstance(count, int) or count <= 0:
        count = config.SERIES_WANT_LIMIT
    try:
        outcome = book_series.want_series(
            asker, row["item_key"], hit.get("anchorItemId") or None,
            limit=count)
    except book_series.Unavailable as exc:
        raise wants.TryLater(str(exc)) from exc
    except (book_series.NotASeries, book_series.Unresolvable) as exc:
        raise wants.Denied(str(exc)) from exc
    asked = outcome["requested"] + outcome["failed"]
    left = max(0, min(count - len(asked), outcome["heldBackCount"]))
    if left == 0:
        store.drop_held(row["user_key"], media.BOOK, row["item_key"])
    else:
        named = {book["title"] for book in asked}
        titles = [title for title in hit.get("titles") or []
                  if isinstance(title, str) and title not in named][:left]
        store.rewrite_held_hit(row["user_key"], media.BOOK, row["item_key"],
                               {**hit, "count": left, "titles": titles})
        if not asked:
            return None
    return {"booksAsked": len(outcome["requested"]), "booksLeft": left}


def _accounts() -> dict[str, jellyfin.User] | None:
    """Every account by its ledger key, or None when Jellyfin cannot say."""
    try:
        return {account.key: account for account in jellyfin.accounts()}
    except jellyfin.JellyfinUnavailable:
        return None


def _price(medium: str, unit: str) -> int:
    """What one waiting ask needs from the allowance to go.

    A book series goes a book at a time, so one book's worth is enough.
    """
    return 1 if medium == media.BOOK else media.cost(medium, unit)


def _room(asker: jellyfin.User, medium: str) -> int | None:
    """What is left of the asker's allowance, or None when they have no limit.

    Books keep their own arithmetic, so they are asked there.
    """
    if medium == media.BOOK:
        return book_wants.allowance(asker)
    return wants.allowance(asker, medium)


def _goes_through(asker: jellyfin.User, row) -> bool:
    """Whether this waiting ask can ever go through without a keyholder.

    An account with no limit at all (made a keyholder since) always can.
    """
    medium = row["medium"]
    cap = (book_wants.daily_cap(asker) if medium == media.BOOK
           else wants.daily_cap(asker, medium))
    return cap is None or held.goes_through_by_itself(cap, _price(medium, row["unit"]))


def _resting(row) -> bool:
    """Whether this ask was refused on its last release and is left alone."""
    key = (row["user_key"], row["medium"], row["item_key"])
    with _refused_guard:
        until = _refused.get(key)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        del _refused[key]
        return False


def _rest(row) -> None:
    key = (row["user_key"], row["medium"], row["item_key"])
    with _refused_guard:
        _refused[key] = time.monotonic() + REFUSED_RETRY_SECONDS


def watch() -> None:
    """Start the pass that lets waiting asks through once they fit."""
    threading.Thread(target=_release_loop, name="held-release",
                     daemon=True).start()
    log.info("held asks: checked against their allowance every %d s",
             config.HELD_RELEASE_SECONDS)


def _release_loop() -> None:
    time.sleep(RELEASE_FIRST_DELAY_SECONDS)
    while True:
        try:
            went = release_when_room()
            if went:
                log.info("held asks: %d went through by themselves", len(went))
        except Exception as exc:  # noqa: BLE001 - the thread must not die, or
            # waiting asks silently stop going through.
            log.warning("held asks pass failed: %s", exc)
        time.sleep(config.HELD_RELEASE_SECONDS)


def decline(keyholder: jellyfin.User, medium: str, item_key: str,
            reason: str = "") -> dict:
    """Say no to everybody waiting on one thing. Raises NothingWaiting."""
    keys = store.decline_held(medium, item_key, reason.strip(), keyholder.name)
    if not keys:
        raise NothingWaiting("Nothing is waiting for that any more.")
    if medium == media.BOOK:
        # A no to the rest of a series is a no to its next books as well.
        # Followed on, each new one would wait for the same no again.
        for key in keys:
            store.unfollow_series(key, item_key)
    names = _names()
    declined_for = [names.get(key, "Somebody no longer here") for key in keys]
    log.info("declined by=%s medium=%s key=%s for=%s reason=%r",
             keyholder.key, medium, item_key, keys, reason.strip())
    return {"medium": medium, "itemKey": item_key,
            "declinedFor": declined_for,
            "message": "Declined for " + _listed(declined_for) + "."}


def _listed(names: list[str]) -> str:
    """Names as a sentence lists them: "Alex", "Alex and Rebecca"."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
