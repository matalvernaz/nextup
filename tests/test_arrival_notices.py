"""People who turned notices on are told when what they asked for arrives.

The other half of request notices (Matt, 2026-10-30, "build as much as
possible"): an arrival is settled where it always was, the first time a
request list is read after it lands, and is told there, once. A pass reads
the lists of everybody who turned notices on, so nobody has to open theirs
first. Never a row of an imported list, and many at once are one notice.
"""
import threading

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4", MOVIE_DAILY_CAP="20", BOOK_DAILY_CAP="5",
    APPROVALS="on", REQUEST_NOTICES="on", JELLYFIN_USER="",
    LISTENARR_URL="http://listenarr.invalid", LISTENARR_API_KEY="k",
)

from app import arr, buskarr, config, held, jellyfin, listenarr, media, notices, radarr, store, wants  # noqa: E402
from app.books import shelves  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

check = harness.Check("arrival notices")
store.init()

KID = jellyfin.User(id="u-kid", name="Kid", is_admin=False)
QUIET = jellyfin.User(id="u-quiet", name="Quiet", is_admin=False)
ACCOUNTS = [KID, QUIET]
jellyfin_down = {"now": False}


def fake_accounts():
    if jellyfin_down["now"]:
        raise jellyfin.JellyfinUnavailable("Jellyfin is not answering")
    return list(ACCOUNTS)


jellyfin.accounts = fake_accounts
jellyfin.account = lambda reference: next(a for a in ACCOUNTS if a.id == reference)

REGISTRY = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 20, ("lib-movies",)),
    media.BOOK: media.Medium(media.BOOK, "Books", ("book", "series"), 5, ("lib-books",)),
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("lib-music",)),
}
media.available = lambda: dict(REGISTRY)
media.get = lambda medium: REGISTRY.get(medium)
media.episode_counts = lambda provider_ids: {}
OWNED = {"movies": set()}


def owned_now(force=False):
    if jellyfin_down["now"]:
        raise jellyfin.JellyfinUnavailable("Jellyfin is not answering")
    return jellyfin.Owned(movie_tmdb=set(OWNED["movies"]))


media.owned = owned_now
radarr.add = lambda tmdb, title="", year="", monitored=True: arr.AddResult(
    True, "Sent to Radarr.", f"r{tmdb}", title, year)
held._post = lambda url, body, headers: None
buskarr.add = lambda unit, hit, by, bulk=False: arr.AddResult(
    True, "Sent to buskarr.", f"job:{hit.get('ref')}", hit.get("title", ""))
BUSKARR: dict[str, dict] = {}
buskarr.states = lambda refs: {ref: BUSKARR[ref] for ref in refs if ref in BUSKARR}
buskarr.state = lambda ref: BUSKARR.get(ref)
BOOKS_OWNED: set[str] = set()
shelves.owned_index = lambda user: (set(BOOKS_OWNED), {})
shelves.forget_asin = lambda asin, user_key=None: None
listenarr.add = lambda asin, monitored=True, metadata=None: listenarr.AddResult(
    True, "Sent to Listenarr", 41, title=f"Book {asin}", authors=("An Author",))
listenarr.enqueue_search = lambda audiobook_id: True
listenarr.unmonitor = lambda asins: None
book_wants._stop_looking = lambda asins: None

delivered: list[tuple[str, dict]] = []
jellyfin.notify_user = lambda user_id, payload: (
    delivered.append((user_id, payload)) or {"Sent": 1, "Failed": 0, "Dropped": 0})


def told(user):
    for thread in threading.enumerate():
        if thread.name == "request-notice":
            thread.join(5)
    return [(payload["Title"], payload["Body"]) for user_id, payload in delivered
            if user_id == user.key]


def ask_film(user, n, **extra):
    return wants.want(user, media.MOVIE, f"tmdb:{n}", "movie",
                      {"title": f"Film {n}", "year": "2001"}, **extra)


notices.set_wanted(KID.key, True)

# --- a film arrives -----------------------------------------------------------------

ask_film(KID, 1)
check.equal(notices.check_arrivals(), 1, "the pass reads the list of the one account that turned notices on")
check.equal(told(KID), [], "nothing has arrived yet, so nothing is said")
OWNED["movies"].add("1")
notices.check_arrivals()
check.equal(told(KID), [("Ready to watch", "Film 1 is in the library.")],
            "once it is in the library it is said, without the list being opened")
