"""Asks waiting for a keyholder go through by themselves once they fit.

Matt, 2026-10-30: "if someone asks for a show one day, I forget to approve it
and they have room the next day, it should just automatically happen." An ask
only waits because it was past the allowance when it was made; once the
allowance has room again it goes through as the asker's own ask, charged as
one, whether or not a keyholder got to it. A no stays a no, and an account
whose every ask needs approving keeps needing it.
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
)

from app import (approvals, arr, buskarr, config, held,  # noqa: E402
                 jellyfin, media, radarr, seasons, sonarr, store, wants)

check = harness.Check("held asks go through by themselves")
store.init()

MATT = jellyfin.User(id="u-matt", name="Matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="Kid", is_admin=False)
OTHER = jellyfin.User(id="u-other", name="Other", is_admin=False)
STRICT = jellyfin.User(id="u-strict", name="Strict", is_admin=False)
ACCOUNTS = [MATT, KID, OTHER, STRICT]
jellyfin_down = {"now": False}


def fake_accounts():
    if jellyfin_down["now"]:
        raise jellyfin.JellyfinUnavailable("Jellyfin is not answering")
    return list(ACCOUNTS)


jellyfin.accounts = fake_accounts


def _account(reference):
    for account in ACCOUNTS:
        if account.id == reference or account.name.casefold() == reference.casefold():
            return account
    raise LookupError(f"no Jellyfin account named {reference!r}")


jellyfin.account = _account

REGISTRY = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 2, ("lib-movies",)),
    media.SERIES: media.Medium(media.SERIES, "TV shows", ("series",), 1, ("lib-tv",)),
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("lib-music",)),
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

added: list[tuple] = []
#: What the next Radarr add answers: fine, refused for good, or unreachable.
radarr_answer = {"ok": True, "transient": False}


def fake_radarr_add(tmdb, title="", year="", monitored=True):
    added.append(("movie", tmdb, title))
    if not radarr_answer["ok"]:
        return arr.AddResult(False, "Radarr refused it.",
                             transient=radarr_answer["transient"])
    return arr.AddResult(True, "Sent to Radarr.", f"r{tmdb}", title, year)


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

notices: list[str] = []
held._post = lambda url, body, headers: notices.append(body)


def settle_notices():
    for thread in threading.enumerate():
        if thread.name == "approval-notice":
            thread.join(5)


def film(n):
    return {"title": f"Film {n}", "year": "2001"}


def ask_film(user, n):
    return wants.want(user, media.MOVIE, f"tmdb:{n}", "movie", film(n),
                      over_limit=True)


def ask_music(user, unit, n):
    return wants.want(user, media.MUSIC, f"bk:{unit}:{n}", unit,
                      {"title": f"{unit.title()} {n}", "ref": str(n),
                       "source": "deezer"}, over_limit=True)


def a_day_passes(item_key=None):
    """Move charges back past the allowance's day, the way time would.

    The allowance is a rolling day from each request, so moving one request
    back gives back exactly that one's cost and no more.
    """
    with store.db() as conn:
        if item_key is None:
            conn.execute("UPDATE requests SET requested_at = requested_at - 90000")
        else:
            conn.execute("UPDATE requests SET requested_at = requested_at - 90000 "
                         "WHERE item_key=?", (item_key,))


def waiting(user, medium, item_key):
    row = store.held(user.key, medium, item_key)
    return row is not None and row["state"] == store.HELD_WAITING


# --- what the asker and the keyholder are told --------------------------------

ask_film(KID, 1)
ask_film(KID, 2)
state, message = ask_film(KID, 3)
settle_notices()
check.equal(state, held.WAITING_FOR_APPROVAL, "a third film waits for approval")
check.that("goes through by itself" in message,
           f"and the asker is told it goes through by itself later: {message!r}")
check.that("If you leave it, it goes through by itself" in notices[-1],
           f"and so is whoever approves, so they know they can leave it: {notices[-1]!r}")
_, message = ask_film(KID, 3)
check.that(message.startswith("Already asked for.")
           and "or for your allowance to free up" in message,
           f"asking again says what it is waiting for: {message!r}")
listed = {r["itemKey"]: r for r in wants.states(KID, include_held=True)}
check.equal(listed["tmdb:3"].get("releasesOnItsOwn"), True,
            "and their list says it goes through by itself")
entry = {e["itemKey"]: e for e in approvals.waiting()}["tmdb:3"]
check.equal(entry["askedBy"][0].get("releasesOnItsOwn"), True,
            "and so does the keyholder's list")

# --- nothing goes while the allowance has no room ---------------------------

handed = len(added)
check.equal(approvals.release_when_room(), [],
            "a pass the same day lets nothing through")
check.equal(len(added), handed, "and hands nothing to Radarr")
check.that(waiting(KID, media.MOVIE, "tmdb:3"), "the ask still waits")

# --- a day later it goes through, charged ------------------------------------

a_day_passes()
went = approvals.release_when_room()
check.equal([(w["itemKey"], w["state"]) for w in went], [("tmdb:3", wants.ON_ITS_WAY)],
            "a day later the waiting film goes through by itself")
check.equal(added[-1], ("movie", "3", "Film 3"),
            "handed to Radarr with what the ask carried")
request = store.get(KID.key, media.MOVIE, "tmdb:3")
check.that(request is not None, "it is an ordinary request of theirs now")
check.equal((request["cost"], request["allowance"]), (1, ""),
            "charged to their ordinary allowance, not the keyholder's free pool")
check.equal(wants.allowance(KID, media.MOVIE), 1,
            "so it took one of the day's two")
check.equal(store.held(KID.key, media.MOVIE, "tmdb:3"), None,
            "and nothing waits any more")
check.equal(approvals.release_when_room(), [], "the next pass has nothing to do")

# --- oldest first, as many as fit --------------------------------------------

state, _ = ask_film(KID, 4)
check.equal(state, wants.ON_ITS_WAY, "the day's second film goes straight through")
for n in (5, 6, 7):
    check.equal(ask_film(KID, n)[0], held.WAITING_FOR_APPROVAL, f"film {n} waits")
a_day_passes("tmdb:3")
went = approvals.release_when_room()
check.equal([w["itemKey"] for w in went], ["tmdb:5"],
            "with room for one, only the oldest waiting film goes")
check.that(waiting(KID, media.MOVIE, "tmdb:6") and waiting(KID, media.MOVIE, "tmdb:7"),
           "the two after it go on waiting")

# --- room spent between the check and the ask --------------------------------

# The pass looks at the allowance, then asks. The asker's own tap can land in
# between, and then the ask is only a repeat that leaves it waiting.
seen_room = approvals._room
approvals._room = lambda asker, medium: 1
handed = len(added)
check.equal(approvals.release_when_room(), [],
            "with the room gone by the time it asks, nothing is said to have gone")
approvals._room = seen_room
check.equal(len(added), handed, "nothing reached Radarr")
check.that(waiting(KID, media.MOVIE, "tmdb:6") and waiting(KID, media.MOVIE, "tmdb:7"),
           "and both asks still wait")

# --- a no stays a no ----------------------------------------------------------

approvals.decline(MATT, media.MOVIE, "tmdb:6", "Not this one.")
a_day_passes()
went = approvals.release_when_room()
check.equal([w["itemKey"] for w in went], ["tmdb:7"],
            "a declined ask is passed over; the one after it goes")
row = store.held(KID.key, media.MOVIE, "tmdb:6")
check.equal((row["state"], row["reason"]), (store.HELD_DECLINED, "Not this one."),
            "and the no stands, with its reason")
check.equal(store.get(KID.key, media.MOVIE, "tmdb:6"), None,
            "and it was never asked for")

# --- a limit of 0 always waits for a keyholder --------------------------------

store.set_cap(STRICT.key, media.MOVIE, 0)
state, message = ask_film(STRICT, 8)
check.equal(state, held.WAITING_FOR_APPROVAL, "a limit of 0 holds the ask")
check.that("goes through by itself" not in message,
           f"and promises nothing about it going through: {message!r}")
a_day_passes()
check.equal(approvals.release_when_room(), [], "and a pass never lets it through")
check.that(waiting(STRICT, media.MOVIE, "tmdb:8"), "it waits for a keyholder")
listed = {r["itemKey"]: r for r in wants.states(STRICT, include_held=True)}
check.equal(listed["tmdb:8"].get("releasesOnItsOwn"), False,
            "and their list does not say it will")

# --- the seasons a series was asked with --------------------------------------

check.equal(wants.want(OTHER, media.SERIES, "tvdb:10", "series", {"title": "Show A"},
                       over_limit=True)[0],
            wants.ON_ITS_WAY, "the day's one series goes through")
state, _ = wants.want(OTHER, media.SERIES, "tvdb:11", "series", {"title": "Show B"},
                      choice=seasons.Seasons(seasons.LATEST), over_limit=True)
check.equal(state, held.WAITING_FOR_APPROVAL, "a second series waits")
a_day_passes()
went = approvals.release_when_room()
check.equal([w["itemKey"] for w in went], ["tvdb:11"],
            "a day later the series goes through")
check.equal(series_choices[-1], seasons.Seasons(seasons.LATEST),
            "with the seasons it was asked with")
check.equal(store.get(OTHER.key, media.SERIES, "tvdb:11")["seasons"], "latest",
            "which the request records")

# --- music: a big ask is not overtaken by small ones --------------------------

for n in (1, 2, 3):
    ask_music(KID, "track", n)
check.equal(ask_music(KID, "artist", 4)[0], held.WAITING_FOR_APPROVAL,
            "an artist costing the whole day waits")
check.equal(ask_music(KID, "track", 5)[0], held.WAITING_FOR_APPROVAL,
            "and a song asked for after it waits behind it")
a_day_passes("bk:track:1")
check.equal(approvals.release_when_room(), [],
            "with room for one song, the artist does not fit and the song behind it waits its turn")
a_day_passes()
went = approvals.release_when_room()
check.equal([w["itemKey"] for w in went], ["bk:artist:4"],
            "with the whole day free the artist goes, and the song after it has to wait")
check.equal(store.get(KID.key, media.MUSIC, "bk:artist:4")["cost"], 3,
            "the artist charged at its full price")
check.that(waiting(KID, media.MUSIC, "bk:track:5"), "the song is still waiting")

# --- an ask no day could cover waits for a keyholder, and holds nothing back --

store.set_cap(OTHER.key, media.MUSIC, 2)
ask_music(OTHER, "track", 20)
ask_music(OTHER, "track", 21)
state, message = ask_music(OTHER, "artist", 22)
check.equal(state, held.WAITING_FOR_APPROVAL, "an artist past a limit of 2 waits")
check.that("goes through by itself" not in message,
           f"and promises nothing, since no day could cover it: {message!r}")
ask_music(OTHER, "track", 23)
a_day_passes()
went = approvals.release_when_room()
check.equal([w["itemKey"] for w in went], ["bk:track:5", "bk:track:23"],
            "the song after it goes (and Kid's, a day on); the artist is the keyholder's alone")
check.that(waiting(OTHER, media.MUSIC, "bk:artist:22"), "and still waits")

# --- a refusal leaves it for a keyholder for a while ---------------------------

store.reset_allowance(KID.key, media.MOVIE)
for n in (30, 31):
    ask_film(KID, n)
ask_film(KID, 32)
a_day_passes()
radarr_answer.update(ok=False, transient=False)
tries = len(added)
check.equal(approvals.release_when_room(), [], "Radarr refusing it lets nothing through")
check.equal(len(added), tries + 1, "after one try")
check.that(waiting(KID, media.MOVIE, "tmdb:32"), "and the ask goes on waiting")
radarr_answer.update(ok=True)
check.equal(approvals.release_when_room(), [],
            "the next pass leaves it alone rather than asking again")
check.equal(len(added), tries + 1, "so Radarr is not asked every ten minutes")
approvals._refused.clear()
check.equal([w["itemKey"] for w in approvals.release_when_room()], ["tmdb:32"],
            "once the rest is over it is tried again, and goes")

# --- an unreachable tool is tried again on the next pass ----------------------

check.equal(ask_film(KID, 33)[0], wants.ON_ITS_WAY, "film 33 uses the day's last room")
check.equal(ask_film(KID, 34)[0], held.WAITING_FOR_APPROVAL, "so film 34 waits")
a_day_passes()
radarr_answer.update(ok=False, transient=True)
check.equal(approvals.release_when_room(), [], "Radarr unreachable lets nothing through")
radarr_answer.update(ok=True, transient=False)
check.equal([w["itemKey"] for w in approvals.release_when_room()], ["tmdb:34"],
            "and the next pass tries again at once")

# --- already in the library ----------------------------------------------------

ask_film(KID, 40)
ask_film(KID, 41)
ask_film(KID, 42)
set_owned(movie_tmdb={"42"})
a_day_passes()
handed = len(added)
went = {w["itemKey"]: w["state"] for w in approvals.release_when_room()}
check.equal(went.get("tmdb:42"), wants.IN_LIBRARY,
            "something that arrived meanwhile is settled, not asked for")
check.equal([a for a in added[handed:] if a[1] == "42"], [],
            "and nothing is handed to Radarr for it")
set_owned()

# --- nothing happens without Jellyfin, or with holding switched off -----------

check.equal(ask_film(KID, 50)[0], wants.ON_ITS_WAY, "film 50 uses the day's last room")
check.equal(ask_film(KID, 51)[0], held.WAITING_FOR_APPROVAL, "film 51 waits")
check.equal(ask_film(KID, 52)[0], held.WAITING_FOR_APPROVAL, "and so does 52")
a_day_passes()
jellyfin_down["now"] = True
check.equal(approvals.release_when_room(), [], "with Jellyfin down nothing goes")
jellyfin_down["now"] = False
config.APPROVALS = False
check.equal(approvals.release_when_room(), [],
            "with holding switched off nothing goes either")
config.APPROVALS = True
check.equal([w["itemKey"] for w in approvals.release_when_room()], ["tmdb:51", "tmdb:52"],
            "and once both are back they go")

# --- one ask failing in a way nobody foresaw holds nothing back ---------------

a_day_passes()
for n in (70, 71):
    ask_film(KID, n)
check.equal(ask_film(KID, 72)[0], held.WAITING_FOR_APPROVAL, "film 72 waits")
check.equal(ask_film(KID, 73)[0], held.WAITING_FOR_APPROVAL, "and 73 behind it")
a_day_passes()
working_add = radarr.add


def breaks_on_72(tmdb, title="", year="", monitored=True):
    if tmdb == "72":
        raise RuntimeError("something nobody foresaw")
    return working_add(tmdb, title, year, monitored)


def one_pass():
    """A pass, with anything it raises recorded as a failure, not a crash."""
    try:
        return approvals.release_when_room()
    except Exception as exc:  # noqa: BLE001 - raising is the defect here.
        check.that(False, f"the pass raised {exc!r} instead of carrying on")
        return None


radarr.add = breaks_on_72
went = one_pass()
radarr.add = working_add
check.equal([w["itemKey"] for w in went or []], ["tmdb:73"],
            "the ask behind a broken one still goes")
check.that(waiting(KID, media.MOVIE, "tmdb:72"),
           "and the broken one waits for a keyholder")
check.equal(one_pass(), [], "and is left alone for a while, not retried every pass")
approvals._refused.clear()

# --- Jellyfin going quiet in the middle of a pass ------------------------------

working_owned = media.owned


def owned_but_down(*args, **kwargs):
    raise jellyfin.JellyfinUnavailable("Jellyfin is not answering")


media.owned = owned_but_down
went = one_pass()
media.owned = working_owned
check.equal(went, [], "Jellyfin going quiet mid-pass stops the pass quietly")
check.that(waiting(KID, media.MOVIE, "tmdb:72"), "with the ask still waiting")
check.equal([w["itemKey"] for w in one_pass() or []], ["tmdb:72"],
            "and not left alone: the next pass lets it through")

# --- somebody made a keyholder since -------------------------------------------

store.set_cap(STRICT.key, media.MOVIE, 0)
ask_film(STRICT, 60)
ACCOUNTS[ACCOUNTS.index(STRICT)] = jellyfin.User(id="u-strict", name="Strict",
                                                 is_admin=True)
check.equal([w["itemKey"] for w in approvals.release_when_room()][-1:], ["tmdb:60"],
            "an account with no limit any more has its waiting asks go through")

harness.cleanup()
raise SystemExit(check.report())
