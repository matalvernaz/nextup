"""Signing in without a proxy, which is what a stranger's install has.

Before this the pages resolved identity from a forward-auth header alone, with
a fallback that is empty by design -- so an installation without oauth2-proxy
rendered a dead end, and the distribution around these services exposed no
pages at all. There was no interface a person could reach.
"""
import harness

harness.setup()

from fastapi.testclient import TestClient  # noqa: E402

from app import config, jellyfin, main, sessions, store  # noqa: E402

check = harness.Check("sign-in")
store.init()

USER = jellyfin.User(id="u1", name="matt", is_admin=True)
OTHER = jellyfin.User(id="u2", name="kadija", is_admin=False)

client = TestClient(main.app, raise_server_exceptions=False)


def accepts(username, password, device):
    if username == "matt" and password == "right":
        return "token-for-matt", USER
    raise jellyfin.TokenRejected("Jellyfin did not accept that.")


jellyfin.authenticate = accepts
jellyfin.user_from_token = lambda token: (
    USER if token == "token-for-matt" else
    OTHER if token == "token-for-kadija" else
    (_ for _ in ()).throw(jellyfin.TokenRejected("no")))
main.media.available = lambda: {}
main.wants.states = lambda user, medium=None: []

# --- with nothing at all, the page asks rather than refusing -----------------
anonymous = client.get("/", follow_redirects=False)
check.equal(anonymous.status_code, 401, "an unknown visitor is not served")
check.that("Jellyfin username" in anonymous.text,
           "and is given a form, not a dead end")
check.that("password" in anonymous.text.lower(),
           "which asks for a password")

# --- a wrong password says so, and is not a server error ---------------------
refused = client.post("/signin", data={"username": "matt", "password": "no"},
                      follow_redirects=False)
check.equal(refused.status_code, 401, "a wrong password is refused")
check.that("did not accept" in refused.text,
           "with Jellyfin's own answer, not a generic failure")
check.that(sessions.COOKIE_NAME not in refused.cookies,
           "and nothing is issued")

# --- the right one signs in --------------------------------------------------
ok = client.post("/signin", data={"username": "matt", "password": "right"},
                 follow_redirects=False)
check.equal(ok.status_code, 303, "a correct password redirects")
check.equal(ok.headers.get("location"), "/", "to the page that was wanted")
cookie = ok.cookies.get(sessions.COOKIE_NAME)
check.that(bool(cookie), "and issues a session")
check.that("token-for-matt" not in (cookie or ""),
           "which does not carry the Jellyfin token in the clear")

signed_in = client.get("/")
check.equal(signed_in.status_code, 200, "the page now renders")
check.that("Signed in as matt" in signed_in.text, "as the right person")
check.that("Sign out" in signed_in.text, "with a way back out")

# --- the cookie is the identity, and it is signed ----------------------------
tampered = TestClient(main.app, raise_server_exceptions=False)
tampered.cookies.set(sessions.COOKIE_NAME, (cookie or "")[:-3] + "aaa")
check.equal(tampered.get("/", follow_redirects=False).status_code, 401,
            "an altered cookie is nobody")

forged = TestClient(main.app, raise_server_exceptions=False)
forged.cookies.set(sessions.COOKIE_NAME, sessions.issue("token-for-kadija", "u2"))
as_kadija = forged.get("/")
check.that("Signed in as kadija" in as_kadija.text,
           "a validly signed cookie resolves its own account, not the first one")

# --- a revoked token signs out rather than falling back ----------------------
#
# The fallback would otherwise show the configured account's list to whoever's
# session had just expired.
config.JELLYFIN_USER = "matt"
jellyfin.user = lambda name=None: USER
revoked = TestClient(main.app, raise_server_exceptions=False)
revoked.cookies.set(sessions.COOKIE_NAME, sessions.issue("token-revoked", "u9"))
expired = revoked.get("/", follow_redirects=False)
check.equal(expired.status_code, 401,
            "a session whose token Jellyfin has revoked is signed out")
check.that("expired" in expired.text.lower(),
           "and told why, rather than silently becoming somebody else")
config.JELLYFIN_USER = ""

# --- signing out ---------------------------------------------------------------
out = client.post("/signout", follow_redirects=False)
check.equal(out.status_code, 303, "signing out redirects")
check.equal(out.headers.get("location"), "/signin", "to the form")

# --- the cookie is marked Secure only when the connection really is ----------
check.equal(sessions.is_secure("https", "http"), True,
            "a proxy saying https is believed, because behind one the request "
            "reaching this process is plain http however it started")
check.equal(sessions.is_secure("http", "http"), False,
            "and a proxy saying http is believed too")
check.equal(sessions.is_secure(None, "https"), True,
            "with no proxy, the scheme decides")
check.equal(sessions.is_secure(None, "http"), False,
            "and a direct plain-http install is supported, not refused: that "
            "is what a first install on a home network looks like")

harness.cleanup()
raise SystemExit(check.report())
