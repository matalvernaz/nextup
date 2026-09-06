"""The JSON API must authenticate on the token alone.

This is the hazard the whole module shape exists for. The browser pages
resolve identity from a proxy header and fall back to a configured user; the
API is not behind that proxy, so if it ever shared that resolver, a request
carrying only a forged header would be served somebody else's list and spend
somebody else's allowance.
"""
import harness

harness.setup()

from fastapi.testclient import TestClient  # noqa: E402  (after harness.setup)

from app import api, config, jellyfin  # noqa: E402

check = harness.Check("api auth")


def client_with(monkeypatched, prefixes=False):
    """A test client whose Jellyfin introspection is `monkeypatched`.

    `prefixes` mounts the three the real application mounts, for the few
    assertions that are about routing rather than about authentication.
    """
    from fastapi import FastAPI
    jellyfin.user_from_token = monkeypatched
    app = FastAPI()
    app.include_router(api.router)
    if prefixes:
        from app import compat_nextread
        app.include_router(api.router, prefix="/nextup")
        app.include_router(compat_nextread.router)
    return TestClient(app, raise_server_exceptions=False)


def rejects(_token):
    raise jellyfin.TokenRejected("unknown token")


def unavailable(_token):
    raise jellyfin.JellyfinUnavailable("connection refused")


def accepts(_token):
    return jellyfin.User(id="u1", name="tester", is_admin=False)


client = client_with(rejects)

r = client.get("/api/v1/capabilities")
check.equal(r.status_code, 401, "no token at all is refused")

# The exact shape of the hazard: a request that carries the proxy's identity
# header and nothing else. The browser resolver would accept this.
r = client.get("/api/v1/capabilities",
               headers={"X-Auth-Request-Preferred-Username": "matt"})
check.equal(r.status_code, 401, "a proxy header alone is refused")

r = client.get("/api/v1/capabilities", headers={"X-Emby-Token": "nonsense"})
check.equal(r.status_code, 401, "an unknown token is refused")

client = client_with(unavailable)
r = client.get("/api/v1/capabilities", headers={"X-Emby-Token": "anything"})
check.equal(r.status_code, 503,
            "an unreachable Jellyfin is 503, never a guess at authorisation")

# Tokens are cached by digest. A rejection must not be able to reuse the
# acceptance of a different token.
api._tokens.clear()
client = client_with(accepts)
r = client.get("/api/v1/capabilities", headers={"X-Emby-Token": "good-token"})
check.equal(r.status_code, 200, "a real token is accepted")

client = client_with(rejects)
r = client.get("/api/v1/capabilities", headers={"X-Emby-Token": "other-token"})
check.equal(r.status_code, 401,
            "a second, unknown token does not inherit the first one's cache")

# --- a silent backend is published, not merely logged ------------------------
# The medium stays on offer while its acquisition tool is down, because taking
# it away would also take away the list of what has already been asked for. So
# a client needs to be told, or an outage looks exactly like a catalogue with
# nothing in it.
from app import buskarr, media, store  # noqa: E402

media._registry = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 3,
                              ("lib-movies",), False, "radarr did not answer."),
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                              ("lib-music",)),
}
media._registry_built_at = float("inf")
media._registry_settled = True

store.init()
client = client_with(accepts)
body = client.get("/api/v1/capabilities",
                  headers={"X-Emby-Token": "good-token"}).json()
blocks = {block["medium"]: block for block in body["media"]}
check.equal(blocks["movie"]["backendReachable"], False,
            "a medium whose backend is silent says so in capabilities")
check.equal(blocks["movie"]["backendDetail"], "radarr did not answer.",
            "carrying the reason, so a client can name it")
check.equal(blocks["music"]["backendReachable"], True,
            "and a healthy one is not made to look broken")
media.forget()

# --- an outage answers in JSON on the JSON routes ---------------------------
# The application's own handler renders an HTML page, which is right for the
# browser pages and useless to a client that asked for JSON. The
# recommendation route already did this; search and requests did not.
media._registry = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 3,
                              ("lib-movies",)),
}
media._registry_built_at = float("inf")
media._registry_settled = True


def no_index(*_args, **_kwargs):
    raise jellyfin.JellyfinUnavailable("connection refused")


media._owned.forget()
media.jellyfin.owned_index = no_index
# One row, or `states` answers an empty list without ever needing the index --
# which is correct, and would make this check pass for the wrong reason.
store.record("u1", media.MOVIE, "tmdb:1", "movie", "A Film", "2001", 1, "9")
for path in ("/api/v1/search?medium=movie&q=anything", "/api/v1/requests"):
    r = client.get(path, headers={"X-Emby-Token": "good-token"})
    check.equal(r.status_code, 503, f"{path} answers 503 through an outage")
    check.that(r.headers.get("content-type", "").startswith("application/json"),
               f"{path} answers it in JSON, not as a page")
