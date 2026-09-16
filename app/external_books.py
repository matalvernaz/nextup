"""A book's community rating, from a catalogue that is not Audible.

**Why this exists.** The book engine has always had exactly two kinds of
rating: the listener's own, out of Jellyfin's `UserData`, and nothing else.
Audible's similar-products graph is a *similarity* signal and carries no score
at all -- `audible._thin` keeps title, people, series and blurb. So a book
nobody in the household has rated has no quality evidence whatsoever, and a
well-regarded book and a poorly-regarded one by the same author, in the same
series slot, score identically.

**What a rating is used for, and what it is not.** It is a weak prior, capped,
applied after everything the engine already knows. A shelf ordered by public
opinion is a bestseller list, not a recommendation; the point here is to break
ties between candidates the taste profile likes equally, and to stop an
obviously poor volume being offered ahead of a good one.

**Three catalogues, tried in order, first confident answer wins.** Open Library
needs no credential and is always tried. Google Books and Hardcover are dormant
without theirs. The order is deliberate: the free one first, so an installation
with no keys is not merely a degraded version of one with them.

**Matching is the failure mode, not fetching.** There is no shared identifier
between an Audible ASIN and any of these, so a lookup is a title and author
search, and a search will happily return something adjacent. Open Library's own
answer for "The Way of Kings" includes "The Way of Kings, Part One" -- five
stars, from four people -- and a four-book omnibus. So a result is accepted
only when its main title matches token for token once the subtitle is removed,
it carries none of the markers a partial or collected edition carries, and an
author surname matches. Anything else is discarded: a wrong rating is worse
than no rating, because it is indistinguishable from a right one.

**Answering and failing are different.** A catalogue that says "never heard of
it" is remembered for the whole TTL; one that could not be reached is not
remembered at all, so an outage does not switch the signal off for a month.
"""
import re
from dataclasses import dataclass

import httpx

from . import config, logs, store

log = logs.get("external_books")

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

#: Open Library asks callers to identify themselves, and an anonymous crawler
#: is the first thing they rate-limit.
USER_AGENT = "nextup (+https://github.com/matalvernaz/nextup)"

#: Every rating is normalised to this scale before it leaves the module. All
#: three catalogues are already out of five. TMDb's ten-point scale does not
#: appear here -- films are `tmdb.py`'s business.
RATING_SCALE = 5.0

#: Below this, an average says more about who bothered to rate it than about
#: the book. Open Library in particular carries works rated by four people.
MIN_RATING_COUNT = 20

#: What separates an edition's subtitle from its title. A colon covers most of
#: it; " - " covers the publishers who use a dash instead.
_SUBTITLE = re.compile(r"\s*(?::| - ).*$")

#: Markers that a result is a piece of a book, or several books, rather than
#: the book asked for. Checked on the *found* title after the subtitle is
#: stripped, because that is where they survive: "The Way of Kings, Part One"
#: has no colon to strip.
_NOT_THE_WHOLE_BOOK = re.compile(
    r"\b(?:part|pt|vol|volume|book)\s+"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b"
    r"|\b(?:omnibus|boxed set|box set|collection|anthology|"
    r"complete series|trilogy|duology|abridged)\b")

_PUNCTUATION = re.compile(r"[^\w\s]+")


@dataclass(frozen=True, slots=True)
class Rating:
    """One catalogue's verdict on one book."""
    #: Out of five.
    average: float
    #: How many people. Part of the value: an average of 4.8 from six readers
    #: is not the same evidence as 4.1 from forty thousand.
    count: int
    #: Which catalogue said so, for the log and for a reason string.
    source: str


#: What a lookup returns. `answered` is whether the catalogue was asked and
#: replied at all -- false for one that is not configured and for one that
#: could not be reached, and those two must not be written into the cache as
#: "this book has no rating".
@dataclass(frozen=True, slots=True)
class Answer:
    answered: bool
    rating: "Rating | None" = None


NOT_ASKED = Answer(answered=False)


def _normalise(text: str) -> str:
    return " ".join(_PUNCTUATION.sub(" ", (text or "").casefold()).split())


def _main_title(title: str) -> str:
    return _normalise(_SUBTITLE.sub("", title or ""))


def _surnames(authors) -> set[str]:
    """Last words of each author name, which is the part an edition keeps.

    Catalogues disagree about initials, middle names and the order of the two,
    and "Brandon Sanderson" against "Sanderson, Brandon" must not be a miss.
    The surname survives all of it.
    """
    found = set()
    for name in authors or []:
        words = _normalise(name).split()
        if words:
            found.add(words[-1])
    return found


