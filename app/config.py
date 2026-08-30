"""Runtime configuration, all from environment so nothing secret lands in the image.

Every backend is optional and independently so. A deployment with only Radarr
configured serves films and says nothing about series or music -- that is the
ordinary case, not a degraded one, and it is why this service does not depend
on any particular acquisition tool being present.
"""
import os


def _text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _text(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _ids(name: str) -> list[str]:
    """A comma-separated list of Jellyfin ids, dashes and case normalised away."""
    return [x.strip().replace("-", "").lower()
            for x in _text(name).split(",") if x.strip()]


API_VERSION = 1

# Jellyfin is the single library of record. Nextup never treats an acquisition
# tool as a catalogue of what is owned: those tools know only what they
# themselves fetched, which on an established server is a small fraction of it.
JELLYFIN_URL = _text("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_TOKEN = os.environ["JELLYFIN_TOKEN"]

# The trusted forward-auth proxy supplies this for browser requests. The JSON
# API never reads it -- see `api.caller`.
AUTH_USER_HEADER = _text("AUTH_USER_HEADER", "X-Auth-Request-Preferred-Username")

# Deliberately empty, and deliberately not a name. This is the identity assumed
# for a browser request arriving with no proxy header. A default that names
# somebody means a fresh install elsewhere quietly resolves a person who does
# not exist there, so unset the pages refuse rather than guess.
JELLYFIN_USER = _text("JELLYFIN_USER")

# Which Jellyfin libraries each medium covers. Unset means "every view whose
# collection type matches", which is right on almost every server and saves an
# install from copying ids out of a URL.
MOVIE_LIBRARY_IDS = _ids("MOVIE_LIBRARY_IDS")
SERIES_LIBRARY_IDS = _ids("SERIES_LIBRARY_IDS")
MUSIC_LIBRARY_IDS = _ids("MUSIC_LIBRARY_IDS")

RADARR_URL = _text("RADARR_URL")
RADARR_API_KEY = _text("RADARR_API_KEY")
# No default. Radarr ships seven profiles and which one a household wants is a
# real choice about disk and bandwidth; picking one here would make that choice
# silently and wrongly. An unset profile disables films, and the log says so.
RADARR_QUALITY_PROFILE_ID = _int("RADARR_QUALITY_PROFILE_ID", 0)
# Discovered when there is exactly one, which is the usual arrangement. Unlike
# the quality profile this is not a judgement call -- there is nothing to choose
# between -- so discovery is safe and one less thing to configure.
RADARR_ROOT_FOLDER = _text("RADARR_ROOT_FOLDER")

SONARR_URL = _text("SONARR_URL")
SONARR_API_KEY = _text("SONARR_API_KEY")
SONARR_QUALITY_PROFILE_ID = _int("SONARR_QUALITY_PROFILE_ID", 0)
SONARR_ROOT_FOLDER = _text("SONARR_ROOT_FOLDER")
# What to monitor when a series is added. Sonarr's own vocabulary, passed
# through: `all`, `firstSeason`, `future`, `missing`, `existing`, `none`.
# `all` is the default because somebody asking for a series they do not have
# means the series, and a first-season default silently half-fills the request.
SONARR_MONITOR = _text("SONARR_MONITOR", "all")
SONARR_SEASON_FOLDER = _text("SONARR_SEASON_FOLDER", "true").lower() != "false"

BUSKARR_URL = _text("BUSKARR_URL")
# Buskarr's JSON API refuses every request unless this matches the key it was
# started with. Unset here disables music, exactly as an unset Radarr key
# disables films.
BUSKARR_API_KEY = _text("BUSKARR_API_KEY")

DB_PATH = _text("DB_PATH", "/data/nextup.db")

# How many catalogue hits to return. What a person can stand to hear read out
# in one list, not what the backend can produce.
SEARCH_LIMIT = _int("SEARCH_LIMIT", 25)

# Requests per account per day, per medium, for accounts that are not Jellyfin
# administrators. Separate counters because the media are not the same size:
# a film is one file, a series is a season or ten, and one artist resolved to
# 922 tracks on the library this was built against. One shared number would let
# a single series add spend what looks like one request and cost fifty.
MOVIE_DAILY_CAP = _int("MOVIE_DAILY_CAP", 3)
SERIES_DAILY_CAP = _int("SERIES_DAILY_CAP", 1)
MUSIC_DAILY_CAP = _int("MUSIC_DAILY_CAP", 3)

# What each kind of music request spends out of MUSIC_DAILY_CAP. An artist is
# a whole discography, so at the default cap it is the day's music allowance in
# one request -- which is the honest price rather than a refusal.
MUSIC_ARTIST_COST = _int("MUSIC_ARTIST_COST", 3)
MUSIC_ALBUM_COST = _int("MUSIC_ALBUM_COST", 1)
MUSIC_TRACK_COST = _int("MUSIC_TRACK_COST", 1)

# How long a request stays on the list after its media arrived. An arrival is
# the news, and a row that vanishes the moment it lands can only be noticed by
# its absence.
ARRIVED_VISIBLE_HOURS = _int("ARRIVED_VISIBLE_HOURS", 48)

# Past this, "on its way" would be a lie. Not a failure state: the request
# stays monitored and the acquisition tool's own sweep keeps retrying.
STILL_LOOKING_AFTER_HOURS = _int("STILL_LOOKING_AFTER_HOURS", 24)

# How long an introspected access token is trusted without re-asking Jellyfin.
# Short on purpose: expiry is the only thing that makes a token revoked in
# Jellyfin stop working here.
TOKEN_CACHE_SECONDS = _int("TOKEN_CACHE_SECONDS", 300)

# How long the library index -- what is already owned, by provider id -- is
# reused. Arrival is only ever as fresh as this.
OWNED_INDEX_TTL_SECONDS = _int("OWNED_INDEX_TTL_SECONDS", 900)

LOG_LEVEL = _text("LOG_LEVEL", "INFO").upper()
