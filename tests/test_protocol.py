"""What each protocol version is told, and what happens when one is invented.

Books are the whole of the difference between 1 and 2, and the reason they are
withheld from 1 is not a decoding worry -- a fourth medium decodes fine in the
shipped client. It is that a books library named for requests here, while the
audiobook prefix also names it for recommendations, makes that client draw two
rows for one feature from one server.
"""
import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="6",
    LISTENARR_URL="http://listenarr.invalid:4545",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, backends, jellyfin, media, store  # noqa: E402

check = harness.Check("protocol")
store.init()

USER = jellyfin.User(id="u1", name="matt", is_admin=False)
jellyfin.user_from_token = lambda token: USER

# Both backends answer and both libraries exist, so films and books are served.
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media.jellyfin.library_ids = lambda medium: {
    "movie": ["lib-films"], "book": ["lib-books"]}.get(medium, [])
media.forget()

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-Emby-Token": "a-real-looking-token"}


def media_of(response):
    return sorted(block["medium"] for block in response.json()["media"])


# --- protocol 1: exactly what shipped ---------------------------------------
one = client.get("/api/v1/capabilities", headers=AUTH)
check.equal(one.status_code, 200, "an unversioned request is answered")
check.equal(media_of(one), ["movie"],
            "and is told only about the media that shipped")

explicit = client.get("/api/v1/capabilities?protocol=1", headers=AUTH)
check.equal(media_of(explicit), ["movie"],
            "asking for 1 explicitly is the same answer")

# --- protocol 2: books --------------------------------------------------------
two = client.get("/api/v1/capabilities?protocol=2", headers=AUTH)
check.equal(two.status_code, 200, "protocol 2 is answered")
check.equal(media_of(two), ["book", "movie"],
            "and adds books, which is the whole of the difference")

book = next(b for b in two.json()["media"] if b["medium"] == "book")
check.equal(book["libraryIds"], ["lib-books"],
            "the books medium names the library it covers")
check.equal(sorted(book["units"]), ["book", "series"],
            "with both units: one book, or the rest of a series")

# --- an invented protocol is refused, not rounded down -----------------------
#
# Serving the nearest shape would make a client that is newer than its server,
# and a client with a typo, both look like a server that simply has fewer
# media -- which is the failure this whole codebase is arranged against.
future = client.get("/api/v1/capabilities?protocol=3", headers=AUTH)
check.equal(future.status_code, 400, "an unknown protocol is refused")
check.that("1, 2" in future.json()["detail"],
           "and the refusal says which ones there are")

# --- /info advertises the choice ---------------------------------------------
info = client.get("/api/v1/info").json()
check.equal(info["protocol"], 1,
            "the field a shipped client reads still says 1")
check.equal(info["protocols"], [1, 2],
            "and the new one lists both, for a client that knows to look")

harness.cleanup()
raise SystemExit(check.report())
