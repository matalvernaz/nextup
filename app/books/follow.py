"""Book series people asked for whole, and the books that come out in them later.

Asking for a whole series asks for what is out now. A book published after
that was nobody's business: nothing looked at the series again, so the next
volume was only ever found by somebody noticing and asking. Asking for a
series follows it now (`series.want_series(follow=True)`), and this looks at
each followed series again once a day.

A new book is one the series did not list as out when it was last looked at:
the next volume, or one that was announced and has now been published. Each
is asked for on the follower's behalf through the ordinary book request path,
charged like any ask of theirs, so past the day's allowance it waits for a
keyholder like any other and goes through by itself once there is room. Books
that were out already are left alone, however many are missing: those are the
ask's business, and the next tap's.
"""
import threading
import time

from .. import config, jellyfin, listenarr, logs, notices
from .. import store as ledger
from . import series, wants

log = logs.get("follow")

#: How long after starting the first look runs, so a restart is settled first.
FIRST_DELAY_SECONDS = 300
#: How often the follows are looked through for ones due a look.
PASS_SECONDS = 3600
#: What a keyholder's notice calls a book asked for this way.
HELD_DETAIL = "a new book in a series they follow"


def check_due() -> list[dict]:
    """Look again at every followed series due a look. Returns what was asked for.

    The library is listed once per account per pass rather than once per
    series, and nothing at all is asked of Jellyfin when nothing is due.
    """
    if not config.FOLLOW_SERIES:
        return []
    due = ledger.follows_due(time.time() - config.FOLLOW_CHECK_HOURS * 3600)
    if not due:
        return []
    try:
        accounts = {account.key: account for account in jellyfin.accounts()}
    except jellyfin.JellyfinUnavailable:
        return []
    libraries: dict[str, series.Library] = {}
    asked: list[dict] = []
    for row in due:
        follower = accounts.get(row["user_key"])
        if follower is None:
            # Gone from Jellyfin. Left as it is: an account that comes back
            # under the same id still follows what it followed.
            continue
        try:
            asked += _look(follower, row, libraries)
        except jellyfin.JellyfinUnavailable as exc:
            log.info("followed series pass stopped, Jellyfin is not answering: %s", exc)
            break
        except series.Unavailable as exc:
            # Listenarr did not answer. Not marked as looked at, so the next
            # pass tries again.
            log.info("followed series not looked at user=%s series=%r: %s",
                     row["user_key"], row["series"], exc)
        except (series.NotASeries, series.Unresolvable) as exc:
            # Kept, and looked at again tomorrow: a library or catalogue that
            # cannot place a series today may well place it again.
            ledger.series_checked(row["user_key"], row["series"], row["known"])
            log.warning("followed series could not be placed user=%s series=%r: %s",
                        row["user_key"], row["series"], exc)
        except Exception as exc:  # noqa: BLE001 - one series failing in a way
            # nobody foresaw must not stop every series after it being looked at.
            ledger.series_checked(row["user_key"], row["series"], row["known"])
            log.warning("followed series failed user=%s series=%r: %r",
                        row["user_key"], row["series"], exc)
    return asked


def _look(follower: jellyfin.User, row: dict,
          libraries: dict[str, "series.Library"]) -> list[dict]:
    """Look at one followed series, and ask for whatever came out since."""
    library = libraries.get(follower.key)
    if library is None:
        library = series.Library.listed(jellyfin.books(follower.id))
        libraries[follower.key] = library
    planned = series.plan(follower, row["series"], row["anchor_item_id"] or None,
                          library=library)
    known = set(row["known"])
    new = [book for book in planned["missing"] if book["key"] not in known]
    published = set(series.published_keys(planned))
    if len(new) > config.FOLLOW_MOST_NEW:
        # Not a series that published this many books overnight, but a
        # catalogue that changed how it lists it. Nothing is asked for; they
        # are taken as known, so this is said once, and asking for the series
        # again gets them.
        ledger.series_checked(follower.key, row["series"], sorted(known | published))
        log.warning("followed series lists %d new books at once, none asked for "
                    "user=%s series=%r: %s", len(new), follower.key, row["series"],
                    ", ".join(book["title"] for book in new))
        return []
    asked: list[dict] = []
    refused: set[str] = set()
    for book in new:
        try:
            state, message = wants.want(
                follower, book["asin"], book["title"],
                metadata=listenarr.metadata_from_search_row(
                    planned["rows"][book["asin"]], region=planned["region"]),
                over_limit=True, detail=HELD_DETAIL)
        except wants.Denied as exc:
            # Listenarr would not take it, or the day's allowance is spent on
            # a server that does not hold asks for a keyholder. Left out of
            # what is known, so the next look tries it again.
            refused.add(book["key"])
            log.warning("new book in a followed series refused user=%s series=%r "
                        "book=%r: %s", follower.key, row["series"], book["title"], exc)
            continue
        log.info("new book in a followed series asked for user=%s series=%r "
                 "book=%r state=%s", follower.key, row["series"], book["title"], state)
        asked.append({"userKey": follower.key, "series": row["series"],
                      "seriesTitle": row["title"] or row["series"],
                      "asin": book["asin"], "title": book["title"],
                      "state": state, "message": message})
        notices.new_in_series(follower.key, row["title"] or row["series"],
                              book["title"], book["asin"], state)
    ledger.series_checked(follower.key, row["series"],
                          sorted((known | published) - refused))
    return asked


def watch() -> None:
    """Start the pass that looks at followed series once a day."""
    threading.Thread(target=_loop, name="followed-series", daemon=True).start()
    log.info("followed series: each looked at every %d h", config.FOLLOW_CHECK_HOURS)


def _loop() -> None:
    time.sleep(FIRST_DELAY_SECONDS)
    while True:
        try:
            asked = check_due()
            if asked:
                log.info("followed series: %d new book(s) asked for", len(asked))
        except Exception as exc:  # noqa: BLE001 - the thread must not die, or
            # followed series silently stop being looked at.
            log.warning("followed series pass failed: %s", exc)
        time.sleep(PASS_SECONDS)
