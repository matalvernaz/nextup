"""Telling somebody, on their phone, what happened to something they asked for.

An ask past the daily limit waits for a keyholder, and until now the only way
to learn what became of it was to open the request list and look. Now a
keyholder's yes or no, an ask going through by itself once the allowance had
room, somebody else's ask settling one that waited, a new book asked for from a
followed series, what they asked for reaching the library, and the audio
description they asked for being ready are each said to the person, on the
phones where they use EchoFin.

Jellyfin delivers: Matt's fork keeps the phones each account signed in on,
and the key Apple wants, for its new-episode notifications already
(`jellyfin.notify_user`). This decides who is told what. Nobody is told
anything until they turn it on (`PUT /request-notices`), and a server whose
Jellyfin cannot deliver says so in its capabilities and is never asked.

Every notice is sent beside whatever caused it, on a thread of its own, so a
slow or absent Jellyfin costs the request nothing.
"""
import threading
import time

import httpx

from . import config, describarr, jellyfin, logs, store

log = logs.get("notices")

#: The per-account choice, in `user_settings`: "on", or absent for off.
SETTING = "REQUEST_NOTICES"
#: What kind of notice this is, for the phone to know what a tap opens.
KIND = "requests"
#: How long a Jellyfin that said it cannot send is left alone.
UNAVAILABLE_RETRY_SECONDS = 3600
#: More arrivals at once than this are said in one notice rather than one each.
MOST_ARRIVALS_NAMED = 3
#: How long after starting the first look for arrivals runs.
ARRIVALS_FIRST_DELAY_SECONDS = 180
#: The kind of notice that says a description somebody asked for is ready. Not
#: KIND: the phone opens the request list for that one, and a description is
#: not on it.
DESCRIBED_KIND = "described"
#: How long a title somebody asked to have described is watched for. One not
#: done by then is forgotten rather than asked about for ever.
DESCRIPTION_WAIT_DAYS = 30
#: What describarr says of a file whose description is there.
DESCRIPTION_READY = ("described", "already_described")
#: What it says once it has given up on one: no source had it, the one found
#: was refused, or the work failed. Anything else is still to come.
DESCRIPTION_GAVE_UP = ("no_match", "rejected", "error")

_unavailable_until = 0.0
_guard = threading.Lock()

#: What a state means, said after what happened.
_STATE_SENTENCES = {
    "on_its_way": "It is on its way.",
    "still_looking": "It is being looked for.",
    "in_library": "It is already in the library.",
}


def supported() -> bool:
    """Whether this server's Jellyfin can put a notice on a phone at all."""
    return config.REQUEST_NOTICES


def wanted(user_key: str) -> bool:
    """Whether this account asked to be told."""
    return store.user_setting(user_key, SETTING) == "on"


def set_wanted(user_key: str, on: bool) -> None:
    store.put_user_setting(user_key, SETTING, "on" if on else "")


def capability(user: jellyfin.User) -> dict:
    """The `requestNotices` block: whether this server can, and whether they want it."""
    return {"supported": supported(), "on": supported() and wanted(user.key)}


def tell(user_key: str, title: str, body: str, medium: str,
         item_key: str) -> threading.Thread | None:
    """Put one notice on this account's phones, if they asked for them.

    Returns the thread sending it, so a test can wait for it, or None when
    nothing is sent.
    """
    return _deliver(user_key, KIND, title, body,
                    {"medium": medium[:200], "itemKey": item_key[:200]})


def _deliver(user_key: str, kind: str, title: str, body: str,
             data: dict) -> threading.Thread | None:
    """Send one notice of this kind, the way every notice here is sent.

    Nothing where the server cannot send or the account did not ask, nothing
    for an hour after Jellyfin said it cannot send, and on a thread of its own.
    """
    if not supported() or not wanted(user_key):
        return None
    with _guard:
        if time.monotonic() < _unavailable_until:
            return None
    payload = {"Title": title[:120], "Body": body[:600], "Kind": kind,
               "ThreadId": kind, "Data": data}
    thread = threading.Thread(target=_send, args=(user_key, payload),
                              name="request-notice", daemon=True)
    thread.start()
    return thread


def _send(user_key: str, payload: dict) -> None:
    global _unavailable_until
    try:
        result = jellyfin.notify_user(user_key, payload)
    except jellyfin.PushUnavailable as exc:
        with _guard:
            _unavailable_until = time.monotonic() + UNAVAILABLE_RETRY_SECONDS
        log.warning("request notices: Jellyfin cannot send notifications, so none "
                    "are tried for an hour: %s", exc)
        return
    except (LookupError, jellyfin.JellyfinUnavailable) as exc:
        log.warning("request notice not delivered user=%s: %s", user_key, exc)
        return
    log.info("request notice user=%s %r sent=%s failed=%s dropped=%s", user_key,
             payload["Title"], result.get("Sent"), result.get("Failed"),
             result.get("Dropped"))


def _named(title: str, item_key: str) -> str:
    return title.strip() or item_key


