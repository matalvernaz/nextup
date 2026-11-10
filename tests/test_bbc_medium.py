"""BBC is registered as a medium, told only to protocol 4, and unreachable by every older route.

Chunk 2c of the BBC plan: the medium exists in the registry with its cap, its unit and the TV
libraries it shares with series, the backends probe asks bbc-ui's health, and the Backends page
can set its address and secret. What must NOT happen yet is any way to search for or ask for a
BBC programme, because the request path that would handle one is a later chunk. So every
acquisition backend is replaced here by a fake that fails the test if BBC reaches it, and the
ledger is checked afterwards for any BBC row at all. Cancelling and listing read stored rows, not
the registry, so for them this proves only that no BBC row can be stored yet, plus that a stored
one could never be handed to buskarr. A BBC outage must not change how long the registry of the
other media is cached, either.
"""
import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k", RADARR_QUALITY_PROFILE_ID="6",
    SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k", SONARR_QUALITY_PROFILE_ID="4",
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    LISTENARR_URL="http://listenarr.invalid:4545",
    SERIES_LIBRARY_IDS="lib-tv",
    BBC_UI_URL="http://bbc-ui.invalid:8080", BBC_UI_TOKEN="shared-secret",
)
harness.forbid_network()

import json  # noqa: E402
import os  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, backends, books, buskarr, config, held, jellyfin, media,  # noqa: E402
                 radarr, settings, setup, sonarr, store, wants)
from app.bbc import client as bbc_client, contract  # noqa: E402

check = harness.Check("bbc medium")
store.init()

USER = jellyfin.User(id="u1", name="matt", is_admin=False)
jellyfin.user_from_token = lambda token: USER

# Every backend answers, and every library the registry asks about exists.
reachability = {"bbc": (True, "")}


def fake_status(medium, force=False):
    reachable, detail = reachability.get(medium, (True, ""))
    return backends.Status(medium, medium, configured=True, reachable=reachable, detail=detail)


real_status = backends.status
backends.status = fake_status
real_library_ids = jellyfin.library_ids
media.jellyfin.library_ids = lambda medium: {
    "movie": ["lib-films"], "series": ["lib-tv"], "music": ["lib-music"],
    "book": ["lib-books"], "bbc": ["lib-tv"]}.get(medium, [])
media.forget()

# --- 1. BBC reads the TV libraries, because that is where its downloads are imported --------
check.that(real_library_ids("bbc") == real_library_ids("series") != [],
           "the bbc medium is given the series libraries")
check.equal(jellyfin.COLLECTION_TYPES.get("bbc"), "tvshows",
    "and with none configured it would look for TV libraries")

# --- 2. In the registry, but kept out of everything an older route reads ---------------------
check.that(media.BBC not in media.available(), "available() leaves BBC out")
check.that(media.get(media.BBC) is None, "so get('bbc') answers None, as for a medium never served")
check.equal(sorted(media.offered(3)), sorted(media.available()), "offered(3) is exactly available()")
check.that(media.BBC in media.offered(4), "offered(4) includes BBC")
found = media.lookup(media.BBC)
check.that(found is not None, "lookup finds BBC in the whole registry")
if found is not None:
    check.equal(found.units, ("episode",), "its one unit is an episode")
    check.equal(found.daily_cap, 10, "its cap is BBC_DAILY_CAP")
    check.equal(found.library_ids, ("lib-tv",), "and its libraries are the TV ones")
    check.equal(found.label, "BBC iPlayer", "it is called BBC iPlayer")

# --- 3. Protocols 1 to 3 answer byte for byte what they did without BBC ---------------------
app = FastAPI()
app.include_router(api.router)
http = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-Emby-Token": "a-real-looking-token"}


def capabilities(protocol):
    response = http.get(f"/api/v1/capabilities?protocol={protocol}", headers=AUTH)
    return response.status_code, json.dumps(response.json(), sort_keys=True)


with_bbc = {protocol: capabilities(protocol) for protocol in (1, 2, 3)}
os.environ["BBC_UI_URL"] = ""
media.forget()
check.that(media.lookup(media.BBC) is None, "with no address BBC is not in the registry at all")
without_bbc = {protocol: capabilities(protocol) for protocol in (1, 2, 3)}
for protocol in (1, 2, 3):
    check.equal(with_bbc[protocol], without_bbc[protocol],
        f"protocol {protocol} is unchanged by a configured BBC")
