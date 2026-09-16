"""One person's own Hardcover shelf: what they have rated, read and want.

**Why this is different from a community rating.** `external_books` answers
"what do strangers think of this book", which is the same number for everybody
and belongs to the household. This answers "what has *this listener* read and
what did they think of it", which is the only kind of evidence a recommendation
is actually made of -- and a Hardcover token is personal, so it is the one
thing that can answer it. `me` returns the account that issued the token.

What it is worth, concretely. The engine's taste profile is built from books in
*this Jellyfin library* that somebody played or rated. A reader who has been
using Hardcover has rated books this server has never held -- print, Kindle,
borrowed -- and every one of those is evidence the profile has been missing.
The three things taken from it:

* **Rated** -- a seed, weighted like any other rating, so a book read in print
  shapes the shelf exactly as one listened to here would.
* **Read** -- an exclusion. Recommending a book somebody finished last year is
  the most obviously wrong row a shelf can carry.
* **Want to read** -- a candidate, with the strongest reason there is: they
  said so themselves.

**Never the household's token.** A shelf read with somebody else's credential
is not a degraded answer, it is another person's reading history presented as
yours. An account without its own token contributes nothing here and is
unchanged.
"""
import httpx

from .. import logs, store

log = logs.get("hardcover_shelf")

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

URL = "https://api.hardcover.app/v1/graphql"

#: Hardcover's reading statuses, read off `user_book_statuses` on 2026-09-16
#: rather than guessed. `book_statuses` is a DIFFERENT enum -- it is moderation
#: state (OK / To Review / Deleted / Deduped) -- and reads plausibly enough to
#: be used by mistake.
WANT_TO_READ = 1
CURRENTLY_READING = 2
READ = 3
PAUSED = 4
DID_NOT_FINISH = 5
IGNORED = 6

#: Finished with, one way or another. A book somebody abandoned is as wrong to
#: offer back as one they finished -- more so, since they made a decision about
#: it.
FINISHED = frozenset({READ, DID_NOT_FINISH, IGNORED})

#: How many of somebody's books to read. A prolific Hardcover account runs to
#: thousands and the shelf only needs enough to profile a taste; the most
#: recently touched are the ones that describe it.
MAX_BOOKS = 500

#: Short, unlike the community-rating cache. That one is a public number that
#: moves over months; this is a person's own shelf, and somebody who rates a
#: book should see it count the same day. Matched to the upkeep interval.
TTL_HOURS = 6

QUERY = """
query MyShelf($limit: Int!) {
  me {
    username
    books_count
    user_books(limit: $limit, order_by: {updated_at: desc}) {
      rating
      status_id
      book {
        title
        contributions { author { name } }
      }
    }
  }
}
"""


class Refused(Exception):
    """The token was rejected, or the query was. Worth telling somebody."""


def _rows(payload: dict) -> list[dict]:
    """The books out of one `me` answer, however empty it is."""
    me = ((payload.get("data") or {}).get("me")) or []
    if not me:
        return []
    return me[0].get("user_books") or []


def fetch(token: str) -> dict:
    """One account's shelf, straight from Hardcover. Raises on refusal.

    Returns the whole answer rather than just the books, because the account
    name and its book count are what the settings page says back to somebody
    who has just pasted a token -- "connected as matt1211, 0 books" is a
    verification, and "saved" is not.
    """
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"query": QUERY, "variables": {"limit": MAX_BOOKS}})
        # A wrong token answers 401 (measured). Turned into a refusal here
        # rather than left as an HTTPStatusError, because the two are different
        # sentences to the person who just pasted it: "Hardcover would not
        # accept that token" is something they can fix, and "Hardcover is not
        # answering" is something they should try again later. Everything else
        # stays an HTTP error.
        if resp.status_code in (401, 403):
            raise Refused("Hardcover would not accept that token")
        resp.raise_for_status()
        body = resp.json()
    # Both refusal shapes. A rejected operator answers a bare `error` with no
    # `errors` array at all, which a check for the array alone reads as an
    # empty shelf -- indistinguishable from a reader who has shelved nothing.
    if body.get("errors") or body.get("error"):
        raise Refused(str(body.get("error") or body["errors"]))
    me = ((body.get("data") or {}).get("me")) or []
    if not me:
        raise Refused("the token is valid but names no account")
    return {
        "username": me[0].get("username") or "",
        "books_count": me[0].get("books_count") or 0,
        "books": [
            {
                "title": (((row.get("book") or {}).get("title")) or "").strip(),
                "authors": [
                    ((c.get("author") or {}).get("name") or "")
                    for c in ((row.get("book") or {}).get("contributions") or [])
                ],
                "rating": row.get("rating"),
                "status_id": row.get("status_id"),
            }
            for row in _rows(body)
            if ((row.get("book") or {}).get("title") or "").strip()
        ],
    }


def verify(token: str) -> dict:
    """Check a token at the moment somebody pastes it.

    The whole point of doing this at save time: a token that is wrong, revoked
    or pasted with a space in it is otherwise stored happily and then fails
    silently inside a background shelf build, where nobody sees it. Here it is
    one sentence in front of the person who can fix it.
    """
    return fetch(token)


def _cache_key(user_key: str) -> str:
    return f"hardcover:shelf:{user_key}"


def for_user(user_key: str) -> dict | None:
    """This account's shelf, cached, or None where there is nothing to read.

    None covers every way of having no shelf -- no token, a refused token, an
    unreachable Hardcover -- because the engine does the same thing in all of
    them, which is carry on without it. The log says which.
    """
    token = store.user_setting(user_key, "HARDCOVER_TOKEN")
    if not token:
        return None
    cached = store.get_external(_cache_key(user_key), TTL_HOURS)
    if cached is not None:
        return cached
    try:
        found = fetch(token)
    except (httpx.HTTPError, Refused, ValueError) as exc:
        # Not cached. A refused token is worth re-trying after somebody fixes
        # it, and remembering "no shelf" for six hours would outlive the fix.
        log.warning("hardcover shelf unavailable for %s (%s)", user_key, exc)
        return None
    store.put_external(_cache_key(user_key), found)
    log.info("hardcover shelf for %s: %d book(s) as %s",
             user_key, len(found["books"]), found["username"])
    return found


def forget(user_key: str) -> None:
    """Drop a cached shelf, so a newly saved token takes effect at once."""
    store.drop_external(_cache_key(user_key))


def rated(shelf: dict | None) -> list[dict]:
    """Books this person scored, whatever they did with them afterwards."""
    if not shelf:
        return []
    return [b for b in shelf.get("books") or []
            if isinstance(b.get("rating"), (int, float))]


def finished(shelf: dict | None) -> list[dict]:
    """Books this person is done with: read, abandoned or ignored."""
    if not shelf:
        return []
    return [b for b in shelf.get("books") or []
            if b.get("status_id") in FINISHED]


def wanted(shelf: dict | None) -> list[dict]:
    """Books this person has said they want to read.

    Deliberately not including Currently Reading. Somebody halfway through a
    book does not need it offered to them as something to start.
    """
    if not shelf:
        return []
    return [b for b in shelf.get("books") or []
            if b.get("status_id") == WANT_TO_READ]
