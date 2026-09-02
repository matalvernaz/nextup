"""The request path: allowances, repeats, arrival, and taking a request back."""
import time

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4",
    SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k",
    SONARR_QUALITY_PROFILE_ID="4",
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MOVIE_DAILY_CAP="3", SERIES_DAILY_CAP="1", MUSIC_DAILY_CAP="3",
    MUSIC_ARTIST_COST="3", MUSIC_ALBUM_COST="1", MUSIC_TRACK_COST="1",
)

from app import arr, buskarr, jellyfin, media, radarr, sonarr, store, wants  # noqa: E402

check = harness.Check("wants")
store.init()

MATT = jellyfin.User(id="u-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="kid", is_admin=False)
OTHER = jellyfin.User(id="u-other", name="other", is_admin=False)

# The registry is normally derived from a live Jellyfin. Here it is stated, so
# the tests exercise the request path rather than a network.
media._registry = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 3, ("lib-movies",)),
    media.SERIES: media.Medium(media.SERIES, "Series", ("series",), 1, ("lib-tv",)),
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("lib-music",)),
}

owned = jellyfin.Owned()
episode_counts: dict[str, int] = {}


def set_owned(episodes=None, **kwargs):
    """State what the library holds, and how many episodes of each series.

    Episodes are counted per series against Jellyfin rather than indexed, so
    the stub replaces that lookup rather than a field on the index.
    """
    global owned, episode_counts
    owned = jellyfin.Owned(**kwargs)
    media._owned._value = owned
    media._owned._built_at = time.monotonic()
    episode_counts = dict(episodes or {})


media.episode_counts = lambda provider_ids: {
    k: v for k, v in episode_counts.items() if k in provider_ids}
set_owned()

added: list[tuple] = []
radarr.add = lambda tmdb, title="", year="", monitored=True: (
    added.append(("movie", tmdb)) or arr.AddResult(True, "Sent to Radarr.", "r1", title, year))
sonarr.add = lambda tvdb, title="", monitored=True: (
    added.append(("series", tvdb)) or arr.AddResult(True, "Sent to Sonarr.", "s1", title))
buskarr.add = lambda unit, hit, by: (
    added.append(("music", unit)) or arr.AddResult(True, "Sent to buskarr.", "job:7",
                                                   hit.get("title", "")))
buskarr.state = lambda ref: None
radarr.cancel = sonarr.cancel = buskarr.cancel = lambda backend_id: True

# --- allowances ------------------------------------------------------------

check.equal(wants.allowance(MATT, media.MOVIE), None,
            "an administrator is uncapped")
check.equal(wants.allowance(KID, media.MOVIE), 3, "a capped account starts full")

state, _ = wants.want(KID, media.MOVIE, "tmdb:100", "movie", {"title": "A"})
check.equal(state, wants.ON_ITS_WAY, "a new film request is on its way")
check.equal(wants.allowance(KID, media.MOVIE), 2, "a film spends one")

state, message = wants.want(KID, media.MOVIE, "tmdb:100", "movie", {"title": "A"})
check.equal(state, wants.ON_ITS_WAY, "asking again returns the same state")
check.equal(wants.allowance(KID, media.MOVIE), 2, "asking again spends nothing")
check.equal(len(added), 1, "asking again does not hand it to Radarr twice")

# Weighted costs: one artist is the whole day's music allowance.
check.equal(media.cost(media.MUSIC, "artist"), 3, "an artist costs three")
check.equal(media.cost(media.MUSIC, "track"), 1, "a track costs one")
wants.want(KID, media.MUSIC, "bk:artist:deezer:1", "artist",
           {"title": "Someone", "ref": "1", "source": "deezer"})
check.equal(wants.allowance(KID, media.MUSIC), 0,
            "one artist spends the day's music allowance")
check.raises(wants.Denied,
             lambda: wants.want(KID, media.MUSIC, "bk:track:abc", "track",
                                {"title": "A song", "artist": "Someone"}),
             "a track is refused once the music allowance is gone")

# Spending on one medium leaves the others alone.
check.equal(wants.allowance(KID, media.MOVIE), 2,
            "music spending does not touch the film allowance")

# A unit that costs more than the whole cap is refused with a useful reason.
media._registry[media.MUSIC] = media.Medium(
    media.MUSIC, "Music", buskarr.UNITS, 2, ("lib-music",))
try:
    wants.want(OTHER, media.MUSIC, "bk:artist:deezer:9", "artist",
               {"title": "Big", "ref": "9", "source": "deezer"})
    check.that(False, "an over-cap unit should be refused")
