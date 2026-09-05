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
    """A whole number from the environment, or the default when it is unset.

    A value that is present but will not parse raises. Reading
    `RADARR_QUALITY_PROFILE_ID=six` as the default of 0 instead disqualifies
    the backend outright -- `Arr.configured` requires a profile above zero --
    so films stop being offered at all, on a container that starts, reports
    healthy and logs only that a backend is not configured. A container that
    refuses to start and names the variable is far easier to find.
    """
    raw = _text(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"{name} must be a whole number, not {raw!r}") from None


def _ids(name: str) -> list[str]:
    """A comma-separated list of Jellyfin ids, dashes and case normalised away."""
    return [x.strip().replace("-", "").lower()
            for x in _text(name).split(",") if x.strip()]


SERVICE_NAME = "nextup"

API_VERSION = 1

# Where clients reach this service at the Jellyfin origin, e.g.
# "https://jellyfin.example.com/nextup". Optional, and used only to check that
# route is really there -- see app/selfcheck.py. Unset means the check does not
# run, which is right for an install serving only the browser pages.
PUBLIC_URL = _text("PUBLIC_URL").rstrip("/")

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

# --- Books ------------------------------------------------------------------
#
# Listenarr acquires; Audible supplies the similarity graph and the blurbs;
# Jellyfin remains the library of record. As with every other backend, an
# unset URL means this deployment simply does not offer books.

# No default. The audiobook service this came from defaulted to
# "http://listenarr:4545", which was right when it was the only thing running
# and wrong the moment the code shipped to somebody else: a default URL makes
# `configured` true by shape, so books were offered on installs where nothing
# was listening.
LISTENARR_URL = _text("LISTENARR_URL")
LISTENARR_QUALITY_PROFILE_ID = _int("LISTENARR_QUALITY_PROFILE_ID", 1)

# Which Jellyfin libraries hold audiobooks. Unset means every view whose
# collection type matches, the same rule the other three media follow.
BOOK_LIBRARY_IDS = _ids("BOOK_LIBRARY_IDS")

# Marketplaces to consult, in order. A LIST, not one value, because a library
# can genuinely span two: measured 2026-08-28, B0CWW1L8NL exists on audible.ca
# and not on .com while B0HC7V8ZR4 exists on .com and not on .ca. Whichever
# single region were chosen, the other store's books would answer with an
# empty product -- and an empty product is what let a request be filled with
# the wrong book. First is preferred: it decides ties and it is the region an
# item is filed under when both stores have it.
AUDIBLE_REGIONS = [r.strip().lower()
                   for r in _text("AUDIBLE_REGIONS", "ca,us").split(",")
                   if r.strip()] or ["ca"]

#: The preferred marketplace, for callers that can only carry a single value.
AUDIBLE_REGION = AUDIBLE_REGIONS[0]

# The one persistent Jellyfin playlist the book shelf is written into, updated
# in place. Recreating it would churn item ids and reset the client's view
# every run. A playlist and not a collection: collections are server-global
# and a shelf belongs to one account.
PLAYLIST_NAME = _text("PLAYLIST_NAME", "Next Read")

# How long a cached Audible similar-products response stays fresh. That
# endpoint is unauthenticated and must never be hit on a page load.
SIMS_TTL_HOURS = _int("SIMS_TTL_HOURS", 168)

# A blurb and a runtime, which change about as often as a book is re-issued.
PRODUCT_TTL_HOURS = _int("PRODUCT_TTL_HOURS", 720)

# Audible caps sims responses; ask for a useful spread per seed.
SIMS_PER_SEED = _int("SIMS_PER_SEED", 10)

# How many recommendations a book shelf shows.
MAX_SHELF = _int("MAX_SHELF", 40)

