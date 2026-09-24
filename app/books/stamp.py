"""Giving a requested book the details it was asked for with.

A book asked for through this service is a known edition: an Audible ASIN with
a release date. The file that arrives for it is whatever an uploader made, and
Jellyfin believes its tags. Death Has Joined the Party, asked for on
2026-09-24, came tagged `date=2000` beside a (P)2026 copyright line, and six
books in the library showed the year 2000 that night. The fork's Audible lookup
cannot repair that: a recent book falls back to Audible's catalogue record,
which it reads for the ASIN and the publisher only, and even a full match never
replaces a year the file has already filled in.

This service has the edition in hand, so when a request is fulfilled it gives
the library item that fulfilled it the edition's release date. Only that; only
where the year disagrees or no date is set; never on a locked item, since a
lock is somebody's decision; and only when the item's running time agrees with
the edition's. The arrival check matches on title and author, which cannot tell
a full reading from a dramatisation of the same book, and stamping one
edition's date onto another would only make a new wrong year.

Not the ASIN, though it is in hand too. An item given an Audible id is one the
fork's provider then matches by id on every refresh, and a refresh in its
default mode replaces the fields that provider supplies: audnex.us has renamed
a series back to a name Audible had dropped that way. Adding ids to thirty
books, as a dry run against the live library would have, trades a wrong year
for a chance of wrong series names nobody asked about.

An item stamped here is not locked. Jellyfin fills only empty fields from a
file's tags, so the year written here is not put back by the next scan.
"""
import threading
import time
from datetime import date
from threading import Thread

from .. import config, jellyfin, logs
from . import audible, engine, shelves, store, wants

log = logs.get("books.stamp")

#: Outcomes after which an ASIN is not looked at again.
FINAL = frozenset({"stamped", "already-right", "locked", "other-edition",
                   "ambiguous"})

#: How many times an ASIN whose outcome is not final is tried before it is left
#: alone. At the sweep's half-hour this is six hours of a book that has
#: arrived by the ledger but cannot yet be found, or an Audible that will not
#: answer.
MAX_ATTEMPTS = 12

#: How far apart the edition's running time and the file's may be and still be
#: taken for one edition: the larger of the two. A chapter's length either way
#: covers an intro trimmed or an outro kept; a dramatisation against a full
#: reading differs by hours.
RUNTIME_TOLERANCE_MINUTES = 10
RUNTIME_TOLERANCE_FRACTION = 0.05

_TICKS_PER_MINUTE = 600_000_000

#: How often the sweep settles arrivals nobody has looked at and stamps them.
SWEEP_MINUTES = 30
FIRST_DELAY_SECONDS = 420

#: One book at a time, from the sweep and from an arrival alike, so the two
#: never stamp one ASIN twice at once.
_stamping = threading.Lock()

#: Whether the running service has started stamping. Set by `watch`, which
#: only the application's startup calls, so a test process that reads a
#: request list never starts stamping books it has no library for.
_active = False


def release_date(product: dict) -> date | None:
    """The edition's release date, or None where Audible gives no real one.

    A volume announced and not yet published carries 2200-01-01, which is a
    placeholder rather than a year to show anybody.
    """
    text = (product.get("release_date") or product.get("issue_date") or "")[:10]
    try:
        released = date.fromisoformat(text)
    except ValueError:
        return None
    return None if released.year >= 2100 else released


def same_edition(item: dict, product: dict) -> bool:
    """Whether a library item's running time agrees with the edition's.

    Nothing to compare is taken as agreement: title and author have already
    decided that this is the book, and a runtime missing from either side is
    no evidence that it is another edition.
    """
    minutes = product.get("runtime_length_min")
    ticks = item.get("RunTimeTicks")
    if not minutes or not ticks:
        return True
    allowed = max(RUNTIME_TOLERANCE_MINUTES, minutes * RUNTIME_TOLERANCE_FRACTION)
    return abs(ticks / _TICKS_PER_MINUTE - minutes) <= allowed


#: How far a library item's year may sit from the edition's release and be
#: left alone. Within it is the book's own publication year against its audio
#: release, or a marketplace listing a year late, which are not errors; the
#: placeholder this exists for was twenty-six years out.
YEAR_SLACK = 2


def amend(record: dict, released: date) -> list[str]:
    """Put the edition's release date on a whole item record. Returns what
    changed.

    The year where it is missing or more than `YEAR_SLACK` out, and the date
    with it. A missing date alone is filled only where the year already agrees,
    so the date and the year never contradict each other. Jellyfin writes an
    unset date as the year 1.
    """
    year = record.get("ProductionYear")
    unset = (record.get("PremiereDate") or "")[:4] in ("", "0001")
    stamp_date = f"{released.isoformat()}T00:00:00.0000000Z"
    if year is None or abs(year - released.year) > YEAR_SLACK:
        record["ProductionYear"] = released.year
        record["PremiereDate"] = stamp_date
        return [f"year {year} -> {released.year}"]
    if unset and year == released.year:
        record["PremiereDate"] = stamp_date
        return [f"date {released.isoformat()}"]
    return []


def _candidates(asin: str, title: str, authors: list[str]) -> list[dict]:
    """Library items the arrival check itself would call this request's book.

    Found by Jellyfin's search, which is fuzzy and only narrows the field, and
    then judged one item at a time by `wants._arrived` against an index of that
    item alone -- the same test that decided the request had arrived, so the
    item stamped is the one that fulfilled it.
    """
    row = {"asin": asin, "title": title, "authors": authors}
    found: dict[str, dict] = {}
    for form in _search_forms(title):
        for item in jellyfin.books_named(form):
            found.setdefault(item["Id"], item)
    return [item for item in found.values()
            if wants._arrived(row, *engine._owned_index([item]))]