except wants.Denied as denied:
    check.that("album or a track" in str(denied),
               f"the refusal suggests a smaller unit, got {denied!r}")
media._registry[media.MUSIC] = media.Medium(
    media.MUSIC, "Music", buskarr.UNITS, 3, ("lib-music",))

# --- arrival ---------------------------------------------------------------

rows = wants.states(KID, media.MOVIE)
check.equal(rows[0]["state"], wants.ON_ITS_WAY, "an unarrived film is on its way")

set_owned(movie_tmdb=frozenset({"100"}))
rows = wants.states(KID, media.MOVIE)
check.equal(rows[0]["state"], wants.IN_LIBRARY,
            "a film whose TMDB id is in the library has arrived")
check.that(store.get(KID.key, media.MOVIE, "tmdb:100")["fulfilled_at"] is not None,
           "arrival is written down, so it is not re-derived every read")

# A series exists in Jellyfin the moment Sonarr creates it. That is not arrival.
wants.want(KID, media.SERIES, "tvdb:200", "series", {"title": "A show"})
set_owned(movie_tmdb=frozenset({"100"}), series_tvdb=frozenset({"200"}),
          episodes={"200": 0})
rows = [r for r in wants.states(KID, media.SERIES) if r["itemKey"] == "tvdb:200"]
check.equal(rows[0]["state"], wants.ON_ITS_WAY,
            "a series folder with no episode in it has not arrived")

set_owned(movie_tmdb=frozenset({"100"}), series_tvdb=frozenset({"200"}),
          episodes={"200": 3})
rows = [r for r in wants.states(KID, media.SERIES) if r["itemKey"] == "tvdb:200"]
check.equal(rows[0]["state"], wants.IN_LIBRARY,
            "a series with an episode in it has started arriving")
check.equal(rows[0]["episodesInLibrary"], 3, "and it says how much of it is here")

set_owned(movie_tmdb=frozenset({"100"}), series_tvdb=frozenset({"200"}))
rows = [r for r in wants.states(KID, media.SERIES) if r["itemKey"] == "tvdb:200"]
check.equal(rows[0]["state"], wants.IN_LIBRARY,
            "a series already marked arrived stays arrived when the count "
            "cannot be taken")
check.that("episodesInLibrary" not in rows[0],
           "and an unknown count is absent rather than reported as zero")

# Waiting long enough stops it being called "on its way".
wants.want(OTHER, media.MOVIE, "tmdb:300", "movie", {"title": "Slow"})
with store.db() as conn:
    conn.execute("UPDATE requests SET requested_at=? WHERE item_key='tmdb:300'",
                 (time.time() - 48 * 3600,))
rows = wants.states(OTHER, media.MOVIE)
check.equal(rows[0]["state"], wants.STILL_LOOKING,
            "past the threshold it is still looking, not on its way")

# --- cancelling ------------------------------------------------------------

stopped: list[str] = []
radarr.cancel = lambda backend_id: stopped.append(backend_id) or True

removed, _ = wants.cancel(OTHER, media.MOVIE, "tmdb:300")
check.that(removed, "a request can be taken back")
check.equal(stopped, ["r1"], "and the acquisition is called off")
check.equal(wants.allowance(OTHER, media.MOVIE), 3, "the allowance is refunded")

# Two people waiting on one film: the first to cancel must not delete it.
stopped.clear()
wants.want(KID, media.MOVIE, "tmdb:400", "movie", {"title": "Shared"})
wants.want(OTHER, media.MOVIE, "tmdb:400", "movie", {"title": "Shared"})
removed, message = wants.cancel(KID, media.MOVIE, "tmdb:400")
check.that(removed, "the first canceller's row goes")
check.equal(stopped, [],
            "but the film is not called off while somebody else waits")
check.that("still waiting" in message, "and the message says why")

removed, _ = wants.cancel(OTHER, media.MOVIE, "tmdb:400")
check.equal(stopped, ["r1"], "the last canceller does call it off")

removed, message = wants.cancel(KID, media.MOVIE, "tmdb:999")
check.that(not removed, "cancelling something never asked for is refused")

# A backend that will not answer must not strand the row on screen.
radarr.cancel = lambda backend_id: False
wants.want(KID, media.MOVIE, "tmdb:500", "movie", {"title": "Stuck"})
removed, message = wants.cancel(KID, media.MOVIE, "tmdb:500")
check.that(removed, "the ledger row goes even when the backend is down")
check.that(store.get(KID.key, media.MOVIE, "tmdb:500") is None,
           "and it is really gone")
check.that("could not be reached" in message, "and the message says so")

harness.cleanup()
raise SystemExit(check.report())
