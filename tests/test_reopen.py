"""Asking again for something that arrived once and has since gone.

The ledger is keyed on (account, medium, item) and a repeat tap is free, which
is right while a request is outstanding and wrong once it has been fulfilled:
a film deleted from Jellyfin could never be asked for again by the account
that had it, on either surface, with "Already asked for." as the only
explanation.

What has to be got right is the test for "gone". It is presence, never
`_arrived`: that one asks whether a whole-series request *finished*, needs
Sonarr's aired total, and answers no whenever the total cannot be had -- so
using it here would read every fulfilled series as gone and hand the lot back
to Sonarr.
"""
import time

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4",
    SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k",
    SONARR_QUALITY_PROFILE_ID="4",
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    LISTENARR_URL="http://listenarr.invalid",
    LISTENARR_QUALITY_PROFILE_ID="1",
    MOVIE_DAILY_CAP="3", SERIES_DAILY_CAP="3", MUSIC_DAILY_CAP="3",
)

from app import (arr, buskarr, jellyfin, listenarr, media,  # noqa: E402
                 radarr, sonarr, store, wants)
from app.books import store as book_store  # noqa: E402

check = harness.Check("reopen")
store.init()

MATT = jellyfin.User(id="u-matt", name="matt", is_admin=True)

media._registry = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 3, ("lib-movies",)),
    media.SERIES: media.Medium(media.SERIES, "Series", ("series",), 3, ("lib-tv",)),
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("lib-music",)),
    media.BOOK: media.Medium(media.BOOK, "Books", listenarr.UNITS, 3, ("lib-books",)),
}


def set_owned(**kwargs):
    media._owned._value = jellyfin.Owned(**kwargs)
    media._owned._built_at = time.monotonic()


added: list[tuple] = []
radarr.add = lambda tmdb, title="", year="", monitored=True: (
    added.append(("movie", tmdb))
    or arr.AddResult(True, "Sent to Radarr.", "r1", title, year))
sonarr.add = lambda tvdb, title="", monitored=True: (
    added.append(("series", tvdb))
    or arr.AddResult(True, "Sent to Sonarr.", "s1", title))
buskarr.add = lambda unit, hit, by: (
    added.append(("music", unit))
    or arr.AddResult(True, "Sent to buskarr.", "job:7", hit.get("title", "")))
listenarr.add = lambda asin, monitored=True, metadata=None: (
    added.append(("book", asin))
    or listenarr.AddResult(True, "Sent to Listenarr.", 42, "A Book", ("Someone",)))
listenarr.enqueue_search = lambda audiobook_id: True
# Nothing in this file is settled by Sonarr's aired total, which is the point:
# a fulfilled series must not be reopened just because the total is unknown.
sonarr.acquisition_progress = lambda backend_ids: {}
media.episode_counts = lambda provider_ids: {}
buskarr.state = lambda ref: None


def fulfilled(medium: str, item_key: str, unit: str = "", backend_id: str = "b1"):
    """One request that has already arrived, as the ledger would hold it."""
    store.forget(MATT.key, medium, item_key)
    store.record(MATT.key, medium, item_key, unit or medium, "A Thing", "2020",
                 1, backend_id)
    store.mark_arrived(MATT.key, medium, {item_key})
    row = store.get(MATT.key, medium, item_key)
    assert row["fulfilled_at"] is not None
    return row


# --- a film that is still there is not asked for twice ----------------------
set_owned(movie_tmdb=frozenset({"550"}))
fulfilled(media.MOVIE, "tmdb:550")
added.clear()
state, message = wants.want(MATT, media.MOVIE, "tmdb:550", "movie",
                            {"title": "A Thing"})
check.equal(message, "Already asked for.",
            "a fulfilled film the library still holds is a repeat")
check.equal(added, [], "and nothing is handed to Radarr again")

# --- a film that has gone can be asked for again ----------------------------
set_owned(movie_tmdb=frozenset())
added.clear()
was = store.get(MATT.key, media.MOVIE, "tmdb:550")["requested_at"]
state, message = wants.want(MATT, media.MOVIE, "tmdb:550", "movie",
                            {"title": "A Thing"})
check.equal(state, wants.ON_ITS_WAY,
            "a film deleted from Jellyfin can be asked for again")
check.equal(added, [("movie", "550")], "and Radarr is asked for it")
row = store.get(MATT.key, media.MOVIE, "tmdb:550")
check.that(row is not None and row["fulfilled_at"] is None,
           "the ledger row is open again")
check.that(row is not None and row["requested_at"] >= was,
           "and dated from this request, not the one that arrived")

