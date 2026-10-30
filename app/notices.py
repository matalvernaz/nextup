"""Telling somebody, on their phone, what happened to something they asked for.

An ask past the daily limit waits for a keyholder, and until now the only way
to learn what became of it was to open the request list and look. Now a
keyholder's yes or no, an ask going through by itself once the allowance had
room, somebody else's ask settling one that waited, a new book asked for from a
followed series, and what they asked for reaching the library are each said to
the person, on the phones where they use EchoFin.

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

from . import config, jellyfin, logs, store

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
    if not supported() or not wanted(user_key):
        return None
    with _guard:
        if time.monotonic() < _unavailable_until:
            return None
    payload = {"Title": title[:120], "Body": body[:600], "Kind": KIND,
               "ThreadId": KIND,
               "Data": {"medium": medium[:200], "itemKey": item_key[:200]}}
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
    only makes sure that happens without them opening their list first.
    """
    if not supported():
        return 0
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
            break
        except Exception as exc:  # noqa: BLE001 - one account failing must not
            # stop everybody after it hearing about their arrivals.
            log.warning("arrivals not looked at user=%s: %r", user_key, exc)
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
