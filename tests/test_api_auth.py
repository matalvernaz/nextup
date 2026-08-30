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

from app import api, jellyfin  # noqa: E402

check = harness.Check("api auth")


def client_with(monkeypatched):
    """A test client whose Jellyfin introspection is `monkeypatched`."""
    from fastapi import FastAPI
    jellyfin.user_from_token = monkeypatched
    app = FastAPI()
    app.include_router(api.router)
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

harness.cleanup()
raise SystemExit(check.report())
