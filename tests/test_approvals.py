"""Asks past the daily allowance: held for a keyholder, approved, declined.

The rule being tested is the one Matt chose: up to their limit people get what
they ask for, and past it an ask becomes a request a keyholder answers. So the
allowance check is where holding happens, approving is the same ask made again
with the allowance set aside, and nothing about the ordinary path changes for
a caller that did not say it can show a waiting request.
"""
import threading

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4",
    SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k",
    SONARR_QUALITY_PROFILE_ID="4",
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MOVIE_DAILY_CAP="2", SERIES_DAILY_CAP="1", MUSIC_DAILY_CAP="3",
    MUSIC_ARTIST_COST="3", MUSIC_ALBUM_COST="1", MUSIC_TRACK_COST="1",
    APPROVALS="on",
    APPROVAL_NOTIFY_URL="http://notify.invalid/approvals",
    APPROVAL_NOTIFY_TOKEN="publish-token",
    PAGES_URL="https://nextup.example",
)

from app import (approvals, arr, buskarr, config, episodes, held,  # noqa: E402
                 jellyfin, media, podcasts, podfetch, radarr, seasons, sonarr,
                 store, wants)

check = harness.Check("approvals")
store.init()

MATT = jellyfin.User(id="u-matt", name="Matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="Kid", is_admin=False)
OTHER = jellyfin.User(id="u-other", name="Other", is_admin=False)
THIRD = jellyfin.User(id="u-third", name="Third", is_admin=False)
ACCOUNTS = [MATT, KID, OTHER, THIRD]

jellyfin.accounts = lambda: list(ACCOUNTS)


def _account(reference):
    for account in ACCOUNTS:
        if account.id == reference or account.name.casefold() == reference.casefold():
            return account
    raise LookupError(f"no Jellyfin account named {reference!r}")


jellyfin.account = _account

# Stated rather than derived from a live Jellyfin, and pinned through the two
# readers: the cached registry expires against a monotonic clock, which a
# bare assignment to it does not survive on a machine that has been up a while.
REGISTRY = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 2, ("lib-movies",)),
    media.SERIES: media.Medium(media.SERIES, "Series", ("series",), 1, ("lib-tv",)),
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("lib-music",)),
    media.PODCAST: media.Medium(media.PODCAST, "Podcasts", ("podcast",), 1,
                                ("lib-podcasts",)),
}
media.available = lambda: dict(REGISTRY)
media.get = lambda medium: REGISTRY.get(medium)


def set_owned(**kwargs):
    import time
    media._owned._value = jellyfin.Owned(**kwargs)
    media._owned._built_at = time.monotonic()


set_owned()
media.episode_counts = lambda provider_ids: {}
sonarr.acquisition_progress = lambda backend_ids: {}
buskarr.state = lambda ref: None
buskarr.states = lambda refs: {}
podcasts.owned = lambda: []
podcasts.progress = lambda row: None
podcasts.arrived = lambda row: False

added: list[tuple] = []
#: What the next add answers, so a test can make Radarr refuse or say it
#: already had the film.
radarr_answer = {"ok": True, "created": True}


def fake_radarr_add(tmdb, title="", year="", monitored=True):
    added.append(("movie", tmdb, title))
    if not radarr_answer["ok"]:
        return arr.AddResult(False, "Radarr refused it.")
    return arr.AddResult(True, "Sent to Radarr.", f"r{tmdb}", title, year,
                         created=radarr_answer["created"])


radarr.add = fake_radarr_add
series_choices: list = []


def fake_sonarr_add(tvdb, title="", monitored=True, choice=None):
    added.append(("series", tvdb, title))
    series_choices.append(choice)
    return arr.AddResult(True, "Sent to Sonarr.", f"s{tvdb}", title,
                         seasons=choice.encode() if choice else "")