os.environ["BBC_UI_URL"] = "http://bbc-ui.invalid:8080"
media.forget()

# --- 4. Protocol 4 is told about BBC, with the numbers its request path will use ------------
four = http.get("/api/v1/capabilities?protocol=4", headers=AUTH)
check.equal(four.status_code, 200, "protocol 4 is answered")
blocks = {block["medium"]: block for block in four.json().get("media", [])}
check.that(media.BBC in blocks, "and lists BBC")
block = blocks.get(media.BBC, {})
check.equal(block.get("units"), ["episode"], "BBC is asked for an episode at a time")
check.equal(block.get("unitCosts"), {"episode": 1}, "an episode costs one")
check.equal(block.get("dailyCap"), 10, "an ordinary account is capped at BBC_DAILY_CAP")
check.equal(block.get("remainingToday"), 10, "and has all of it left")
check.equal(block.get("libraryIds"), ["lib-tv"], "BBC names the TV libraries")
check.that(block.get("backendReachable") is True, "a healthy bbc-ui reads as reachable")
three = http.get("/api/v1/capabilities?protocol=3", headers=AUTH).json().get("media", [])
check.equal(sorted(m for m in blocks if m != media.BBC), sorted(b["medium"] for b in three),
    "and the other media are the ones protocol 3 lists")
reachability["bbc"] = (False, "bbc-ui answered, but its UK tunnel is down")
media.forget()
down = {block["medium"]: block for block in http.get(
    "/api/v1/capabilities?protocol=4", headers=AUTH).json().get("media", [])}
check.that(media.BBC in down, "a bbc-ui with its tunnel down is still listed")
check.that(down.get(media.BBC, {}).get("backendReachable") is False, "as not reachable")
check.that("tunnel" in down.get(media.BBC, {}).get("backendDetail", ""), "saying why")
reachability["bbc"] = (True, "")
media.forget()

# --- 4b. A BBC outage leaves the other media's registry alone --------------------------------
# An unreachable backend unsettles the build it is in, and an unsettled build is kept 5 minutes
# instead of an hour. With one cache for every medium, a UK tunnel that is down would have had
# films, series, music and books rebuilt twelve times as often, each time a Jellyfin round trip.
builds = []
real_build = media._build
media._build = lambda later=False: builds.append(later) or real_build(later)
reachability["bbc"] = (False, "bbc-ui answered, but its UK tunnel is down")
media.forget()
media.offered(4)
check.that(media._registry_settled, "with BBC down, the other media's registry still settles")
check.that(not getattr(media, "_later_settled", True), "while BBC's own is kept only briefly")
builds.clear()
media._later_built_at = getattr(media, "_later_built_at", 0.0) - media.PROVISIONAL_TTL_SECONDS - 1
media.offered(4)
check.equal(builds, [True], "and once that runs out, only BBC is built again")
media._build = real_build
reachability["bbc"] = (True, "")
media.forget()


# --- 5. Nothing can search for or ask for a BBC programme yet -------------------------------
def must_not_be_reached(name):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a BBC call reached {name}")
    return refuse


for module, names in ((radarr, ("search", "add", "cancel")), (sonarr, ("search", "add", "cancel")),
        (buskarr, ("search", "add", "cancel")), (books, ("search_hits", "want", "cancel"))):
    for name in names:
        setattr(module, name, must_not_be_reached(f"{module.__name__}.{name}"))

PID = "bbc:p0000001"
check.equal(wants.search("Horrible Histories", media.BBC, user=USER), [], "a BBC search finds nothing")
check.raises(wants.Denied, lambda: wants.want(USER, media.BBC, PID, "episode"), "a BBC ask is refused")
# Cancelling and listing find nothing because nothing above could store a row, not because of the gate.
check.equal(wants.cancel(USER, media.BBC, PID)[0], False, "with no BBC row stored, a cancel finds nothing")
check.equal(wants.states(USER, media.BBC), [], "and a list shows nothing")
# And a stored BBC row could never be handed to buskarr: the cancel dispatch no longer falls through to it.
check.equal(wants._stop(media.BBC, {"backend_id": "job-1"}), False,
    "a BBC row's cancel is refused rather than sent to buskarr")