delivered.clear()
notices.check_arrivals()
wants.states(KID)
check.equal(told(KID), [], "and only once, whoever reads the list after")

# --- the person's own reading of their list tells them too ---------------------------

ask_film(KID, 2)
OWNED["movies"].add("2")
wants.states(KID, include_held=True)
check.equal(told(KID), [("Ready to watch", "Film 2 is in the library.")],
            "an arrival settled by opening the list is said the same way, once")

# --- only for those who turned it on ----------------------------------------------------

delivered.clear()
ask_film(QUIET, 3)
OWNED["movies"].add("3")
notices.check_arrivals()
check.equal(told(QUIET), [], "somebody who never turned notices on is told nothing")
check.equal(store.get(QUIET.key, media.MOVIE, "tmdb:3")["fulfilled_at"], None,
            "and the pass never reads their list")

# --- an imported music list is not news a song at a time ----------------------------------

delivered.clear()
wants.want(KID, media.MUSIC, "bk:track:7", "track",
           {"title": "A Song", "ref": "7", "source": "deezer"}, imported=True)
BUSKARR["job:7"] = {"state": "have"}
notices.check_arrivals()
check.equal(told(KID), [], "a song of an imported music list arriving is not said")
check.that(store.get(KID.key, media.MUSIC, "bk:track:7")["fulfilled_at"] is not None,
           "though it is settled as arrived like any other")
wants.want(KID, media.MUSIC, "bk:track:8", "track",
           {"title": "Another Song", "ref": "8", "source": "deezer"})
BUSKARR["job:8"] = {"state": "have"}
notices.check_arrivals()
check.equal(told(KID), [("Ready to listen", "Another Song is in the library.")],
            "a song asked for on its own is")

# An imported film list is not set apart: film lists are short, and the ledger
# keeps no mark of where a film was asked from, so its arrivals are said.
delivered.clear()
ask_film(KID, 4, imported=True)
OWNED["movies"].add("4")
notices.check_arrivals()
check.equal(told(KID), [("Ready to watch", "Film 4 is in the library.")],
            "a film from an imported list is said like any other")

# --- many at once are one notice ----------------------------------------------------------

delivered.clear()
for n in range(10, 15):
    ask_film(KID, n)
    OWNED["movies"].add(str(n))
notices.check_arrivals()
check.equal(told(KID), [("Ready to watch", "5 things you asked for are in the library: "
                                           "Film 14, Film 13, Film 12 and 2 more.")],
            "five at once are one notice naming the first three")

# --- a book arrives ---------------------------------------------------------------------

delivered.clear()
book_wants.want(KID, "B01", "A Book")
BOOKS_OWNED.add("B01")
notices.check_arrivals()
check.equal(told(KID), [("Ready to listen", "Book B01 is in the library.")],
            "a book arriving is said, under the title Listenarr gave it")

# --- when the pass cannot look ---------------------------------------------------------------

delivered.clear()
ask_film(KID, 20)
OWNED["movies"].add("20")
jellyfin_down["now"] = True
check.equal(notices.check_arrivals(), 0, "with Jellyfin not answering nothing is looked at")
jellyfin_down["now"] = False
check.equal(told(KID), [], "and nothing is said")
config.REQUEST_NOTICES = False
check.equal(notices.check_arrivals(), 0, "a server that cannot send never looks")
config.REQUEST_NOTICES = True
notices.check_arrivals()
check.equal(told(KID), [("Ready to watch", "Film 20 is in the library.")],
            "and the next pass that can, says it")

# --- one account failing does not stop the others ----------------------------------------------

OTHER = jellyfin.User(id="u-other", name="Other", is_admin=False)
ACCOUNTS.append(OTHER)
notices.set_wanted(OTHER.key, True)
ask_film(OTHER, 30)
OWNED["movies"].add("30")
delivered.clear()
reading = wants.states


def breaks_for_kid(user, *args, **kwargs):
    if user.key == KID.key:
        raise RuntimeError("something nobody foresaw")
    return reading(user, *args, **kwargs)


wants.states = breaks_for_kid
try:
    notices.check_arrivals()
except Exception as exc:  # noqa: BLE001 - raising is the defect here.
    check.that(False, f"the pass raised {exc!r} instead of carrying on")
wants.states = reading
check.equal(told(OTHER), [("Ready to watch", "Film 30 is in the library.")],
            "one account's list failing does not stop the next being read")

harness.cleanup()
raise SystemExit(check.report())
