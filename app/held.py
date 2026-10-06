"""Asks past somebody's daily allowance, waiting for a keyholder.

The allowance stays the whole policy. Up to it, an ask goes straight through;
past it, where the server allows it and the caller can show what happened, it
waits here instead of being refused. A limit of 0 then means everything that
account asks for of that kind waits, and a keyholder, who has no limit, never
waits for anybody.

This is the part every request path shares: whether holding is on, what the
asker is told, writing the ask down and saying so, settling everybody else
waiting on a thing once it has been asked for, and how a held ask reads on
somebody's list. Saying yes or no is the keyholder's side, in `approvals`.
"""
import json
import threading
import time

import httpx

from . import artwork, config, episodes, jellyfin, logs, media, seasons, store

log = logs.get("held")

#: A held ask, as a request list says it. Only ever sent to a caller that
#: asked for held asks: a client that predates them knows three states and
#: no more.
WAITING_FOR_APPROVAL = "waiting_for_approval"
#: A held ask a keyholder said no to.
DECLINED = "declined"
STATES = (WAITING_FOR_APPROVAL, DECLINED)

#: How long the names of the people who approve are reused. One `/Users` read
#: per hold is nothing at a household's rate; one per list drawn is not.
APPROVERS_TTL_SECONDS = 300

_approvers: tuple[float, list[str]] | None = None
_approvers_guard = threading.Lock()


def enabled() -> bool:
    """Whether this server holds asks past the allowance at all."""
    return config.APPROVALS


def approvers() -> list[str]:
    """Who can approve: the names of Jellyfin's administrators.

    An empty list when Jellyfin cannot say and nothing was cached, which the
    sentences below turn into "a keyholder" rather than a refusal: an ask is
    not worth failing over the name of the person it goes to.
    """
    global _approvers
    with _approvers_guard:
        cached = _approvers
    if cached and time.monotonic() - cached[0] < APPROVERS_TTL_SECONDS:
        return cached[1]
    try:
        names = sorted((account.name for account in jellyfin.accounts()
                        if account.is_admin), key=str.casefold)
    except jellyfin.JellyfinUnavailable:
        return cached[1] if cached else []
    with _approvers_guard:
        _approvers = (time.monotonic(), names)
    return names


def who_approves() -> str:
    """The approvers as a sentence names them: "Matt", "Matt or Alex"."""
    names = approvers()
    if not names:
        return "a keyholder"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def held_message(label: str, cap: int | None) -> str:
    """What somebody is told when their ask waits instead of going through."""
    noun = label.lower()
    who = who_approves()
    if cap == 0:
        return (f"Asking for {noun} needs approval on this account, so it has "
                f"gone to {who} to approve.")
    return (f"That is past today's limit for {noun}, so it has gone to {who} "
            "to approve.")


def repeat_message() -> str:
    """Asking again for something already waiting.

    Starts the way every other repeat does. An imported list reads "Already "
    as already asked for, which is what this is.
    """
    return f"Already asked for. It is waiting for {who_approves()} to approve it."


def hold(user: jellyfin.User, medium: str, item_key: str, unit: str,
         hit: dict, choice: str, label: str, cap: int | None,
         detail: str = "") -> tuple[str, str]:
    """Write an ask past the allowance down, and tell whoever approves.

    Returns the state and sentence the asker gets. `hit` is kept whole so
    approving it later hands the acquisition tool what it would have had
    now; `detail` is how the notice describes it beyond its title.
    """
    title = str(hit.get("title") or "")
    is_new = store.hold(
        user.key, medium, item_key, unit, title, str(hit.get("year") or ""),
        artwork.https_url(hit.get("imageUrl")) or "",
        str(hit.get("overview") or "").strip(), _keepable(hit), choice)
    log.info("want held user=%s medium=%s unit=%s key=%s new=%s",
             user.key, medium, unit, item_key, is_new)
    if is_new:
        notify(user.name, title or item_key, label, detail)
    return WAITING_FOR_APPROVAL, held_message(label, cap)


def _keepable(hit: dict) -> dict:
    """The parts of a hit that survive being written down as JSON."""
    plain = (str, int, float, bool, type(None), list, dict)
    return {key: value for key, value in hit.items() if isinstance(value, plain)}