search = http.get("/api/v1/search?medium=bbc&q=Horrible", headers=AUTH)
check.equal(search.status_code, 404, "the search route says it does not serve BBC")
want = http.post("/api/v1/want", headers=AUTH, json={"medium": "bbc", "itemKey": PID, "unit": "episode"})
check.that(want.status_code >= 400, f"the want route refuses BBC ({want.status_code})")
cancel = http.post("/api/v1/cancel", headers=AUTH, json={"medium": "bbc", "itemKey": PID})
check.equal(cancel.status_code, 404, "the cancel route finds no stored BBC row")
listed = http.get("/api/v1/requests?medium=bbc&held=true", headers=AUTH)
check.equal(listed.json().get("requests"), [], "the requests route lists nothing for BBC")
check.that("bbc" not in listed.json().get("remainingToday", {}),
    "and its allowances are what an older client has always been told")
check.that(store.get(USER.key, media.BBC, PID) is None, "no BBC row reached the ledger")
check.equal(held.described_for(USER, media.BBC), [], "and none is waiting for a keyholder")

# --- 6. The probe asks bbc-ui's health, with the probe's own short timeout ------------------
backends.status = real_status
seen = {}


def fake_health(timeout=bbc_client.TIMEOUT):
    seen["timeout"] = timeout
    return seen["answer"]


real_health = bbc_client.health
bbc_client.health = fake_health
seen["answer"] = contract.Health(ok=True, tunnel_ok=True)
healthy = backends._bbc()
check.that(healthy.configured and healthy.reachable is True, "a healthy bbc-ui is reachable")
check.that(seen.get("timeout") is backends.PROBE_TIMEOUT, "and was asked with the probe's timeout")
seen["answer"] = contract.Health(ok=True, tunnel_ok=False)
tunnel_down = backends._bbc()
check.that(tunnel_down.reachable is False and "tunnel" in tunnel_down.detail,
    "a bbc-ui with its tunnel down is not reachable, and says the tunnel is why")
seen["answer"] = contract.Health(ok=False, tunnel_ok=True)
check.that(backends._bbc().reachable is False, "a bbc-ui that says it is not working is not reachable")


def unreachable(timeout=bbc_client.TIMEOUT):
    raise bbc_client.BbcUnavailable("bbc-ui health could not be reached: ConnectError")


bbc_client.health = unreachable
gone = backends._bbc()
check.that(gone.configured and gone.reachable is False and "ConnectError" in gone.detail,
    "a bbc-ui that cannot be reached says so")
os.environ["BBC_UI_URL"] = ""
unset = backends._bbc()
check.that(not unset.configured and unset.reachable is None, "with no address nothing is asked")
os.environ["BBC_UI_URL"] = "http://bbc-ui.invalid:8080"
check.that(backends._bbc in backends._PROBES, "and the probe is one of the backends asked")

# The real health call never carries the shared secret.
bbc_client.health = real_health
sent = {}


def capture_get(url, headers=None, timeout=None, **kwargs):
    sent["headers"] = dict(headers or {})
    sent["timeout"] = timeout
    return httpx.Response(200, json={"schema_version": contract.SCHEMA_VERSION, "ok": True, "tunnel_ok": True},
        request=httpx.Request("GET", url))


real_get = bbc_client.httpx.get
bbc_client.httpx.get = capture_get
answer = bbc_client.health(timeout=backends.PROBE_TIMEOUT)
bbc_client.httpx.get = real_get
check.that(answer.ok and answer.tunnel_ok, "the real health call reads a healthy reply")
check.that(bbc_client.TOKEN_HEADER not in sent.get("headers", {}), "and sends no shared secret")
check.that(sent.get("timeout") is backends.PROBE_TIMEOUT, "with the timeout it was given")

# --- 7. The Backends page can set bbc-ui's address and secret -------------------------------
forms = {form.key: form for form in setup.forms()}
check.that("bbc" in forms, "the Backends page has a BBC form")
fields = {field.name: field for field in forms.get("bbc").fields} if "bbc" in forms else {}
check.equal(sorted(fields), ["BBC_UI_TOKEN", "BBC_UI_URL"], "with the address and the secret")
check.that(fields.get("BBC_UI_TOKEN") is not None and fields["BBC_UI_TOKEN"].secret
    and fields["BBC_UI_TOKEN"].value == "", "the secret is never shown back")
check.that({"BBC_UI_URL", "BBC_UI_TOKEN"} <= settings.WRITABLE, "both can be saved from the page")
check.that("BBC_UI_TOKEN" in settings.SECRET, "and the secret is kept out of logs and pages")

harness.cleanup()
raise SystemExit(check.report())
