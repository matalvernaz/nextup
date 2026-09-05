"""Keeping the book shelves, and the playlist they are written into, current.

The shelves are read two ways: by a native client over the JSON API, and by
any Jellyfin client at all through a playlist this service writes. Only the
first of those has a reader that asks for it.

Before this existed, the playlist was written as a side effect of somebody
loading a web page. When that page went away in a merge, the write went with
it -- silently, because a stale playlist looks exactly like a current one. So
the schedule lives here instead, and nothing about the playlist's freshness
depends on a person visiting.

Only accounts that already have a shelf are refreshed. Building one for a
household member who has never opened the feature would cost a twelve-second
Jellyfin listing per person per cycle to produce something nobody asked for.
"""
import time
from threading import Thread

from .. import config, jellyfin, logs
from . import shelves, store

log = logs.get("books.upkeep")

#: Waited out before the first pass. A container that has just started is
#: competing with its own first requests, and Jellyfin may not be up yet.
FIRST_DELAY_SECONDS = 300


def _users_with_shelves() -> list[jellyfin.User]:
    """Resolved accounts, skipping keys Jellyfin no longer knows.

    Resolved by name through Jellyfin rather than assembled from the stored key
    so the account's real administrator flag comes with it: a `User` built here
    with a guessed flag would be a trap for the first caller that reads it.
    """
    keys = store.shelf_keys()
    if not keys:
        return []
    by_id = {uid: name for name, uid in jellyfin.all_users().items()}
    found = []
    for key in keys:
        name = by_id.get(key)
        if not name:
            log.info("shelf for an account Jellyfin no longer has: %s", key)
            continue
        found.append(jellyfin.user(name))
    return found


def once() -> int:
    """One pass. Returns how many accounts were refreshed."""
    done = 0
    for user in _users_with_shelves():
        try:
            shelves.result(user, update_playlist=True)
            done += 1
        except Exception as exc:  # noqa: BLE001 - one account's failure is not
            # every account's, and this runs where nobody is watching.
            log.warning("could not refresh shelves user=%s: %s", user.key, exc)
    return done


def _loop() -> None:
    time.sleep(FIRST_DELAY_SECONDS)
    while True:
        try:
            refreshed = once()
            log.info("refreshed %d account(s)", refreshed)
        except Exception as exc:  # noqa: BLE001 - the thread must not die, or
            # the playlist silently stops being written, which is the failure
            # this module exists to end.
            log.warning("shelf upkeep pass failed: %s", exc)
        time.sleep(config.BOOK_UPKEEP_HOURS * 3600)


def watch() -> None:
    """Start the upkeep pass, if this deployment serves books at all."""
    if not config.BOOK_UPKEEP_HOURS:
        log.info("shelf upkeep is off (BOOK_UPKEEP_HOURS=0); the playlist will "
                 "only be written when something asks for a shelf")
        return
    Thread(target=_loop, name="book-shelf-upkeep", daemon=True).start()
