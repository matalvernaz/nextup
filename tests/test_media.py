"""Which media the registry offers, and how long it keeps an unsettled answer.

Empty library ids are exactly what tell a client to show no control at all, so
an answer produced while the deployment was half up must not outlive it. The
first version of this cached any build Jellyfin managed to answer, which meant
a library created after the container started was never noticed.
"""
import harness

harness.setup(RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
              RADARR_QUALITY_PROFILE_ID="6")

from app import backends, config, jellyfin, media  # noqa: E402

check = harness.Check("media")

# The registry asks whether a configured backend is actually answering, and
# `radarr.invalid` never will. That question is tested on its own in
# test_backends.py; here it is held to "yes" so this file stays about the
# library ids, and turned back on at the end.
REACHABLE = backends.Status("movie", "radarr", configured=True, reachable=True)
backends.status = lambda medium, force=False: (
    REACHABLE if medium == "movie" else None)

LIBRARIES: dict[str, list[str]] = {"movie": []}


def fake_library_ids(medium):
    if LIBRARIES.get(medium) is None:
        raise jellyfin.JellyfinUnavailable("connection refused")
    return LIBRARIES.get(medium, [])


media.jellyfin.library_ids = fake_library_ids


# --- a configured backend whose library does not exist yet ------------------
media.forget()
offered = media.available()
check.that("movie" in offered, "a configured backend is offered before its library exists")
check.equal(offered["movie"].library_ids, (), "with no library ids, which is honest")
check.that(not media._registry_settled,
           "and the registry knows it has not settled")

# Create the library. The next read past the retry window has to see it, or a
# client is told there is nothing to show until somebody restarts the container.
LIBRARIES["movie"] = ["lib-films"]
media._registry_built_at -= media.PROVISIONAL_TTL_SECONDS + 1
check.equal(media.available()["movie"].library_ids, ("lib-films",),
            "a library created later is picked up")
check.that(media._registry_settled, "and now the registry has settled")

# Settled means settled: no further Jellyfin round trips.
asked = []
media.jellyfin.library_ids = lambda medium: asked.append(medium) or ["lib-films"]
media.available()
media.available()
check.equal(asked, [], "a settled registry is not rebuilt on every request")
media.jellyfin.library_ids = fake_library_ids


# --- Jellyfin unreachable at build time -------------------------------------
media.forget()
LIBRARIES["movie"] = None
unreachable = media.available()
check.that("movie" in unreachable,
           "a medium is still offered when Jellyfin could not be asked")
check.that(not media._registry_settled, "but that answer is not settled either")

LIBRARIES["movie"] = ["lib-films"]
media._registry_built_at -= media.PROVISIONAL_TTL_SECONDS + 1
check.equal(media.available()["movie"].library_ids, ("lib-films",),
            "and it is corrected once Jellyfin answers")

# The unsettled answer is still cached for a little while, or every request
# during an outage would pay for its own Jellyfin round trip.
media.forget()
LIBRARIES["movie"] = None
media.available()
asked = []
media.jellyfin.library_ids = lambda medium: asked.append(medium) or []
media.available()
check.equal(asked, [], "an unsettled registry is cached for the retry window")


# --- a backend that is configured and not answering --------------------------
#
# The medium stays. Dropping it would take away the list of what somebody has
# already asked for at exactly the moment they want to know what became of it,
# and an acquisition tool restarting is a normal minute in a homelab.
SILENT = backends.Status("movie", "radarr", configured=True, reachable=False,
                         detail="http://radarr.invalid/... could not be reached")
backends.status = lambda medium, force=False: (
    SILENT if medium == "movie" else None)
media.forget()
LIBRARIES["movie"] = ["lib-films"]
media.jellyfin.library_ids = fake_library_ids
silent = media.available()
check.that("movie" in silent,
           "a medium whose backend is silent is still offered")
check.equal(silent["movie"].backend_reachable, False,
            "and says so rather than looking healthy")
check.that("could not be reached" in silent["movie"].backend_detail,
           "carrying the reason, so a client can name it")
check.that(not media._registry_settled,
           "an outage is not a settled fact, so it is asked again")

# --- a settled registry is still asked again eventually ----------------------
# It used to have no lifetime at all, so a library created later, or a backend
# that stopped answering after the build, stayed invisible until somebody saved
# a setting or restarted the container.
check.that(media.SETTLED_TTL_SECONDS > media.PROVISIONAL_TTL_SECONDS,
           "a settled registry outlives an unsettled one")
backends.status = lambda medium, force=False: None
media.forget()
LIBRARIES["movie"] = ["lib-films"]
settled = media.available()
check.that(media._registry_settled, "a complete build settles")
builds = []
real_build = media._build
media._build = lambda: builds.append(1) or real_build()
media.available()
check.equal(builds, [], "and is served from memory while it is young")
media._registry_built_at -= media.SETTLED_TTL_SECONDS + 1
media.available()
check.equal(len(builds), 1, "and asked again once it is not")
media._build = real_build

# --- an outage is not an empty library ---------------------------------------
# `_items` used to answer an HTTP failure with an empty list, and this cache
# kept it for a quarter of an hour: everything the library holds then read as
# not owned, so owned films were offered as askable and every outstanding
# request read as not yet arrived.
GOOD = jellyfin.Owned(frozenset({"1"}), frozenset(), frozenset(), {})


def unavailable():
    raise jellyfin.JellyfinUnavailable("no route to host")


media._owned.forget()
media.jellyfin.owned_index = unavailable
raised = False
try:
    media.owned()
except jellyfin.JellyfinUnavailable:
    raised = True
check.that(raised, "with nothing to fall back on, the caller is told")

media.jellyfin.owned_index = lambda: GOOD
check.equal(media.owned(force=True).movie_tmdb, frozenset({"1"}),
            "a good index is kept")
media.jellyfin.owned_index = unavailable
check.equal(media.owned(force=True).movie_tmdb, frozenset({"1"}),
            "and served through an outage rather than replaced by an empty one")

harness.cleanup()
raise SystemExit(check.report())