media.forget()

# Header parsing: the handshake form clients actually send.
check.equal(
    jellyfin.token_from_header(
        'MediaBrowser Client="EchoFin", Device="iPhone", Token="abc123"'),
    "abc123", "token is picked out of a handshake header by name")
check.equal(jellyfin.token_from_header('MediaBrowser Token="a", Client="b"'),
            "a", "token is found when it comes first")
check.equal(jellyfin.token_from_header(None), "", "no header is no token")
check.equal(jellyfin.token_from_header("MediaBrowser Client=\"x\""), "",
            "a handshake with no token yields no token")


# --- the version number is a promise, not a counter -------------------------
# The shipped EchoFin client requires version == 1 exactly, so bumping this
# removes the whole feature from every phone that already has the app and
# cannot be undone by shipping a new one. Additive capability is announced by
# a named block in /capabilities, which an older client simply does not ask
# about. Do not "fix" this. The share gateway pins its own the same way.
check.equal(config.API_VERSION, 1, "API_VERSION is frozen at 1")

client = client_with(accepts)
caps = client.get("/api/v1/capabilities", headers={"X-Emby-Token": "good"}).json()
check.equal(caps["version"], 1, "capabilities reports version 1")

# The states a request can be in are part of the same promise. Clients decode
# them into a fixed enum, so a fourth state does not degrade one row -- it
# fails the decode of the whole response and takes the screen with it.
check.equal(caps["states"], ["on_its_way", "still_looking", "in_library"],
            "the three states are the three states")


# --- being found at all -----------------------------------------------------
# /info answers with no token so that "no such service" and "the service is
# broken" stop being the same answer. Everything else needs credentials, and a
# client probing the Jellyfin origin cannot tell a missing proxy rule from a
# server that simply does not run this.
found = client_with(rejects).get("/api/v1/info")
check.equal(found.status_code, 200, "/info answers without a token")
body = found.json()
check.equal(body["service"], "nextup", "/info names the service")
check.equal(body["protocol"], 1,
            "and the protocol a client that knows nothing else will read")
check.equal(body["protocols"], [1, 2],
            "with every shape it can answer in, which is additive")

# --- three prefixes, one app -------------------------------------------------
#
# A client's address is derived rather than typed: an explicit override lands
# on /api/v1, and a Companion or same-origin address on /nextup/api/v1. The
# audiobook protocol keeps its own prefix because every build in the field
# derives that one too, and none of them can be updated to stop.
probe = client_with(rejects, prefixes=True)
check.equal(probe.get("/nextup/api/v1/info").json()["service"], "nextup",
            "the derived nextup prefix answers as nextup")
check.equal(probe.get("/nextread/api/v1/info").json()["service"], "nextread",
            "and the audiobook prefix answers as nextread")
check.equal(probe.get("/nextread/api/v1/info").json()["protocol"], 1,
            "on the frozen protocol its clients were built against")

# The auth hazard holds under every prefix. The proxy header alone must never
# resolve anybody here: these routes are not behind the proxy, so the HTML
# resolver's fallback to JELLYFIN_USER would hand any caller the owner's list.
for path in ("/api/v1/capabilities", "/nextup/api/v1/capabilities",
             "/nextread/api/v1/capabilities"):
    header_only = probe.get(path, headers={config.AUTH_USER_HEADER: "matt"})
    check.equal(header_only.status_code, 401,
                f"a proxy header alone is refused at {path}")
check.that("user" not in found.json(),
           "/info says nothing about anybody, having asked for nothing")


# --- the token cache does not grow for ever ---------------------------------
# Expired rows were read past but never removed, so a service that had seen a
# few thousand rotated tokens held every one of them until it restarted.
api._tokens.clear()
# Relative to now, not a bare 0.0: time.monotonic() counts from boot, so on a
# freshly started machine zero is not yet expired and the check passes for the
# wrong reason. It failed exactly that way on CI.
import time  # noqa: E402

api._tokens["long-gone"] = (time.monotonic() - config.TOKEN_CACHE_SECONDS - 1,
                            jellyfin.User(id="u0", name="ghost"))
client_with(accepts).get("/api/v1/capabilities", headers={"X-Emby-Token": "fresh"})
check.that("long-gone" not in api._tokens,
           "an expired cache entry is dropped, not kept for ever")
check.equal(len(api._tokens), 1, "only the live entry remains")

harness.cleanup()
raise SystemExit(check.report())