sonarr.add = fake_sonarr_add
buskarr.add = lambda unit, hit, by, bulk=False: (
    added.append(("music", unit, hit.get("title"))) or
    arr.AddResult(True, "Sent to buskarr.", f"job:{hit.get('ref')}", hit.get("title", "")))
podcast_choices: list = []


def fake_podfetch_add(feed_url, title="", choice=None):
    added.append(("podcast", feed_url, title))
    podcast_choices.append(choice)
    return arr.AddResult(True, "Sent to the fetcher.", "p1", title)


podfetch.add = fake_podfetch_add
cancelled: list = []
radarr.cancel = sonarr.cancel = buskarr.cancel = podfetch.cancel = (
    lambda backend_id: cancelled.append(backend_id) or True)

notices: list[tuple] = []
held._post = lambda url, body, headers: notices.append((url, body, dict(headers)))


def settle_notices():
    """Wait for the notice threads, which run beside the ask by design."""
    for thread in threading.enumerate():
        if thread.name == "approval-notice":
            thread.join(5)


def film(n, title=None):
    return {"title": title or f"Film {n}", "year": "2001",
            "imageUrl": f"https://img.example/{n}.jpg", "overview": f"About {n}."}


def ask(user, n, over_limit=True, **kwargs):
    return wants.want(user, media.MOVIE, f"tmdb:{n}", "movie", film(n),
                      over_limit=over_limit, **kwargs)


# --- within the allowance nothing changes ------------------------------------

check.equal(ask(KID, 1)[0], wants.ON_ITS_WAY, "a first film goes straight through")
check.equal(ask(KID, 2)[0], wants.ON_ITS_WAY, "and a second, up to the limit")
check.equal(wants.allowance(KID, media.MOVIE), 0, "which is now spent")
check.equal(len(added), 2, "both were handed to Radarr")

# --- a caller that cannot show a waiting request is refused, as before -------

check.raises(wants.Denied, lambda: ask(KID, 3, over_limit=False),
             "past the limit without saying so, it is refused as before")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:3"), None,
            "and nothing is held for it")

config.APPROVALS = False
check.raises(wants.Denied, lambda: ask(KID, 3),
             "a server that does not hold asks refuses even a caller that can")
config.APPROVALS = True

# --- past the limit it waits -------------------------------------------------

state, message = ask(KID, 3)
settle_notices()
check.equal(state, held.WAITING_FOR_APPROVAL, "past the limit it waits instead")
check.that("past today's limit for films" in message and "Matt" in message,
           f"and says why, and who it went to: {message!r}")
check.equal(len(added), 2, "nothing was handed to Radarr")
check.equal(wants.allowance(KID, media.MOVIE), 0, "and nothing was charged")
row = store.held(KID.key, media.MOVIE, "tmdb:3")
check.that(row is not None and row["state"] == store.HELD_WAITING,
           "the ask is written down as waiting")
check.equal(len(notices), 1, "whoever approves is told once")
url, body, headers = notices[0]
check.equal(url, "http://notify.invalid/approvals", "at the configured address")
check.that("Kid asked for Film 3 (a film)" in body,
           f"naming who asked for what: {body!r}")
check.equal(headers.get("Click"), "https://nextup.example/approvals",
            "linking to the page that answers it")
check.equal(headers.get("Authorization"), "Bearer publish-token",
            "with the configured token")

state, message = ask(KID, 3)
settle_notices()
check.equal(state, held.WAITING_FOR_APPROVAL, "asking again is still waiting")
check.that(message.startswith("Already asked for."),
           f"and reads as a repeat, which an imported list counts as one: {message!r}")
check.equal(len(notices), 1, "and is not announced twice")

check.equal([r["itemKey"] for r in wants.states(KID)
             if r["state"] == held.WAITING_FOR_APPROVAL], [],
            "a list for a caller that did not ask for held asks leaves them out")
listed = {r["itemKey"]: r for r in wants.states(KID, include_held=True)}
check.equal(listed.get("tmdb:3", {}).get("state"), held.WAITING_FOR_APPROVAL,
            "and a caller that asked sees it waiting")
