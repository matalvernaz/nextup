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
"""
from . import episodes, held, jellyfin, logs, media, seasons, store, wants
from .books import adapter as books

log = logs.get("approvals")


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
    titles that would be asked for.
    """
    names = _names()
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
        entry["askedBy"].append({
            "id": row["user_key"],
            "name": names.get(row["user_key"], "Somebody no longer here"),
            "askedAt": row["asked_at"],
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
               seasons_choice: seasons.Seasons | None) -> tuple[str, str]:
    """One held ask, made again on the asker's behalf with the keyholder's yes."""
    medium, item_key, unit = row["medium"], row["item_key"], row["unit"]
    hit = held.hit_of(row)
    if medium == media.SERIES:
        choice = (seasons_choice if seasons_choice is not None
                  else seasons.decode(row["choice"]))
        return wants.want(asker, medium, item_key, unit, hit, choice=choice,
                          approved=True)
    if medium == media.PODCAST:
        return wants.want(asker, medium, item_key, unit, hit,
                          episodes_choice=episodes.decode(row["choice"]),
                          approved=True)
    return wants.want(asker, medium, item_key, unit, hit, approved=True)


def decline(keyholder: jellyfin.User, medium: str, item_key: str,
            reason: str = "") -> dict:
    """Say no to everybody waiting on one thing. Raises NothingWaiting."""
    keys = store.decline_held(medium, item_key, reason.strip(), keyholder.name)
    if not keys:
        raise NothingWaiting("Nothing is waiting for that any more.")
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