# --- a series is judged on presence, never on completeness ------------------
#
# The trap this file exists for. `_arrived` needs Sonarr's aired total and
# answers no without one -- which is the state above -- so a completeness test
# here would reopen every fulfilled series and re-add it to Sonarr.
set_owned(series_tvdb=frozenset({"1399"}),
          series_item_ids={"1399": "jf-series-1"})
fulfilled(media.SERIES, "tvdb:1399")
added.clear()
state, message = wants.want(MATT, media.SERIES, "tvdb:1399", "series",
                            {"title": "A Show"})
check.equal(message, "Already asked for.",
            "a fulfilled series the library still holds is a repeat, even "
            "with no aired total to be had")
check.equal(added, [], "and nothing is handed to Sonarr again")

set_owned(series_tvdb=frozenset())
added.clear()
state, message = wants.want(MATT, media.SERIES, "tvdb:1399", "series",
                            {"title": "A Show"})
check.equal(state, wants.ON_ITS_WAY, "a series that has gone can be re-asked")
check.equal(added, [("series", "1399")], "and Sonarr is asked for it")

# --- unknown counts as held, everywhere -------------------------------------
#
# Reopening on an unknown re-acquires something the library still has;
# refusing on one costs a second tap once the answer is back. The second is
# much the cheaper mistake.
media._owned._value = None
media._owned._built_at = 0.0


def unavailable():
    raise jellyfin.JellyfinUnavailable("no route to host")


jellyfin.owned_index = unavailable
fulfilled(media.MOVIE, "tmdb:603")
added.clear()
state, message = wants.want(MATT, media.MOVIE, "tmdb:603", "movie",
                            {"title": "A Thing"})
check.equal(message, "Already asked for.",
            "a Jellyfin that cannot be asked does not reopen anything")
check.equal(added, [], "and nothing is re-acquired on a guess")
set_owned()

# --- music is buskarr's to answer -------------------------------------------
fulfilled(media.MUSIC, "album:1", unit="album", backend_id="job:7")
added.clear()
buskarr.state = lambda ref: {"state": "have"}
wants.want(MATT, media.MUSIC, "album:1", "album", {"title": "An Album"})
check.equal(added, [], "a track buskarr still holds is not fetched again")

buskarr.state = lambda ref: {"state": "gone"}
added.clear()
state, message = wants.want(MATT, media.MUSIC, "album:1", "album",
                            {"title": "An Album"})
check.equal(state, wants.ON_ITS_WAY,
            "and one it no longer holds can be asked for again")
check.equal(added, [("music", "album")], "with buskarr asked for it")

buskarr.state = lambda ref: None
fulfilled(media.MUSIC, "album:2", unit="album", backend_id="job:8")
added.clear()
wants.want(MATT, media.MUSIC, "album:2", "album", {"title": "Another"})
check.equal(added, [],
            "a buskarr that will not answer does not reopen anything either")

# --- a book asked for again is written down ---------------------------------
#
# This path always let a fulfilled book through to Listenarr. What it did not
# do was record it: `record_request` will not write over a row that is already
# there, so the ask reached Listenarr, was charged for, and left nothing on
# the list to say either had happened.
book_store.record_request(MATT.key, "B0GONE", "A Book", ["Someone"])
store.mark_arrived(MATT.key, media.BOOK, {"B0GONE"})
before = book_store.request_row(MATT.key, "B0GONE")
check.that(before is not None and before["fulfilled_at"] is not None,
           "a book that has arrived is on the ledger as arrived")
added.clear()
state, message = wants.want(MATT, media.BOOK, "B0GONE", "book",
                            {"title": "A Book"})
check.equal(state, wants.ON_ITS_WAY, "asking again is accepted")
check.equal(added, [("book", "B0GONE")], "Listenarr is asked for it")
after = book_store.request_row(MATT.key, "B0GONE")
check.that(after is not None and after["fulfilled_at"] is None,
           "and the list says so, rather than still reading as arrived")

# --- an outstanding request is still a free repeat ---------------------------
#
# The whole point of the ledger key. Nothing above may have made a second tap
# on something still on its way cost anything.
store.forget(MATT.key, media.MOVIE, "tmdb:680")
store.record(MATT.key, media.MOVIE, "tmdb:680", "movie", "Waiting", "", 1, "r2")
added.clear()
state, message = wants.want(MATT, media.MOVIE, "tmdb:680", "movie",
                            {"title": "Waiting"})
check.equal(message, "Already asked for.",
            "a request still on its way is a repeat, and free")
check.equal(added, [], "with nothing handed to Radarr")

harness.cleanup()
raise SystemExit(check.report())
