"""Filling the gaps in a series the library already holds part of.

EchoFin's series screen lists the books of one series this library has, in
reading order, and the natural question on it is "where are the others".
Listenarr can monitor a whole series, but its library is only what it has
bought, so monitoring would re-acquire every book already on the shelf -- the
architecture rule at the top of `listenarr.py` exists to stop exactly that.
This module answers the question against Jellyfin instead: which of the books
Audible files under this series are not here, and asks for only those, one at
a time, through the same path a single request takes. Every guard on that path
-- the allowance, the duplicate check, the ledger, the immediate search --
applies to each book as if it had been asked for on its own.
"""
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import NamedTuple

from .. import config, jellyfin, listenarr, logs
from . import audible, engine, shelves, store, wants

log = logs.get("series")


class NotASeries(LookupError):
    """Nothing in the caller's library is filed under that name."""


class Unresolvable(Exception):
    """The series is real here but cannot be matched to one Audible series."""


class Unavailable(Exception):
    """Listenarr would not answer, so nothing can be asked for."""


#: How many titles a spoken sentence names before it counts the rest.
NAMED_TITLES = 5

#: What a search row calls itself when it stands for a whole series.
SERIES_KIND = "series"

# A trailing qualifier a library adds to tell editions apart -- "(Jim Dale)",
# "(Full-Cast Editions)" -- which Audible's own series name may or may not
# carry. Tried as written first, then without it.
_QUALIFIER = re.compile(r"\s*\([^()]*\)\s*$")


def _text(value) -> str:
    """A string field out of a catalogue row, whatever actually arrived in it.

    Listenarr relays Audible's JSON as it comes, and a field that is usually a
    string is occasionally a number or null. Every field read off a row goes
    through here so a malformed row is skipped rather than raising past the
    route as a 500.
    """
    return value.strip() if isinstance(value, str) else ""


class Library(NamedTuple):
    """What a plan reads of the library: the owned index of every book, and
    the books under each series name.

    From a fresh listing, or from memory by way of `shelves.series_index`,
    which keeps both beside the index it already serves.
    """

    owned: tuple[set, dict]
    by_series: dict[str, list[dict]]

    @classmethod
    def listed(cls, books: list[dict]) -> "Library":
        return cls(engine._owned_index(books), engine._books_by_series(books))

    def members(self, name: str) -> list[dict]:
        """The books filed under `name`, by the rule a client groups by: the
        name, ignoring case and punctuation."""
        return self.by_series.get(engine._norm(name), [])