check.equal(listed.get("tmdb:3", {}).get("title"), "Film 3",
            "under its own title")

# --- a keyholder is never held -----------------------------------------------

for n in (50, 51, 52):
    check.equal(ask(MATT, n)[0], wants.ON_ITS_WAY,
                f"a keyholder's film {n} goes straight through")
check.equal(store.held(MATT.key, media.MOVIE, "tmdb:52"), None,
            "nothing a keyholder asks for waits")

# --- approving ---------------------------------------------------------------

spent_before = store.spent_today(KID.key, media.MOVIE, 0)
answer = approvals.approve(MATT, media.MOVIE, "tmdb:3")
check.equal(answer["state"], wants.ON_ITS_WAY, "approving sets it coming")
check.equal(answer["approvedFor"], ["Kid"], "for the person who asked")
check.that(answer["message"].startswith("Approved for Kid."),
           f"and says so: {answer['message']!r}")
check.equal(added[-1], ("movie", "3", "Film 3"),
            "with what the ask carried, handed to Radarr once")
request = store.get(KID.key, media.MOVIE, "tmdb:3")
check.that(request is not None, "it is now an ordinary request of theirs")
check.equal((request["cost"], request["allowance"]), (0, store.APPROVED_POOL),
            "recorded free, under its own pool")
check.equal(store.spent_today(KID.key, media.MOVIE, 0), spent_before,
            "so tomorrow's allowance is untouched by it")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:3"), None,
            "and it is no longer waiting")
check.raises(approvals.NothingWaiting,
             lambda: approvals.approve(MATT, media.MOVIE, "tmdb:3"),
             "approving it twice finds nothing waiting")

# --- a refusal at approval leaves it waiting --------------------------------

ask(KID, 4)
radarr_answer["ok"] = False
check.raises(wants.Denied, lambda: approvals.approve(MATT, media.MOVIE, "tmdb:4"),
             "an approval Radarr refuses is a refusal")
radarr_answer["ok"] = True
check.equal(store.held(KID.key, media.MOVIE, "tmdb:4")["state"],
            store.HELD_WAITING, "and the ask goes on waiting")

# --- declining ---------------------------------------------------------------

answer = approvals.decline(MATT, media.MOVIE, "tmdb:4", "  We have it on disc.  ")
check.equal(answer["declinedFor"], ["Kid"], "declining names who it was for")
row = store.held(KID.key, media.MOVIE, "tmdb:4")
check.equal((row["state"], row["reason"], row["decided_by"]),
            (store.HELD_DECLINED, "We have it on disc.", "Matt"),
            "the no is kept with its reason and who gave it")
listed = {r["itemKey"]: r for r in wants.states(KID, include_held=True)}
check.equal(listed["tmdb:4"]["state"], held.DECLINED, "the asker sees the no")
check.equal(listed["tmdb:4"].get("reason"), "We have it on disc.",
            "with the reason")
check.raises(approvals.NothingWaiting,
             lambda: approvals.approve(MATT, media.MOVIE, "tmdb:4"),
             "a declined ask cannot then be approved")

notices.clear()
state, _ = ask(KID, 4)
settle_notices()
check.equal(state, held.WAITING_FOR_APPROVAL,
            "asked for again past the limit, a declined ask waits again")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:4")["reason"], "",
            "without the old answer")
check.equal(len(notices), 1, "and is announced again")
approvals.decline(MATT, media.MOVIE, "tmdb:4")

wants.reset_allowance(KID, media.MOVIE)
check.equal(ask(KID, 4)[0], wants.ON_ITS_WAY,
            "within the allowance a declined film goes straight through")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:4"), None,
            "and the old no leaves the list")

# --- a waiting ask within the allowance simply goes through ------------------