def hit_of(row) -> dict:
    """The hit a held ask was made with."""
    try:
        found = json.loads(row["hit"] or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def notify(asker: str, title: str, label: str,
           detail: str = "") -> threading.Thread | None:
    """Say that something is waiting, beside the ask rather than in front of it.

    Posted in ntfy's form. A thread of its own, so a notice service that is
    down or slow costs the person asking nothing; the thread is returned so a
    test can wait for it.
    """
    url = config.APPROVAL_NOTIFY_URL
    if not url:
        return None
    described = f"{title} ({detail})" if detail else title
    body = f"{asker} asked for {described}, past today's limit for {label.lower()}."
    headers = {"Title": "A request to approve",
               "Content-Type": "text/plain; charset=utf-8"}
    if config.PAGES_URL:
        headers["Click"] = config.PAGES_URL + "/approvals"
    if config.APPROVAL_NOTIFY_TOKEN:
        headers["Authorization"] = f"Bearer {config.APPROVAL_NOTIFY_TOKEN}"
    thread = threading.Thread(target=_post, args=(url, body, headers),
                              name="approval-notice", daemon=True)
    thread.start()
    return thread


def _post(url: str, body: str, headers: dict) -> None:
    try:
        httpx.post(url, content=body.encode(), headers=headers,
                   timeout=config.APPROVAL_NOTIFY_TIMEOUT_SECONDS
                   ).raise_for_status()
        log.info("approval notice sent")
    except httpx.HTTPError as exc:
        log.warning("approval notice not delivered: %s", exc)


def ride_along(medium: str, item_key: str, record, except_user_key: str) -> list[str]:
    """Settle everybody else waiting on something that has just been asked for.

    Whoever asked within their allowance, or a keyholder, has just set it
    coming, so nobody else's ask for the same thing needs approving: each gets
    a request at no cost, the way a second asker within the allowance always
    has. `record(row)` writes that request, because a book is written down
    differently from the rest. Returns whose asks were settled.

    Writes directly instead of asking again on their behalf. The caller holds
    this item's lock, and asking again would take it a second time; it is not
    reentrant.
    """
    settled = []
    for row in store.waiting_on(medium, item_key):
        if row["user_key"] == except_user_key:
            continue
        record(row)
        store.drop_held(row["user_key"], medium, item_key)
        settled.append(row["user_key"])
    if settled:
        log.info("held asks settled medium=%s key=%s for=%s: asked for "
                 "meanwhile", medium, item_key, settled)
    return settled


def settle_owned(medium: str, item_key: str) -> int:
    """Something waited on is in the library already, so nobody need approve it."""
    rows = store.waiting_on(medium, item_key)
    for row in rows:
        store.drop_held(row["user_key"], medium, item_key)
    if rows:
        log.info("held asks settled medium=%s key=%s count=%d: already in "
                 "the library", medium, item_key, len(rows))
    return len(rows)


def described_for(user: jellyfin.User, medium: str | None = None) -> list[dict]:
    """This account's held asks, as its request list shows them.

    A no stays on the list as long as an arrival does, and is then forgotten.
    """
    cutoff = time.time() - config.ARRIVED_VISIBLE_HOURS * 3600
    store.prune_declined(cutoff)
    return [describe(row) for row in store.held_for(user.key, medium, cutoff)]


def describe(row) -> dict:
    """One held ask in the shape of a request on somebody's list."""
    out = {
        "itemKey": row["item_key"],
        "medium": row["medium"],
        "unit": row["unit"],
        "title": row["title"],
        "year": row["year"],
        "state": (WAITING_FOR_APPROVAL if row["state"] == store.HELD_WAITING
                  else DECLINED),
        "requestedAt": row["asked_at"],
    }
    out.update(artwork.art(row["image_url"]))
    if row["overview"]:
        out["overview"] = row["overview"]
    if row["medium"] == media.SERIES:
        asked = seasons.decode(row["choice"])
        if asked is not None:
            out["seasons"] = asked.as_json()
    elif row["medium"] == media.PODCAST:
        asked = episodes.decode(row["choice"])
        if asked is not None:
            out["episodes"] = asked.as_json()
    elif row["medium"] == media.BOOK and row["unit"] == "series":
        # The books of a series that did not fit, waiting as one ask.
        count = hit_of(row).get("count")
        if isinstance(count, int) and count > 0:
            out["bookCount"] = count
    if row["state"] == store.HELD_DECLINED:
        out["decidedAt"] = row["decided_at"]
        if row["decided_by"]:
            out["decidedBy"] = row["decided_by"]
        if row["reason"]:
            out["reason"] = row["reason"]
    return out


def withdraw(user: jellyfin.User, medium: str,
             item_key: str) -> tuple[bool, str] | None:
    """Take a held ask off somebody's list, or None if they have none.

    Nothing was handed to anything, so this is only the row. A request that
    did reach an acquisition tool is the caller's to call off, and is left to
    it when there is one as well.
    """
    row = store.held(user.key, medium, item_key)
    if row is None:
        return None
    store.drop_held(user.key, medium, item_key)
    if store.get(user.key, medium, item_key) is not None:
        # A request that reached a tool as well: calling that off is the
        # caller's, and the held row should not outlive it.
        return None
    log.info("held ask withdrawn user=%s medium=%s key=%s state=%s",
             user.key, medium, item_key, row["state"])
    if row["state"] == store.HELD_WAITING:
        return True, "Taken off your list. Nobody had approved it yet."
    return True, "Taken off your list."


def capability(user: jellyfin.User) -> dict:
    """The `approvals` block a client reads to know asks past the limit wait.

    `supported` is the server's setting; a client that sees it true says so
    on its asks to be held rather than refused. `approvers` is who a waiting
    ask goes to, for the sentence a client says before asking. `waiting` is
    for a keyholder only: how many things are waiting for an answer.
    """
    block: dict = {"supported": enabled(), "states": list(STATES)}
    if enabled():
        block["approvers"] = approvers()
        if user.is_admin:
            block["waiting"] = store.waiting_count()
    return block