def _position(value) -> str | None:
    """A series position as a comparable string, or None when there is none.

    Jellyfin holds an integer and Audible a string that may be "3", "3.0" or
    "3.5"; comparing them as text would make book three two different books.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return text.casefold()
    if math.isnan(number) or math.isinf(number):
        return text.casefold()
    return str(int(number)) if number == int(number) else str(number)


def _row_position(row: dict, series_asin: str) -> str | None:
    """The position Audible files this row at, within the series asked about.

    A book can sit in several series -- a franchise label and the numbered
    sequence -- so the membership is matched by the series' own ASIN before
    falling back to whichever one carries a number.
    """
    memberships = [m for m in (row.get("series") or []) if isinstance(m, dict)]
    for membership in memberships:
        if _text(membership.get("asin")).upper() == series_asin.upper():
            return _position(membership.get("position"))
    for membership in memberships:
        position = _position(membership.get("position"))
        if position is not None:
            return position
    return None


def _catalogue_name(rows: list, series_asin: str) -> str | None:
    """What Audible calls the series these rows belong to.

    The name a plan was asked under is the *library's* spelling, or -- from
    search -- whatever somebody typed. Neither is safe to put on a row that
    offers to acquire ten books: `_series_by_name` accepts a name whose words
    are all present rather than an exact match, so "iron druid" resolves the
    Iron Druid Chronicles and a row titled "iron druid" would not say what is
    about to be asked for. Display and identity are separated for that reason,
    and only display uses this.
    """
    for row in rows:
        if not isinstance(row, dict):
            continue
        for membership in row.get("series") or []:
            if not isinstance(membership, dict):
                continue
            if _text(membership.get("asin")).upper() != series_asin.upper():
                continue
            found = _text(membership.get("name")) or _text(membership.get("title"))
            if found:
                return found
    return None


def _not_out_yet(row: dict, today: date) -> bool:
    """A book nothing can acquire, because it has not been published.

    Measured 2026-09-05, when a nine-row listing of a four-book series turned
    five placeholders into five requests Listenarr will search for forever.

    The rule itself is `listenarr.not_yet_published`, which now also guards the
    add boundary every single request goes through. Held here as well because
    a batch counts and names what it is holding back, where the boundary can
    only refuse one book at a time.
    """
    return listenarr.not_yet_published(_text(row.get("releaseDate")), today)


def _identity(position: str | None, title: str) -> str:
    """What makes two catalogue rows one book.

    The series position where there is one -- Audible lists both marketplaces'
    editions of a book as two rows at the same position -- and the title
    otherwise, because an unnumbered companion volume is listed twice the same
    way and would be asked for twice.
    """
    if position is not None:
        return f"#{position}"
    return f"title:{engine._norm(title)}"


def _tokens(text: str) -> set[str]:
    return set(engine._norm(text).split())


def _series_from_members(members: list[dict], name: str) -> tuple[str, str] | None:
    """The Audible series behind these books, from one that carries an ASIN.

    The membership whose name is the library's spelling wins; failing that,
    the one that matches once a trailing qualifier is dropped; failing that,
    the numbered series the book primarily belongs to, on the grounds that a
    book on this screen is in this series whatever Audible calls it. Returns
    (series ASIN, marketplace it was found in).
    """
    wanted = engine._norm(name)
    unqualified = engine._norm(_QUALIFIER.sub("", name))
    best: tuple[int, str, str] | None = None
    for book in members:
        # Folded, as every ASIN here is: the product cache is keyed by what it
        # was asked for, and Jellyfin hands back whatever spelling it was given.
        asin = (engine._asin(book) or "").upper()
        if not asin:
            continue
        product = audible.product(asin)
        if not product:
            continue
        region = product.get("_region") or config.AUDIBLE_REGION
        primary_name, _ = audible._primary_series(product)
        primary = engine._norm(primary_name or "")
        for membership in product.get("series") or []:
            if not isinstance(membership, dict):
                continue
            series_asin = _text(membership.get("asin")).upper()
            if not series_asin:
                continue
            title = engine._norm(
                _text(membership.get("title")) or _text(membership.get("name")))
            if not title:
                continue
            if title == wanted:
                rank = 0
            elif title == unqualified:
                rank = 1
            elif title == primary:
                rank = 2
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, series_asin, region)
        if best is not None and best[0] == 0:
            break
    if best is None:
        return None
    return best[1], best[2]


def _series_by_name(name: str) -> tuple[tuple[str, str] | None, int]:
    """Audible's own series search, trusted only when it is unambiguous.

    The fallback for a series none of whose books carries an Audible id --
    the torrented half of a library. Three readings of the name are tried in
    turn, each accepted only when exactly one series answers to it: the name
    as the library spells it; a series whose name contains every word of it,
    which is how "Harry Potter (Stephen Fry)" finds "Harry Potter (Narrated by
    Stephen Fry)" and "Wheel of Time" finds "The Wheel of Time"; and the name
    with its trailing qualifier dropped. Two matches at any step is a guess
    about which edition somebody meant, and a guess here acquires the wrong
    narrator's books, so it refuses rather than trying a looser reading.

    Answers with the resolution *and* how many series answered to the name at
    all, because those are two different failures and the caller owes a
    different sentence for each: a name nothing in the catalogue carries is not
    a series, while one carried by three is a series this cannot pick an
    edition of. They were the same `None` while the only caller had a library
    book behind the name to fall back on describing.
    """
    matches, listed = _readings(name)
    if len(matches) == 1:
        asin, row = matches[0]
        return (asin, _text(row.get("region")) or config.AUDIBLE_REGION), listed
    if len(matches) > 1:
        log.info("series name %r matches %d Audible series; refusing to guess",
                 name.strip(), len(matches))
    return None, listed


def _readings(name: str) -> tuple[list[tuple[str, dict]], int]:
    """The catalogue series a name answers to, at the first reading that finds
    any, and how many series the searches returned at all.

    The readings `_series_by_name` describes. Kept apart from it because two
    callers want different things from the same answer: one series to plan,
    or every series a listener could have meant, to choose between.
    """
    full = name.strip()
    unqualified = _QUALIFIER.sub("", name).strip()
    rows_by_asin: dict[str, dict] = {}
    for query in dict.fromkeys([full, unqualified]):
        if not query:
            continue
        rows = listenarr.series_candidates(query)
        if rows is None:
            raise Unavailable("Listenarr did not answer.")
        for row in rows:
            asin = _text(row.get("asin")).upper()
            if asin and asin not in rows_by_asin:
                rows_by_asin[asin] = row
    if not rows_by_asin:
        return [], 0

    wanted = engine._norm(full)
    wanted_tokens = _tokens(full)
    rules = [
        lambda listed: listed == wanted,
        lambda listed: bool(wanted_tokens) and wanted_tokens <= set(listed.split()),
        lambda listed: bool(unqualified) and listed == engine._norm(unqualified),
    ]
    for rule in rules:
        matches = [(asin, row) for asin, row in rows_by_asin.items()
                   if rule(engine._norm(_text(row.get("name"))))]
        if matches:
            return matches, len(rows_by_asin)
    return [], len(rows_by_asin)


#: How many series one searched name may offer to choose between. Each is a
#: plan, which is two catalogue requests, and a client gives the whole search
#: twenty seconds, so the list stays short and is planned a few at a time.
MAX_CHOICES = 6
CHOICE_WORKERS = 4


def plans_for(user: jellyfin.User, query: str) -> list[dict]:
    """A plan for every series a searched name could mean.

    One plan when the name picks out one series. When several answer to it,
    one plan for each, up to `MAX_CHOICES`, so a listener chooses rather than
    being told there is no such series: "disney" is a word in two of
    Audible's, and searching for it said "No series found" (2026-09-24).
    `plan` still refuses to guess for anybody who asks it for one series; each
    choice is planned under the catalogue's own name for it, which picks that
    series and no other, and asking for it re-plans under the same name.

    Empty when nothing answers. Raises `Unavailable` when Listenarr will not.
    Planned against the library as `shelves.series_index` keeps it in memory,
    at most `OWNED_TTL_SECONDS` old, the age a book search already accepts:
    listing it afresh is eleven seconds of the twenty the client gives a
    search. What asking does is planned against a fresh listing.
    """
    query = query.strip()
    if not query:
        return []
    library = Library(*shelves.series_index(user))
    try:
        return [plan(user, query, library=library)]
    except NotASeries:
        return []
    except Unresolvable:
        pass
    matches, _ = _readings(query)
    names: list[str] = []
    seen: set[str] = set()
    for _asin, row in matches:
        name = _text(row.get("name"))
        key = engine._norm(name)
        if key and key not in seen:
            seen.add(key)
            names.append(name)
    names = names[:MAX_CHOICES]
    if not names:
        return []

    def choice(name: str) -> dict | None:
        try:
            return plan(user, name, library=library)
        except (NotASeries, Unresolvable) as exc:
            # Two catalogue series under one name cannot be told apart by it,
            # and a choice that cannot be asked for is not one to offer.
            log.info("series choice %r for %r not offered: %s", name, query, exc)
            return None

    # A few at a time: each plan waits on the catalogue, not on this process.
    with ThreadPoolExecutor(max_workers=CHOICE_WORKERS) as pool:
        planned = list(pool.map(choice, names))
    return [found for found in planned if found is not None]


def _named_in(title: str, index) -> bool:
    """Whether a listing's title names a book in `index`, under any of the
    forms `engine._title_keys` gives it, a shared prefix alone excepted.

    Within one series a title is evidence enough without an author; the
    library's copy can carry a shorter title than Audible's under an author
    tag padded with the series name.
    """
    return any(key in index and not engine.shares_only_a_prefix(key, title, index)
               for key in engine._title_keys(title))


def plan(user: jellyfin.User, name: str, anchor_item_id: str | None = None,
         library: Library | None = None) -> dict:
    """What asking for the rest of a series would do, decided without doing it.

    Owned is judged three ways, and the third is the one that matters: by
    ASIN, by title and author as everywhere else, and by *position*. Audible
    files both marketplaces' editions of one book at the same position --
    the Philosopher's and the Sorcerer's Stone are two rows, both book one --
    so without the position rule a library holding every book of the series
    would be asked to acquire all of them again under their other titles.

    "On order" is an unfulfilled request from anybody in the household, and
    nothing else. A book this listener hid on the Discover shelf is left out
    and said to be, not folded in with the ones on their way. A book Audible
    has not published is held back the same way -- see `_not_out_yet`.

    **A series the library holds none of is a valid plan, not an error.** It
    used to be refused, because the only caller was the series screen, which
    can only be reached through a book already here. Search is the second
    caller and asks the opposite question -- "get me this whole series" --
    and answering it is the difference between acquiring a series in one tap
    and acquiring book one, waiting for it to import, and then asking again
    from a screen that has only now appeared. With no members, `_series_by_name`
    resolves it from the catalogue alone, which is the same fallback a series
    whose books carry no Audible id already relies on, and every row comes back
    as a gap. An *anchored* plan still requires its anchor: that caller names a
    book on screen and a name that does not hold it is a mismatch worth
    refusing.

    The library is listed afresh unless `library` is given. Search gives the
    one kept in memory; asking, from the series screen or from search, does
    not, because what it decides is acted on.
    """
    name = name.strip()
    if not name:
        raise NotASeries("Enter the name of a series.")
    if library is None:
        library = Library.listed(jellyfin.books(user.id))
    members = library.members(name)
    if anchor_item_id and all(book.get("Id") != anchor_item_id for book in members):
        raise NotASeries(f"That book is not filed under {name} in your library.")

    resolved = _series_from_members(members, name)
    listed = 0
    if resolved is None:
        resolved, listed = _series_by_name(name)
    if resolved is None:
        if not members and not listed:
            # Nothing here is filed under it and the catalogue has never heard
            # of it either. That is not a series at all, which is a different
            # answer from one that exists and cannot be pinned to an edition --
            # and the caller shows a row for the second and none for the first.
            raise NotASeries(
                f"Neither your library nor Audible has a series called {name}.")
        raise Unresolvable(
            f"Could not tell which Audible series {name} is. "
            + ("The name alone is not enough to pick an edition."
               if not members else
               "None of these books carries an Audible id that names one, and "
               "the name alone is not enough to pick an edition."))
    series_asin, region = resolved

    rows = listenarr.series_books(series_asin, region)
    if rows is None:
        raise Unavailable("Listenarr did not answer.")
    if not rows:
        raise Unresolvable(f"Audible lists no books under {name}.")

    asins, by_title = library.owned
    # Folded to upper case, as the catalogue rows are below. Jellyfin and the
    # ledger keep whatever spelling they were handed.
    asins = {a.upper() for a in asins if isinstance(a, str)}
    # Within one series a title alone is evidence. The library's own copy can
    # carry a shorter title than Audible's listing -- "Side Jobs" against
    # "Side Jobs: Stories from the Dresden Files" -- under an author tag padded
    # with the series name, which the household-wide check rightly refuses to
    # take for the same author. Measured on the live library, 2026-09-02.
    # As an index rather than a bare set of keys, so a prefix several books
    # share is not taken for one of them: "Disney Princess: Moana and Tales
    # from Motunui" read as held because "Disney Princess: Belle..." is
    # (2026-09-24). See `engine.shares_only_a_prefix`.
    _, member_titles = engine._owned_index(members)
    ordered = {a.upper() for a in store.ordered_asins()}
    # By title too. This listing can come from the other marketplace, whose
    # ASINs differ from the ones the books were asked for under, and on ASIN
    # alone three Disney books already on order read as gaps to ask for again.
    _, ordered_titles = engine._owned_index(
        [{"Name": title} for title in store.ordered_titles()])
    hidden = {a.upper() for a in store.dismissed_asins(user.key)}
    today = datetime.now(timezone.utc).date()

    # Two passes. A row is judged on its own first -- owned by id or by title
    # and author, on order, or hidden -- and only then by what it shares an
    # identity with, because the two editions of one book can arrive in either
    # order and the second must not be planned for as a gap when the first
    # turns out to be owned.
    owned_keys = {
        _identity(position, "") for book in members
        if (position := _position(book.get("IndexNumber"))) is not None
    }
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        asin = _text(row.get("asin")).upper()
        if not asin or asin in seen:
            continue
        seen.add(asin)
        title = _text(row.get("title"))
        if not title:
            # Nothing to recognise it by and nothing to ask for it under.
            log.info("series row %s has no title; skipped", asin)
            continue
        position = _row_position(row, series_asin)
        candidate = {
            "asin": asin,
            "title": title,
            "authors": [_text(a.get("name")) for a in (row.get("authors") or [])
                        if isinstance(a, dict) and _text(a.get("name"))],
            "position": position,
            "key": _identity(position, title),
        }
        candidate["owned"] = (engine._already_owned(candidate, asins, by_title)
                              or _named_in(title, member_titles))
        # Ahead of "on order", because a placeholder asked for in an earlier
        # tap is not on its way and saying so would be a lie with a search
        # behind it.
        candidate["notOut"] = not candidate["owned"] and _not_out_yet(row, today)
        candidate["ordered"] = (not candidate["owned"] and not candidate["notOut"]
                                and (asin in ordered or _named_in(title, ordered_titles)))
        candidate["hidden"] = (not candidate["owned"] and not candidate["notOut"]
                               and not candidate["ordered"] and asin in hidden)
        if candidate["owned"]:
            owned_keys.add(candidate["key"])
        candidates.append(candidate)
    ordered_keys = {c["key"] for c in candidates if c["ordered"]}
    hidden_keys = {c["key"] for c in candidates if c["hidden"]}

    have: list[dict] = []
    not_out: list[dict] = []
    on_order: list[dict] = []
    left_out: list[dict] = []
    missing: list[dict] = []
    missing_keys: set[str] = set()
    for candidate in candidates:
        key = candidate["key"]
        if candidate["owned"] or key in owned_keys:
            if not any(row["key"] == key for row in have):
                have.append(candidate)
        elif candidate["notOut"]:
            # On the row's own date and no other row's. One marketplace can
            # list a book as out while the other still shows a placeholder for
            # it, and the edition that is out is a real gap.
            not_out.append(candidate)
        elif candidate["ordered"] or key in ordered_keys:
            if not any(row["key"] == key for row in on_order):
                on_order.append(candidate)
        elif candidate["hidden"] or key in hidden_keys:
            left_out.append(candidate)
        elif key in missing_keys:
            # The other edition of a gap already planned for. One book, one
            # request.
            continue
        else:
            missing.append(candidate)
            missing_keys.add(key)

    log.info("series plan user=%s series=%r asin=%s region=%s listed=%d have=%d "
             "on_order=%d hidden=%d not_out=%d missing=%d", user.key, name,
             series_asin, region, len(seen), len(have), len(on_order),
             len(left_out), len(not_out), len(missing))
    return {
        "series": name,
        # Identity stays `series` -- it is what re-plans to this same answer,
        # and what the anchored caller and the ledger already use. This is the
        # name to *show*, which is not always the same string.
        "catalogueName": _catalogue_name(rows, series_asin) or name,
        "seriesAsin": series_asin,
        "region": region,
        "have": have,
        "onOrder": on_order,
        "leftOut": left_out,
        "notOut": not_out,
        "missing": missing,
        "rows": {_text(row.get("asin")).upper(): row
                 for row in rows if isinstance(row, dict)},
    }


def state_sentence(have: int, on_order: int, missing: int) -> str:
    """What a row says about a series, before anybody asks for it.

    Deliberately not `sentence`, which describes what one tap just did and
    reads as a past tense. This describes the state a person is choosing from.

    Owning none of it is an ordinary answer rather than a refusal, so the counts
    have to read as a sentence at zero too: "0 of 12 in your library" is
    arithmetic where "none of the 12" is English, and it is spoken aloud.

    Lives here rather than in one adapter because both search routes put the
    same row on screen and a second copy is a second wording to drift.
    """
    total = have + on_order + missing
    if not missing:
        if have and on_order:
            # Not "all 8": that read as the whole series with five of its
            # thirteen still on their way (2026-09-24).
            return (f"{have} of {total} in your library, {on_order} already on "
                    "order. Nothing left to ask for.")
        if have:
            return f"You already have all {have} that Audible lists."
        # Nothing to ask for and nothing here: every book is on order, held
        # back as unpublished, or dismissed. Claiming to own all zero of them
        # would be the one reading that is simply false.
        return f"Nothing left to ask for out of the {total} Audible lists."
    parts = [f"{have} of {total} in your library" if have
             else f"None of the {total} in your library"]
    if on_order:
        parts.append(f"{on_order} already on order")
    parts.append(f"{missing} to ask for")
    return ", ".join(parts) + "."


def search_rows(user: jellyfin.User, query: str) -> list[dict]:
    """One series, in the shape the audiobook search results are already in.

    The nextup route answers with its own hit shape; this is the same plan in
    the shape the recommendation screen's search has always decoded, because
    that screen is where a listener searches for a book and is therefore where
    they will look for a series. Two shapes rather than one because the two
    clients decode different keys, and quietly handing either the other's is
    the seam that cost a round here once already.

    `asin` is the *series* ASIN: it is real, unique, and the identifier the
    client keys a row on. `series` is the name that planned this and is what
    asking for it must send back -- `want_series` re-plans from a name and has
    no ASIN door -- so identity for the row and identity for the request are
    two fields, not one.
    """
    try:
        plans = plans_for(user, query)
    except Unavailable as exc:
        log.info("series search unresolved query=%r (%s)", query, exc)
        return []
    return [_search_row(planned) for planned in plans]


def _search_row(planned: dict) -> dict:
    """One planned series, in the recommendation screen's row shape."""
    have = len(planned.get("have") or ())
    on_order = len(planned.get("onOrder") or ())
    missing = len(planned.get("missing") or ())
    return {
        "asin": planned["seriesAsin"],
        "title": planned.get("catalogueName") or planned["series"],
        # A series is not written by one person and the row has no room to
        # claim it is. Empty rather than absent: the client requires the key.
        "authors": [],
        "narrators": [],
        "runtimeMinutes": None,
        # Owned is nothing left to ask for AND nothing outstanding; a series
        # every book of which is on order is `requested`, the same distinction
        # a single book draws.
        "owned": missing == 0 and on_order == 0,
        "requested": missing == 0 and on_order > 0,
        # Additive, and the reason a client can tell these rows apart: a server
        # that predates this sends no `kind` and every row is a book.
        "kind": SERIES_KIND,
        "series": planned["series"],
        "have": have,
        "onOrder": on_order,
        "missing": missing,
        "detail": state_sentence(have, on_order, missing),
    }


