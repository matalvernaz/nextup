"""The first run, from a container with nothing configured.

The old first run was: create an API key in Jellyfin's dashboard, find two
quality-profile ids in two other applications' URLs, write five variables into
a file, and only then learn whether any of it was right. This is the same
information asked one piece at a time, with each answer checked before it is
kept.
"""
import harness

# Nothing at all in the environment: no Jellyfin token, no backend. Importing
# the app used to be impossible in this state -- `config` read JELLYFIN_TOKEN
# with os.environ[...] and a container with nothing set could not start.
DB = harness.setup()
import os  # noqa: E402

for name in ("JELLYFIN_TOKEN", "JELLYFIN_URL", "JELLYFIN_USER"):
    os.environ.pop(name, None)

from fastapi.testclient import TestClient  # noqa: E402

from app import (backends, config, jellyfin, main, media, settings,  # noqa: E402
                 setup, store)

check = harness.Check("setup pages")
store.init()
settings.forget()

check.equal(config.JELLYFIN_TOKEN, "",
            "a container with nothing configured still starts")
check.equal(setup.needs_setup(), True, "and knows it needs setting up")

client = TestClient(main.app, raise_server_exceptions=False)

# --- a first run goes to the one page that needs no credential ---------------
first = client.get("/", follow_redirects=False)
check.equal(first.status_code, 303, "the ordinary page defers on a first run")
check.equal(first.headers.get("location"), "/setup", "to the setup page")

page = client.get("/setup")
check.equal(page.status_code, 200, "which renders without any credential")
check.that("administrator" in page.text,
           "and says an administrator account is what is needed")
check.that("localhost" in page.text,
           "and warns about the address trap it cannot check for you")

# --- signing in as somebody who is not an administrator ----------------------
ADMIN = jellyfin.User(id="u1", name="matt", is_admin=True)
MEMBER = jellyfin.User(id="u2", name="kadija", is_admin=False)


def authenticate(username, password, device):
    if password != "right":
        raise jellyfin.TokenRejected("Jellyfin did not accept that.")
    return f"token-for-{username}", ADMIN if username == "matt" else MEMBER


jellyfin.authenticate = authenticate
setup._make_api_key = lambda token: None

refused = client.post("/setup", data={"jellyfin_url": "http://jf:8096",
                                      "username": "kadija",
                                      "password": "right"},
                      follow_redirects=False)
check.equal(refused.status_code, 303, "a member is redirected back")
check.that("administrator" in refused.headers.get("location", ""),
           "and told why, because reading every library needs one")
check.equal(config.JELLYFIN_TOKEN, "", "with no credential kept")

# --- and as one who is ---------------------------------------------------------
ok = client.post("/setup", data={"jellyfin_url": "http://jf:8096",
                                 "username": "matt", "password": "right"},
                 follow_redirects=False)
check.equal(ok.status_code, 303, "an administrator is accepted")
check.equal(ok.headers.get("location", "").split("?")[0], "/backends",
            "and sent on to connect the tools they run")
check.equal(config.JELLYFIN_TOKEN, "token-for-matt",
            "the credential is kept")
check.equal(config.JELLYFIN_URL, "http://jf:8096",
            "and so is the address that was typed")
check.equal(setup.needs_setup(), False, "so setup is no longer needed")

# When Jellyfin will not issue a key of its own, the sign-in token is used and
# the page says so -- signing out everywhere would otherwise disconnect this
# service with no explanation.
check.that("access token" in ok.headers.get("location", "")
           or "access%20token" in ok.headers.get("location", ""),
           "and the fallback credential is named rather than hidden")

# --- the backends page is administrators only --------------------------------
jellyfin.user_from_token = lambda token: (
    ADMIN if token == "token-for-matt" else MEMBER)
jellyfin.user = lambda name=None: MEMBER
REAL_AVAILABLE = media.available
media.available = lambda: {}

as_member = client.get("/backends")
check.equal(as_member.status_code, 403,
            "a member cannot change where this connects")
check.that("administrator" in as_member.text,
           "and is told why rather than shown an empty page")

# --- saving a backend --------------------------------------------------------
jellyfin.user = lambda name=None: ADMIN
backends._probe = lambda *args, **kwargs: (True, "")
backends.quality_profiles = lambda url, key: [backends.Choice("4", "HD-1080p")]
backends.root_folders = lambda url, key: [backends.Choice("/movies", "/movies")]

saved = client.post("/backends", data={
    "RADARR_URL": "http://radarr:7878",
    "RADARR_API_KEY": "a-key",
    "RADARR_QUALITY_PROFILE_ID": "4",
}, follow_redirects=False)
check.equal(saved.status_code, 303, "saving redirects rather than re-rendering")
check.equal(config.RADARR_URL, "http://radarr:7878", "the address is kept")
check.equal(config.RADARR_QUALITY_PROFILE_ID, 4,
            "and the profile, as a number")
check.that("answered" in saved.headers.get("location", ""),
           "and the answer is whether it responded, not merely that it saved")

# The registry has to have been dropped, or a saved backend would not be
# offered until somebody restarted the container -- a page that appears to do
# nothing.
media.available = REAL_AVAILABLE
media.jellyfin.library_ids = lambda medium: ["lib-" + medium]
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
check.that("movie" in media.available(),
           "and films are offered straight away, without a restart")

# --- a profile that is not a number is refused, not stored -------------------
refused_profile = setup.save({"RADARR_QUALITY_PROFILE_ID": "six"})
check.that(any("whole number" in line for line in refused_profile),
           "a profile that will not parse is refused at save")
check.equal(config.RADARR_QUALITY_PROFILE_ID, 4,
            "and the working value is left alone")

# --- an empty secret box means 'leave it', not 'delete it' -------------------
setup.save({"RADARR_API_KEY": ""})
check.equal(config.RADARR_API_KEY, "a-key",
            "a blank key field is the normal state of one already set")

# --- the environment wins, and says so ---------------------------------------
os.environ["RADARR_URL"] = "http://from-the-environment:7878"
settings.forget()
held = setup.save({"RADARR_URL": "http://typed-into-the-page:7878"})
check.that(any("environment" in line for line in held),
           "a setting the environment holds is refused with a reason")
check.equal(config.RADARR_URL, "http://from-the-environment:7878",
            "and the environment's value is what is used")
check.equal(settings.get("RADARR_URL"), "http://radarr:7878",
            "the stored one is left untouched rather than overwritten with a "
            "value that would never be read")
del os.environ["RADARR_URL"]
settings.forget()

harness.cleanup()
raise SystemExit(check.report())