# How many books one "ask for the rest of this series" tap may request. A
# series can run to forty volumes, and one tap quietly turning into forty
# acquisitions is not what anybody meant by it. The rest are asked for on the
# next tap, which skips whatever is already on order. Non-keyholders are
# bounded by BOOK_DAILY_CAP as well, whichever is smaller.
SERIES_WANT_LIMIT = _int("SERIES_WANT_LIMIT", 10)

# Signed rating mode -- where a low score pushes a neighbourhood away rather
# than merely failing to pull it -- begins here and reaches full strength over
# RATINGS_RAMP_SPAN more ratings. A hard gate was tried first and produced
# exactly the lurch it was written to prevent: at the threshold every unrated
# seed dropped from parity with a rated one in a single pass, so half the
# shelf reordered the moment one rating landed.
MIN_RATINGS_FOR_SIGNED_MODE = _int("MIN_RATINGS_FOR_SIGNED_MODE", 5)
RATINGS_RAMP_SPAN = _int("RATINGS_RAMP_SPAN", 15)

# Jellyfin item ids whose rating is known to be wrong and cannot be corrected
# -- Jellyfin has no route that clears one, only one that overwrites it.
#
# Empty by default. It carried one hardcoded id for a long time, which was
# correct for the library it was written against and meaningless anywhere
# else: a fresh install elsewhere would have silently discounted whichever of
# its own books happened to share that id.
IGNORED_RATING_ITEM_IDS = frozenset(_ids("IGNORED_RATING_ITEM_IDS"))

# Keyword search is the only channel that can surface a book with no
# connection whatever to a finished one -- and it is OFF, because measured on
# real data it does not work yet. Audible's genre tags are far too broad
# ("Children's Audiobooks", inherited from full-cast editions, returned The
# Gruffalo), and at a small seed count the TF-IDF profile's own top terms are
# proper nouns and blurb boilerplate rather than a genre signature. What would
# fix it is more ratings, not more code.
KEYWORD_PULL_ENABLED = _text("KEYWORD_PULL_ENABLED", "false").lower() == "true"
KEYWORD_QUERIES_MAX = _int("KEYWORD_QUERIES_MAX", 4)
KEYWORD_SHELF_SHARE = float(_text("KEYWORD_SHELF_SHARE", "0.25"))

# A dismissal means "not now", not an irreversible judgement. Taste changes,
# editions change, and an accidental tap must not suppress a book forever.
DISMISS_TTL_DAYS = _int("DISMISS_TTL_DAYS", 30)

# Recommendation snapshots make requests and dismissals attributable to the
# ranker run that produced them. Kept long enough to compare outcomes across a
# few release cycles without turning a small SQLite file into an unbounded
# ledger.
ATTRIBUTION_RETENTION_DAYS = _int("ATTRIBUTION_RETENTION_DAYS", 180)

BOOK_DAILY_CAP = _int("BOOK_DAILY_CAP", 3)

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
TOKEN_CACHE_SECONDS = _int("TOKEN_CACHE_SECONDS", 60)

# How long the library index -- what is already owned, by provider id -- is
# reused. Arrival is only ever as fresh as this.
OWNED_INDEX_TTL_SECONDS = _int("OWNED_INDEX_TTL_SECONDS", 900)

# Owned recommendation surfaces read Jellyfin only, so they add no catalogue
# account or API key to an installation. Results are cached per user, medium,
# and library: building one means asking Jellyfin for every eligible item with
# that user's play state attached.
MOVIE_RECOMMENDATION_LIMIT = _int("MOVIE_RECOMMENDATION_LIMIT", 20)
MOVIE_RECOMMENDATION_CACHE_SECONDS = _int(
    "MOVIE_RECOMMENDATION_CACHE_SECONDS", 3600)
SERIES_RECOMMENDATION_LIMIT = _int("SERIES_RECOMMENDATION_LIMIT", 20)
SERIES_RECOMMENDATION_CACHE_SECONDS = _int(
    "SERIES_RECOMMENDATION_CACHE_SECONDS", 3600)

LOG_LEVEL = _text("LOG_LEVEL", "INFO").upper()