def matches(
    wanted_title: str,
    wanted_authors,
    found_title: str,
    found_authors,
) -> bool:
    """Whether a search result is the book that was asked for.

    Token for token on the main title, in both directions. A subset rule
    accepts "The Way of Kings, Part One" for "The Way of Kings"; a superset
    rule accepts the reverse. Neither is the book.
    """
    found_main = _main_title(found_title)
    if not found_main or _NOT_THE_WHOLE_BOOK.search(found_main):
        return False
    if found_main.split() != _main_title(wanted_title).split():
        return False
    wanted = _surnames(wanted_authors)
    # An author nobody recorded cannot disagree, and the title test above is
    # already exact -- so this is a second opinion rather than the only one.
    if not wanted:
        return True
    return bool(wanted & _surnames(found_authors))


def _pick(rows, title, authors, source, read) -> Rating | None:
    """The most-rated result that is actually the book asked for.

    Most-rated rather than first: catalogues order by their own relevance, and
    the canonical work is the one thousands of people rated, not the
    print-on-demand reissue that happens to match the query text best.

    `read` maps one row to `(title, authors, average, count)`, which is the
    only thing that differs between the three catalogues.
    """
    best: Rating | None = None
    for row in rows:
        found_title, found_authors, average, count = read(row)
        if not matches(title, authors, found_title, found_authors):
            continue
        if not isinstance(average, (int, float)):
            continue
        if not isinstance(count, int) or count < MIN_RATING_COUNT:
            continue
        if best is None or count > best.count:
            best = Rating(
                average=min(RATING_SCALE, max(0.0, float(average))),
                count=count,
                source=source)
    return best


# --- the three catalogues ----------------------------------------------------


def _open_library(title: str, authors) -> Answer:
    """openlibrary.org, which needs no credential at all.

    `search.json` with an explicit field list: the default response carries
    every edition of everything, and the ratings are not in it unless asked for
    by name.
    """
    params = {
        "title": title,
        "fields": "title,author_name,ratings_average,ratings_count",
        "limit": 5,
    }
    people = sorted(_surnames(authors))
    if people:
        params["author"] = " ".join(people)
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                "https://openlibrary.org/search.json",
                params=params,
                headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            docs = resp.json().get("docs") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("open library lookup failed for %r (%s)", title, exc)
        return NOT_ASKED
    return Answer(True, _pick(docs, title, authors, "openlibrary", lambda d: (
        d.get("title"), d.get("author_name"),
        d.get("ratings_average"), d.get("ratings_count"))))


def _google_books(title: str, authors) -> Answer:
    """books.googleapis.com, keyed.

    Deliberately not the keyless form. Unauthenticated callers share one Google
    project's daily quota, and it was measured already exhausted from this
    address -- a 429 naming `project_number:624717413613` -- before this
    service had made a single request. A source that works on a quiet day and
    not on a busy one is worse than one that is plainly off.
    """
    if not config.GOOGLE_BOOKS_API_KEY:
        return NOT_ASKED
    query = f'intitle:"{title}"'
    people = sorted(_surnames(authors))
    if people:
        query += f' inauthor:"{people[0]}"'
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={
                    "q": query,
                    "maxResults": 5,
                    "key": config.GOOGLE_BOOKS_API_KEY,
                })
            resp.raise_for_status()
            volumes = resp.json().get("items") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("google books lookup failed for %r (%s)", title, exc)
        return NOT_ASKED
    infos = [volume.get("volumeInfo") or {} for volume in volumes]
    return Answer(True, _pick(infos, title, authors, "googlebooks", lambda i: (
        i.get("title"), i.get("authors"),
        i.get("averageRating"), i.get("ratingsCount"))))


#: Asked by title and filtered here rather than in GraphQL: the server's filter
#: syntax is the part most likely to differ from what this was written against,
#: and a query that fails outright is a source that is simply off.
HARDCOVER_QUERY = """
query BookRating($title: String!) {
  books(where: {title: {_ilike: $title}}, limit: 5) {
    title
    rating
    ratings_count
    contributions { author { name } }
  }
}
"""

HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"


