"""Configured is a shape; reachable is a fact.

Conflating the two is what made a Radarr at the wrong port produce a working
search box that found nothing, with the only trace a warning line per search.
"""
import harness

harness.setup(
    RADARR_URL="http://radarr.invalid:7878", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="6",
    SONARR_URL="http://sonarr.invalid:8989", SONARR_API_KEY="k",
    # No profile, which is the one setting that has no sensible default.
    SONARR_QUALITY_PROFILE_ID="",
    LISTENARR_URL="http://listenarr.invalid:4545",
)

from app import backends, doctor  # noqa: E402

check = harness.Check("backends")

found = {status.medium: status for status in backends.statuses(force=True)}
check.equal(sorted(found), ["book", "movie", "music", "series"],
            "every backend is reported, configured or not")

# Configured and silent. The host does not resolve, which is what a wrong
# hostname, a wrong port and a stopped container all look like from here.
movie = found["movie"]
check.equal(movie.configured, True, "Radarr has the settings it needs")
check.equal(movie.reachable, False, "and did not answer")
check.equal(movie.usable, False, "so it is not usable")
check.that("could not be reached" in movie.detail,
           "with a sentence naming the address")
check.that("localhost" in movie.detail,
           "and the loopback trap named, because inside Docker that is this "
           "container rather than the host")

# Not configured at all. `reachable` is None rather than False: nothing was
# asked, and showing that the same way as a failed probe would send somebody
# looking for a network fault that is really an empty variable.
series = found["series"]
check.equal(series.configured, False,
            "a missing quality profile disqualifies Sonarr")
check.that(series.reachable is None, "so it was never asked")
check.that("SONARR_QUALITY_PROFILE_ID" in series.detail,
           "and the detail names the variable, not the concept")

music = found["music"]
check.equal(music.configured, False, "buskarr is not configured here")
check.that("BUSKARR_URL" in music.detail and "BUSKARR_API_KEY" in music.detail,
           "and both missing variables are named")

book = found["book"]
check.equal(book.configured, True, "a Listenarr URL is all books need")
check.equal(book.reachable, False, "and that host does not resolve either")

# Cached, because these are asked while somebody waits for a page.
probed = []
original = backends._probe
backends._probe = lambda *a, **kw: probed.append(a) or (True, "")
backends.statuses()
check.equal(probed, [], "a fresh answer is not re-probed")
backends.statuses(force=True)
check.that(len(probed) > 0, "and force asks again")
backends._probe = original

# --- what `python -m app.doctor` prints --------------------------------------
report = doctor.report()
check.that("radarr" in report and "not answering" in report,
           "the doctor names a backend that is configured and silent")
check.that("SONARR_QUALITY_PROFILE_ID" in report,
           "and the exact variable a half-configured backend is missing")
check.that(doctor.exit_code(report) != 0,
           "and exits non-zero, so it can gate a start-up script")

harness.cleanup()
raise SystemExit(check.report())