store.set_cap(THIRD.key, media.MOVIE, 0)
state, message = ask(THIRD, 5)
check.equal(state, held.WAITING_FOR_APPROVAL, "a limit of 0 holds everything")
check.that("needs approval on this account" in message,
           f"and says so plainly: {message!r}")
store.clear_cap(THIRD.key, media.MOVIE)
check.equal(ask(THIRD, 5)[0], wants.ON_ITS_WAY,
            "given an allowance, the same ask goes through")
check.equal(store.held(THIRD.key, media.MOVIE, "tmdb:5"), None,
            "and stops waiting")

# --- somebody else asking for it settles a waiting ask ----------------------

store.set_cap(OTHER.key, media.MOVIE, 0)
check.equal(ask(OTHER, 6)[0], held.WAITING_FOR_APPROVAL, "Other's ask waits")
handed = len(added)
check.equal(ask(THIRD, 6)[0], wants.ON_ITS_WAY,
            "Third asks for the same film within their allowance")
check.equal(len(added), handed + 1, "it is handed to Radarr once")
check.equal(store.held(OTHER.key, media.MOVIE, "tmdb:6"), None,
            "Other no longer waits for a keyholder")
rode = store.get(OTHER.key, media.MOVIE, "tmdb:6")
check.that(rode is not None and rode["cost"] == 0 and rode["backend_id"] == "r6",
           "and has the same request, free")

# --- something already coming costs nothing more ----------------------------

handed = len(added)
radarr_answer["created"] = False
check.equal(ask(OTHER, 2)[0], wants.ON_ITS_WAY,
            "past the limit, asking for a film already coming goes through")
radarr_answer["created"] = True
check.equal(store.held(OTHER.key, media.MOVIE, "tmdb:2"), None,
            "with nothing for a keyholder to decide")
check.equal(store.get(OTHER.key, media.MOVIE, "tmdb:2")["cost"], 0,
            "and nothing charged")

# --- everybody waiting is approved at once ----------------------------------

store.set_cap(THIRD.key, media.MOVIE, 0)
ask(OTHER, 7)
ask(THIRD, 7)
handed = len(added)
answer = approvals.approve(MATT, media.MOVIE, "tmdb:7")
check.equal(sorted(answer["approvedFor"]), ["Other", "Third"],
            "one approval answers everybody who asked")
check.equal(len(added), handed + 1, "with one ask to Radarr")
check.that(store.get(THIRD.key, media.MOVIE, "tmdb:7") is not None
           and store.get(OTHER.key, media.MOVIE, "tmdb:7") is not None,
           "and a request each")

# --- withdrawing -------------------------------------------------------------

ask(OTHER, 8)
removed, message = wants.cancel(OTHER, media.MOVIE, "tmdb:8")
check.that(removed, "a waiting ask can be withdrawn")
check.equal(cancelled, [], "with nothing to call off")
check.equal(store.held(OTHER.key, media.MOVIE, "tmdb:8"), None, "and it is gone")

# --- already in the library at approval -------------------------------------

ask(OTHER, 9)
set_owned(movie_tmdb={"9"})
answer = approvals.approve(MATT, media.MOVIE, "tmdb:9")
check.equal(answer["state"], wants.IN_LIBRARY,
            "an approval of something that has since arrived says so")
check.equal(store.held(OTHER.key, media.MOVIE, "tmdb:9"), None,
            "and nobody is left waiting on it")
set_owned()

# --- a series: approving with fewer seasons ----------------------------------

check.equal(wants.want(KID, media.SERIES, "tvdb:10", "series", {"title": "Show A"},
                       choice=seasons.Seasons(seasons.ALL), over_limit=True)[0],
            wants.ON_ITS_WAY, "the day's one series goes through")
state, _ = wants.want(KID, media.SERIES, "tvdb:11", "series",
                      {"title": "Long Show"},
                      choice=seasons.Seasons(seasons.ALL), over_limit=True)
check.equal(state, held.WAITING_FOR_APPROVAL, "a second series waits")
check.equal(store.held(KID.key, media.SERIES, "tvdb:11")["choice"], "all",
            "with the seasons it was asked with")