def approved(user_keys: list[str], keyholder: str, medium: str, item_key: str,
             title: str, state: str) -> None:
    """A keyholder said yes, for everybody it answered."""
    body = f"{keyholder} approved {_named(title, item_key)}."
    said = _STATE_SENTENCES.get(state)
    if said:
        body += " " + said
    for user_key in user_keys:
        tell(user_key, "Request approved", body, medium, item_key)


def declined(user_keys: list[str], keyholder: str, medium: str, item_key: str,
             title: str, reason: str) -> None:
    """A keyholder said no, with their reason when they gave one."""
    body = f"{keyholder} said no to {_named(title, item_key)}."
    reason = reason.strip()
    if reason:
        body += " " + (reason if reason[-1] in ".!?" else reason + ".")
    for user_key in user_keys:
        tell(user_key, "Request declined", body, medium, item_key)


def went_through(user_key: str, medium: str, item_key: str, title: str,
                 outcome: dict) -> None:
    """A waiting ask went through by itself, once the allowance had room."""
    named = _named(title, item_key)
    if "booksAsked" in outcome:
        asked, left = outcome["booksAsked"], outcome.get("booksLeft", 0)
        if not asked:
            return
        body = (f"{asked} more {'book' if asked == 1 else 'books'} of {named} "
                f"{'was' if asked == 1 else 'were'} asked for, now that your "
                "allowance has room.")
        if left:
            body += (f" The other {left} {'waits' if left == 1 else 'wait'} "
                     "for the next room.")
    elif outcome.get("state") == "in_library":
        body = f"{named} is already in the library, so nothing more was needed."
    else:
        body = (f"{named} went through by itself, now that your allowance has "
                "room. It is on its way.")
    tell(user_key, "Request went through", body, medium, item_key)


def settled(user_key: str, medium: str, item_key: str, title: str) -> None:
    """Somebody else asked for what this account was waiting on, so it is coming."""
    tell(user_key, "Request on its way",
         f"{_named(title, item_key)} is on its way. Somebody else asked for it "
         "too, so it no longer needs approving.", medium, item_key)


def arrived(user_key: str, medium: str, items: list[tuple[str, str]]) -> None:
    """Things this account asked for reached the library. `items` is (key, title).

    One notice each, up to a few; more at once are one notice that names the
    first few and counts the rest, so a busy night is one buzz.
    """
    if not items:
        return
    heading = "Ready to watch" if medium in ("movie", "series") else "Ready to listen"
    if len(items) <= MOST_ARRIVALS_NAMED:
        for item_key, title in items:
            tell(user_key, heading, f"{_named(title, item_key)} is in the library.",
                 medium, item_key)
        return
    named = ", ".join(_named(title, item_key)
                      for item_key, title in items[:MOST_ARRIVALS_NAMED])
    more = len(items) - MOST_ARRIVALS_NAMED
    tell(user_key, heading,
         f"{len(items)} things you asked for are in the library: {named} and "
         f"{more} more.", medium, items[0][0])


def check_arrivals() -> int:
    """Look at the requests of everybody who turned notices on. Returns how many.

    Reading somebody's requests is what settles an arrival, and settling one is
    what tells them (`arrived`, called from where arrivals are settled). This
    only makes sure that happens without them opening their list first. The
    descriptions they asked for are looked at after their requests.
    """
    if not supported():
        return 0
    _forget_old_description_asks()
    wanting = [key for key, value in store.setting_by_account(SETTING).items()
               if value == "on"]
    if not wanting:
        return 0
    try:
        accounts = {account.key: account for account in jellyfin.accounts()}
    except jellyfin.JellyfinUnavailable:
        return 0
    # Here rather than at the top: the request path imports this module.
    from . import wants
    looked = 0
    for user_key in wanting:
        account = accounts.get(user_key)
        if account is None:
            continue
        try:
            wants.states(account)
            looked += 1
        except jellyfin.JellyfinUnavailable as exc:
            log.info("arrivals not looked at, Jellyfin is not answering: %s", exc)
            # The descriptions below need it too, so the pass ends here.
            return looked
        except Exception as exc:  # noqa: BLE001 - one account failing must not
            # stop everybody after it hearing about their arrivals.
            log.warning("arrivals not looked at user=%s: %r", user_key, exc)
    _check_descriptions([accounts[key] for key in wanting if key in accounts])
    return looked


def watch() -> None:
    """Start looking for arrivals, where this server can tell anybody about them."""
    if not supported():
        return
    threading.Thread(target=_arrivals_loop, name="request-arrivals",
                     daemon=True).start()
    log.info("request notices: arrivals looked for every %d s",
             config.REQUEST_NOTICE_ARRIVAL_SECONDS)


def _arrivals_loop() -> None:
    time.sleep(ARRIVALS_FIRST_DELAY_SECONDS)
    while True:
        try:
            check_arrivals()
        except Exception as exc:  # noqa: BLE001 - the thread must not die, or
            # arrivals silently stop being told.
            log.warning("arrivals pass failed: %s", exc)
        time.sleep(config.REQUEST_NOTICE_ARRIVAL_SECONDS)