def _search_forms(title: str) -> list[str]:
    """The spellings a requested title may be shelved under, to search by.

    The same shapes the arrival check accepts: the whole title, the title
    without a trailing qualifier ("The House of Hades (Heroes of Olympus Book
    4)" is shelved as "The House of Hades"), and either side of a colon ("The
    Heroes of Olympus: The Demigod Diaries" is shelved as "The Demigod
    Diaries"). Jellyfin's search is fuzzy but does not reach from one to the
    other.
    """
    forms = [title.strip()]
    bare = wants._TRAILING_PARENTHETICAL.sub("", title).strip()
    for text in (bare, *title.split(":", 1)):
        text = text.strip()
        if text and len(text.split()) >= 2 and text not in forms:
            forms.append(text)
    return forms


def stamp_one(asin: str, title: str, authors: list[str],
              only_if_due: bool = False) -> str | None:
    """Stamp the item that fulfilled a request for `asin`. Returns the outcome,
    or None when `only_if_due` found it already settled.

    The due check is made inside the lock, so an arrival noticed by a read and
    the same arrival reached by the sweep stamp it once, not twice.
    """
    with _stamping:
        if only_if_due and not _due(asin):
            return None
        try:
            outcome, item_id, changed = _stamp(asin, title, authors)
        except jellyfin.JellyfinUnavailable as exc:
            log.warning("stamping %s could not reach Jellyfin: %s", asin, exc)
            outcome, item_id, changed = "error", "", []
        store.put_stamp(asin, item_id, outcome)
    if outcome == "stamped":
        log.info("stamped %r (%s) on item %s: %s", title, asin, item_id,
                 ", ".join(changed))
    else:
        log.info("stamp %r (%s): %s", title, asin, outcome)
    return outcome


def _stamp(asin: str, title: str, authors: list[str]) -> tuple[str, str, list[str]]:
    product = audible.product(asin)
    if not product:
        return "no-product", "", []
    released = release_date(product)
    if released is None:
        return "no-date", "", []
    matches = _candidates(asin, title, authors)
    if not matches:
        return "not-found", "", []
    fitting = [item for item in matches if same_edition(item, product)]
    if not fitting:
        return "other-edition", matches[0]["Id"], []
    if len(fitting) > 1:
        return "ambiguous", "", []
    item_id = fitting[0]["Id"]
    record = jellyfin.item_record(item_id)
    if record is None:
        return "not-found", "", []
    if record.get("LockData"):
        return "locked", item_id, []
    changed = amend(record, released)
    if not changed:
        return "already-right", item_id, []
    jellyfin.update_item(record)
    return "stamped", item_id, changed


def _due(asin: str) -> bool:
    prior = store.get_stamp(asin)
    return prior is None or (prior["outcome"] not in FINAL
                             and prior["attempts"] < MAX_ATTEMPTS)


def stamp_arrivals(rows: list[dict]) -> int:
    """Stamp each newly arrived request that is due. Returns how many changed."""
    done = 0
    for row in rows:
        outcome = stamp_one(row["asin"], row.get("title") or "",
                            row.get("authors") or [], only_if_due=True)
        done += outcome == "stamped"
    return done


def soon(rows: list[dict]) -> None:
    """Stamp arrivals just seen, behind whoever's read noticed them.

    A request list is read with somebody waiting, and stamping costs an
    Audible lookup and two Jellyfin searches a book.
    """
    rows = [dict(row) for row in rows]
    if not rows or not _active:
        return

    def run() -> None:
        try:
            stamp_arrivals(rows)
        except Exception as exc:  # noqa: BLE001 - nobody is waiting on this
            # thread, and the sweep will try the same books again.
            log.warning("stamping arrivals failed: %s", exc)

    Thread(target=run, name="book-stamp", daemon=True).start()


def sweep() -> int:
    """Settle arrivals nobody has looked at, then stamp everything due.

    Arrival is otherwise noticed only when somebody reads a request list, so a
    book that lands while nobody looks would keep the file's year until they
    did. The second half is also the backfill: every request fulfilled before
    stamping existed is due until it has an outcome.
    """
    names = {uid: name for name, uid in jellyfin.all_users().items()}
    for key in store.users_awaiting_books():
        name = names.get(key)
        if not name:
            continue
        try:
            user = jellyfin.user(name)
            wants.states(user.key, shelves.owned_index(user))
        except Exception as exc:  # noqa: BLE001 - one account is not every one
            log.warning("could not settle book arrivals user=%s: %s", key, exc)
    done = 0
    for row in store.fulfilled_book_requests():
        outcome = stamp_one(row["item_key"], row["title"] or "",
                            store._authors_of(row["authors"]), only_if_due=True)
        done += outcome == "stamped"
    return done


def _loop() -> None:
    time.sleep(FIRST_DELAY_SECONDS)
    while True:
        try:
            stamped = sweep()
            if stamped:
                log.info("stamp sweep changed %d book(s)", stamped)
        except Exception as exc:  # noqa: BLE001 - the thread must not die, or
            # arrivals go back to waiting for somebody to read a list.
            log.warning("stamp sweep failed: %s", exc)
        time.sleep(SWEEP_MINUTES * 60)


def watch() -> None:
    """Start stamping, on arrival and by sweep, if book upkeep is on."""
    global _active
    if not config.BOOK_UPKEEP_HOURS:
        log.info("book stamping is off with the rest of book upkeep")
        return
    _active = True
    Thread(target=_loop, name="book-stamp-sweep", daemon=True).start()