answer = approvals.approve(MATT, media.SERIES, "tvdb:11",
                           seasons.Seasons(seasons.LATEST))
check.equal(series_choices[-1], seasons.Seasons(seasons.LATEST),
            "a keyholder can approve it with only the latest season")
check.equal(store.get(KID.key, media.SERIES, "tvdb:11")["seasons"], "latest",
            "and the request records what was really asked for")

state, _ = wants.want(KID, media.SERIES, "tvdb:12", "series", {"title": "Other Show"},
                      choice=seasons.Seasons(seasons.RANGE, 2, 3), over_limit=True)
approvals.approve(MATT, media.SERIES, "tvdb:12")
check.equal(series_choices[-1], seasons.Seasons(seasons.RANGE, 2, 3),
            "approved as asked, it gets the seasons that were asked for")

# --- music: an artist that costs more than is left --------------------------

wants.want(KID, media.MUSIC, "bk:track:1", "track",
           {"title": "A Song", "ref": "1", "source": "deezer"}, over_limit=True)
state, _ = wants.want(KID, media.MUSIC, "bk:artist:2", "artist",
                      {"title": "A Band", "ref": "2", "source": "deezer"},
                      over_limit=True)
check.equal(state, held.WAITING_FOR_APPROVAL,
            "an artist costing more than is left waits")
approvals.approve(MATT, media.MUSIC, "bk:artist:2")
check.equal(added[-1], ("music", "artist", "A Band"),
            "and is handed to buskarr with the hit it was asked with")

# --- a podcast keeps how much of it was asked for ---------------------------

choice = episodes.Episodes(episodes.LATEST, 3)
wants.want(KID, media.PODCAST, "pod:a", "podcast",
           {"title": "Pod A", "feedUrl": "https://feeds.example/a"},
           episodes_choice=choice, over_limit=True)
state, _ = wants.want(KID, media.PODCAST, "pod:b", "podcast",
                      {"title": "Pod B", "feedUrl": "https://feeds.example/b"},
                      episodes_choice=choice, over_limit=True)
check.equal(state, held.WAITING_FOR_APPROVAL, "a second podcast waits")
approvals.approve(MATT, media.PODCAST, "pod:b")
check.equal((added[-1], podcast_choices[-1]),
            (("podcast", "https://feeds.example/b", "Pod B"), choice),
            "approved, it fetches the feed it was asked for, as much as asked")

# --- an imported list is never held -----------------------------------------

store.set_cap(OTHER.key, media.MOVIE, 0)
check.raises(wants.Denied,
             lambda: wants.want(OTHER, media.MOVIE, "tmdb:99", "movie", film(99),
                                imported=True),
             "an imported film past the limit is refused, as before")
check.equal(store.held(OTHER.key, media.MOVIE, "tmdb:99"), None,
            "and never becomes a request to approve")

# --- the keyholder's list ----------------------------------------------------

store.set_cap(THIRD.key, media.MOVIE, 0)
ask(OTHER, 20)
ask(THIRD, 20)
ask(THIRD, 21)
waiting = approvals.waiting()
by_key = {entry["itemKey"]: entry for entry in waiting}
check.equal(sorted(by_key), ["tmdb:20", "tmdb:21"],
            "the keyholder's list has one entry per thing")
check.equal([who["name"] for who in by_key["tmdb:20"]["askedBy"]], ["Other", "Third"],
            "naming everybody who asked, earliest first")
check.equal(store.waiting_count(), 2, "and the count is of things, not asks")
check.equal(held.capability(MATT).get("waiting"), 2,
            "which a keyholder's capabilities carry")
check.equal("waiting" in held.capability(KID), False,
            "and nobody else's do")
check.equal(held.capability(KID).get("approvers"), ["Matt"],
            "everybody is told who approves")

harness.cleanup()
raise SystemExit(check.report())
