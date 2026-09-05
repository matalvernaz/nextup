"""Browser sign-in, on the same Jellyfin token the API already trusts.

Identity for the pages used to come only from a forward-auth proxy's header,
with a fallback to a configured username. That is right for this author's own
deployment and wrong for everybody else's: without oauth2-proxy in front, the
fallback is empty by design and every page rendered a "not signed in" dead
end. The distribution built around these services then exposed no pages at
all, so as published the whole package had no interface a person could reach
without the iOS client.

So the pages sign in against Jellyfin itself, and the token that comes back is
kept in a signed cookie. Identity then resolves through the same
``jellyfin.user_from_token`` path the JSON API uses, which means there is one
notion of who somebody is rather than two.

Three decisions worth stating, because each is easy to get wrong the other way:

* **The signing secret is generated on first boot** and kept in the database.
  Not an environment variable: a stranger should not have to mint a secret to
  sign in, and one they are made to invent will be ``changeme``.
* **Plain HTTP is allowed.** A first installation reached at
  ``http://server:8080`` over a home network is exactly the case this exists
  for. The cookie is marked ``Secure`` only when the request actually arrived
  over HTTPS, and the form says plainly when it did not.
* **One device id per installation**, so signing in does not mint a fresh
  Jellyfin device row every time somebody opens the page.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

from . import config, jellyfin, logs, store

log = logs.get("sessions")

COOKIE_NAME = "nextup_session"

#: How long a browser session lasts. Long enough not to be a nuisance on a
#: household's own network; short enough that a shared machine is not signed in
#: indefinitely. The Jellyfin token inside it may be revoked sooner, and is
#: re-introspected on a schedule of its own -- see `TOKEN_CACHE_SECONDS`.
SESSION_SECONDS = 30 * 24 * 3600

_SECRET_KEY = "session_secret"
_DEVICE_KEY = "device_id"


def _meta(key: str, make) -> str:
    """One durable value, generated once and read thereafter."""
    with store.db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
        if row:
            return row["value"]
        value = make()
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (key, value))
    log.info("generated %s", key)
    return value


def secret() -> bytes:
    return _meta(_SECRET_KEY, lambda: secrets.token_urlsafe(48)).encode()


def device_id() -> str:
    """This installation's Jellyfin device id, stable across restarts.

    Jellyfin files a session against the device that authenticated, so a fresh
    id per sign-in leaves a row per sign-in in its dashboard.
    """
    return _meta(_DEVICE_KEY, lambda: secrets.token_hex(16))


def _sign(payload: bytes) -> str:
    digest = hmac.new(secret(), payload, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(payload).decode().rstrip("=") + "."
            + base64.urlsafe_b64encode(digest).decode().rstrip("="))


def _unpad(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue(token: str, user_id: str) -> str:
    """A signed cookie value carrying one Jellyfin access token."""
    payload = json.dumps({"t": token, "u": user_id,
                          "exp": int(time.time()) + SESSION_SECONDS},
                         separators=(",", ":")).encode()
    return _sign(payload)


def read(cookie: str | None) -> str | None:
    """The Jellyfin access token inside a cookie, or None.

    None for anything wrong at all: no cookie, a tampered signature, an
    expired session, or a value that will not parse. There is deliberately no
    distinction -- every one of them means "sign in again", and telling them
    apart would only tell an attacker which part they got right.
    """
    if not cookie or "." not in cookie:
        return None
    body, _, signature = cookie.rpartition(".")
    try:
        payload = _unpad(body)
        expected = _unpad(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(
            hmac.new(secret(), payload, hashlib.sha256).digest(), expected):
        log.warning("a session cookie did not verify")
        return None
    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if int(claims.get("exp") or 0) < time.time():
        return None
    token = claims.get("t")
    return token if isinstance(token, str) and token else None


def sign_in(username: str, password: str) -> tuple[str, jellyfin.User]:
    """Authenticate against Jellyfin. Returns (cookie value, user).

    Raises `jellyfin.TokenRejected` for a refusal and
    `jellyfin.JellyfinUnavailable` when Jellyfin could not be asked, so a
    wrong password and a server that is down do not read the same on screen.
    """
    token, user = jellyfin.authenticate(username, password, device_id())
    log.info("signed in user=%s", user.key)
    return issue(token, user.id), user


def is_secure(forwarded_proto: str | None, scheme: str) -> bool:
    """Whether this request actually arrived over HTTPS.

    A proxy's header wins where it is set, because behind Traefik the request
    reaching this process is plain HTTP however it started. Nothing is assumed
    from its absence: an install reached directly over HTTP is a supported one,
    and marking a cookie `Secure` there would stop sign-in working at all.
    """
    if forwarded_proto:
        return forwarded_proto.split(",")[0].strip().lower() == "https"
    return scheme == "https"