def want_series(user: jellyfin.User, name: str,
                anchor_item_id: str | None = None) -> dict:
    """Ask for the books of one series the library does not hold, bounded.

    Only what Audible has actually published: an unreleased volume is counted
    and named as such rather than asked for, because a request for a book that
    does not exist is one Listenarr searches for forever.

    Each book goes through `wants.want`, so a repeat is free, the ledger sees
    it, and Listenarr is handed the search. Bounded twice: by the tap limit,
    so one activation cannot become forty acquisitions, and for a capped
    account by what is left of the day. What was not asked for is counted,
    and the sentence says why.
    """
    planned = plan(user, name, anchor_item_id)
    missing = planned["missing"]
    limit = config.SERIES_WANT_LIMIT
    cap_hit = False
    requested: list[dict] = []
    failed: list[dict] = []
    for candidate in missing:
        if len(requested) + len(failed) >= limit:
            break
        remaining = wants.allowance(user)
        if remaining is not None and remaining <= 0:
            cap_hit = True
            break
        metadata = listenarr.metadata_from_search_row(
            planned["rows"][candidate["asin"]], region=planned["region"])
        try:
            wants.want(user, candidate["asin"], candidate["title"], metadata=metadata)
        except wants.AllowanceExhausted:
            # Another request on this account landed between the check above
            # and the attempt. The cap held; only the accounting is owed.
            cap_hit = True
            break
        except wants.Denied as denied:
            # One book Listenarr would not take is not a reason to stop asking
            # for the others.
            failed.append({**candidate, "reason": str(denied)})
            continue
        requested.append(candidate)
        shelves.forget_asin(candidate["asin"])

    held_back = len(missing) - len(requested) - len(failed)
    if cap_hit and not requested and not failed:
        log.warning("series want denied user=%s series=%r reason=daily-cap",
                    user.key, name)
    log.info("series want user=%s series=%r requested=%d failed=%d held_back=%d cap_hit=%s",
             user.key, name, len(requested), len(failed), held_back, cap_hit)
    owned_count = _distinct_books(planned["have"])
    on_order_count = _distinct_books(planned["onOrder"])
    left_out_count = _distinct_books(planned["leftOut"])
    not_out_count = _distinct_books(planned["notOut"])
    return {
        "series": name,
        "seriesAsin": planned["seriesAsin"],
        "ownedCount": owned_count,
        "onOrderCount": on_order_count,
        "leftOutCount": left_out_count,
        "notOutCount": not_out_count,
        "requested": [{"asin": c["asin"], "title": c["title"]} for c in requested],
        "failed": [{"asin": c["asin"], "title": c["title"], "reason": c["reason"]}
                   for c in failed],
        "heldBackCount": held_back,
        "message": sentence(
            name, owned_count=owned_count, on_order=on_order_count,
            left_out=left_out_count, not_out=not_out_count,
            requested=[c["title"] for c in requested],
            failed=[c["title"] for c in failed],
            held_back=held_back, cap_hit=cap_hit, missing=len(missing)),
    }