def hardcover_books(title: str) -> list[dict]:
    """Raw Hardcover rows for one title. Raises on anything going wrong.

    Separate from the adapter below so `doctor` can ask the same question and
    report what came back, rather than swallowing it the way a shelf build has
    to.
    """
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            HARDCOVER_URL,
            headers={"Authorization": f"Bearer {config.HARDCOVER_TOKEN}"},
            json={"query": HARDCOVER_QUERY, "variables": {"title": title}})
        resp.raise_for_status()
        body = resp.json()
    if body.get("errors"):
        # GraphQL answers 200 with an errors array, so a wrong field name is a
        # success as far as HTTP is concerned. Raised rather than returned
        # empty, because this is the failure that would otherwise look exactly
        # like "Hardcover has never heard of any book".
        raise ValueError(f"hardcover rejected the query: {body['errors']}")
    return ((body.get("data") or {}).get("books")) or []


def _hardcover(title: str, authors) -> Answer:
    """api.hardcover.app, token required.

    The catalogue worth having now that Goodreads and StoryGraph publish no
    usable API: it is the one with rating volume behind it. The endpoint and
    the `Authorization: Bearer` shape are verified against the live service;
    the query is not, because verifying it needs a token from an account. So
    `python -m app.doctor` asks it a real question the moment a token exists,
    and a wrong field name shows up as a line in a report rather than as a
    signal that quietly never fires.
    """
    if not config.HARDCOVER_TOKEN:
        return NOT_ASKED
    try:
        rows = hardcover_books(title)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("hardcover lookup failed for %r (%s)", title, exc)
        return NOT_ASKED
    return Answer(True, _pick(rows, title, authors, "hardcover", lambda r: (
        r.get("title"),
        [((c.get("author") or {}).get("name") or "")
         for c in (r.get("contributions") or [])],
        r.get("rating"), r.get("ratings_count"))))


#: Tried in this order, first confident answer wins. Keyless first, so an
#: installation with no credentials is not a degraded version of one with them.
PROVIDERS = (
    ("openlibrary", _open_library),
    ("hardcover", _hardcover),
    ("googlebooks", _google_books),
)


def configured_sources() -> tuple[str, ...]:
    """Which catalogues this installation can actually ask."""
    available = {
        "openlibrary": True,
        "googlebooks": bool(config.GOOGLE_BOOKS_API_KEY),
        "hardcover": bool(config.HARDCOVER_TOKEN),
    }
    return tuple(name for name, _ in PROVIDERS if available[name])


# --- the cache ---------------------------------------------------------------


def _cache_key(source: str, title: str, authors) -> str:
    people = ",".join(sorted(_surnames(authors)))
    return f"rating:{source}:{_main_title(title)}:{people}"


def _cached(source: str, title: str, authors) -> Answer:
    """A stored answer, if there is a fresh one.

    A recorded miss matters as much as a hit: most books are missing from at
    least one catalogue, and re-asking all three about them on every six-hour
    upkeep pass is exactly the spend the budget exists to avoid.
    """
    stored = store.get_external(
        _cache_key(source, title, authors), config.BOOK_RATING_TTL_HOURS)
    if stored is None:
        return NOT_ASKED
    if not stored:
        return Answer(True, None)
    return Answer(True, Rating(
        average=float(stored["average"]),
        count=int(stored["count"]),
        source=stored["source"]))


def _remember(source: str, title: str, authors, rating: "Rating | None") -> None:
    store.put_external(
        _cache_key(source, title, authors),
        {} if rating is None else {
            "average": rating.average,
            "count": rating.count,
            "source": rating.source,
        })


# --- what the engine calls ---------------------------------------------------


def cached_rating(title: str, authors) -> Rating | None:
    """Whatever is already known, asking nothing.

    The half of `rating` that costs no requests, so a shelf build can rank
    every candidate on what it has and spend its lookup budget on the few that
    end up near the top.
    """
    for name, _ in PROVIDERS:
        answer = _cached(name, title, authors)
        if answer.answered and answer.rating is not None:
            return answer.rating
    return None


def rating(title: str, authors) -> Rating | None:
    """This book's community rating, fetching what is not already cached.

    The first confident answer wins. A catalogue that has never heard of the
    book is remembered as such and skipped next time; one that is unreachable,
    or not configured, is neither remembered nor held against it.
    """
    if not (title or "").strip():
        return None
    for name, fetch in PROVIDERS:
        known = _cached(name, title, authors)
        if known.answered:
            if known.rating is not None:
                return known.rating
            continue
        answer = fetch(title, authors)
        if not answer.answered:
            continue
        _remember(name, title, authors, answer.rating)
        if answer.rating is not None:
            return answer.rating
    return None