def new_in_series(user_key: str, series_title: str, book_title: str,
                  asin: str, state: str) -> None:
    """A book came out in a series this account follows, and was asked for."""
    if state == "on_its_way":
        body = f"{book_title} is out, so it has been asked for."
    elif state == "waiting_for_approval":
        body = (f"{book_title} is out. It is past today's limit, so it waits "
                "for approval, or goes through by itself when your allowance "
                "frees up.")
    else:
        return
    tell(user_key, f"New in {series_title}", body, "book", asin)


def asked_for_description(user_key: str, item_id: str, item: dict) -> None:
    """Remember who asked for a title's description, to tell them when it is ready.

    A film or an episode only. describarr says what became of one file
    (`describarr.OUTCOME_TYPES`), and a series or a season is a folder with an
    answer for each episode, which no one notice could follow. Remembered
    whether or not this account has turned notices on yet, so turning them on
    after asking still tells them; never where this server sends none at all.
    """
    if not supported():
        return
    kind = item.get("Type", "")
    if kind not in describarr.OUTCOME_TYPES:
        return
    title = item.get("Name", "")
    series = item.get("SeriesName", "")
    if kind == "Episode" and series:
        title = f"{series}, {title}"
    store.ask_description(user_key, item_id, title)


def description_ready(user_key: str, item_id: str,
                      title: str) -> threading.Thread | None:
    """The description this account asked for is there.

    Returns the thread sending it, or None when nothing is sent. The item goes
    as `item`, because the fork refuses `itemId` from any service but itself.
    """
    return _deliver(user_key, DESCRIBED_KIND, "Audio description is ready",
                    f"{_named(title, item_id)} has audio description now.",
                    {"item": item_id[:200]})


def _forget_old_description_asks() -> None:
    """Forget every ask for a description older than DESCRIPTION_WAIT_DAYS.

    Every account's, not only those the pass looks at: an ask from somebody
    who never turned notices on, or whose account is gone, would otherwise be
    kept for ever.
    """
    cutoff = time.time() - DESCRIPTION_WAIT_DAYS * 24 * 3600
    forgotten = store.prune_description_asks(cutoff)
    if forgotten:
        log.info("description asks forgotten after %d days of waiting: %d",
                 DESCRIPTION_WAIT_DAYS, forgotten)


def _check_descriptions(accounts: list[jellyfin.User]) -> None:
    """Tell these accounts about the descriptions they asked for that are ready.

    describarr says what became of each title, by the file Jellyfin has for
    it. Either of them not answering ends this look with every ask kept, for
    the next pass to ask again. Anything else going wrong with one title costs
    only that title, and only this time: describarr answering with an error
    about one file is that file's trouble, not describarr being away, and
    ending the look there would hold up every title after it on every pass.
    """
    if not describarr.configured():
        return
    for account in accounts:
        for ask in store.description_asks(account.key):
            try:
                _check_description(account, ask)
            except jellyfin.JellyfinUnavailable as exc:
                log.info("descriptions not looked at, Jellyfin is not answering: %s",
                         exc)
                return
            except httpx.TransportError as exc:
                log.info("descriptions not looked at, describarr is not answering: %s",
                         exc)
                return
            except Exception as exc:  # noqa: BLE001 - one title failing must not
                # stop everybody after it hearing about theirs.
                log.warning("description not looked at user=%s item=%s: %r",
                            account.key, ask["item_id"], exc)


def _check_description(account: jellyfin.User, ask) -> None:
    """Tell, forget, or keep waiting on one title somebody asked to have described."""
    item_id = ask["item_id"]
    item = jellyfin.item_with_path(item_id, account.id)
    # Gone from the library, or from the disk: nothing will be described, and
    # describarr answers a file with no path with an error, not an outcome.
    if item is None or not (item.get("Path") or "").strip():
        store.drop_description_ask(account.key, item_id)
        log.info("description ask forgotten, no longer in the library user=%s item=%s",
                 account.key, item_id)
        return
    found = describarr.outcome(item)
    state = found["state"]
    if state in DESCRIPTION_READY:
        # Kept when nothing could be sent, so the hour after Jellyfin said it
        # cannot send puts the notice off rather than losing it.
        if description_ready(account.key, item_id, ask["title"]) is not None:
            store.drop_description_ask(account.key, item_id)
        return
    if state in DESCRIPTION_GAVE_UP:
        # Only the ask this answer was about: somebody asking again meanwhile
        # sent describarr a new request, which may yet succeed. A ready
        # description answers any ask for it, so that branch drops whichever.
        store.drop_description_ask(account.key, item_id, ask["asked_at"])
        log.info("description ask forgotten, describarr said %s user=%s item=%s "
                 "detail=%s", state, account.key, item_id, found["detail"])
    # Anything else is working, queued or not known yet, and waits.