def _distinct_books(candidates: list[dict]) -> int:
    """Books, counting the two editions Audible lists of one book once."""
    return len({c["key"] for c in candidates})


def _named(titles: list[str]) -> str:
    """Up to `NAMED_TITLES` titles, spoken, then a count of the rest."""
    shown = [t for t in titles[:NAMED_TITLES] if t]
    rest = len(titles) - len(titles[:NAMED_TITLES])
    text = ", ".join(shown)
    if rest > 0:
        text += f" and {rest} more"
    return text


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def sentence(name: str, *, owned_count: int, on_order: int, left_out: int,
             requested: list[str], failed: list[str], held_back: int,
             cap_hit: bool, missing: int, not_out: int = 0) -> str:
    """What to say about the outcome, in full, because the row that would have
    carried it is on another screen and the tap has nothing else to show for
    itself."""
    parts: list[str] = []
    if not missing:
        if not on_order and not left_out and not not_out:
            return (f"You already have every book Audible lists in {name}: "
                    f"{_plural(owned_count, 'book')}.")
        parts.append(f"You have {_plural(owned_count, 'book')} of {name}.")
        if on_order and not left_out and not not_out:
            parts.append(
                f"The {_plural(on_order, 'book') if on_order != 1 else 'one'} you do not "
                f"have {'are' if on_order != 1 else 'is'} already being looked for.")
        elif on_order:
            parts.append(f"Another {_plural(on_order, 'book')} "
                         f"{'is' if on_order == 1 else 'are'} already being looked for.")
        if left_out:
            parts.append(f"{_plural(left_out, 'book')} you hid "
                         f"{'was' if left_out == 1 else 'were'} left out.")
        if not_out:
            parts.append(f"{_plural(not_out, 'book')} "
                         f"{'is' if not_out == 1 else 'are'} not out yet.")
        return " ".join(parts)

    if requested:
        parts.append(f"Asked for {_plural(len(requested), 'book')} from {name}: "
                     f"{_named(requested)}.")
    if failed:
        parts.append(f"Could not ask for {_named(failed)}.")
    if held_back:
        if cap_hit:
            if requested or failed:
                parts.append(f"That is today's allowance; {_plural(held_back, 'book')} "
                             "can wait until tomorrow.")
            else:
                parts.append(f"You have used today's requests. {_plural(held_back, 'book')} "
                             f"of {name} {'is' if held_back == 1 else 'are'} still missing.")
        else:
            parts.append(f"{held_back} more {'book' if held_back == 1 else 'books'} "
                         "not asked for yet. Use this again for the next batch.")
    if on_order:
        parts.append(f"Another {_plural(on_order, 'book')} "
                     f"{'is' if on_order == 1 else 'are'} already being looked for.")
    if left_out:
        parts.append(f"{_plural(left_out, 'book')} you hid "
                     f"{'was' if left_out == 1 else 'were'} left out.")
    if not_out:
        parts.append(f"{_plural(not_out, 'book')} "
                     f"{'is' if not_out == 1 else 'are'} not out yet.")
    return " ".join(parts)
