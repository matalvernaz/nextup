"""Who writes the Jellyfin playlist, now that nothing loads a shelf page.

In the audiobook service that write was owed to a human: loading its shelf
page recomputed the shelves and rewrote the playlist. The merge brought the
shelves across but not that page, and the one remaining reader --
`/nextread/api/v1/shelves`, which is what the client polls -- was deliberately
side-effect-free. So the playlist would have stopped being written on the day
of the cutover, with no error anywhere: clients that reach the shelf through
Jellyfin rather than through this API would have gone on showing whatever list
the old service happened to leave behind.
"""
import harness

# JELLYFIN_USER empty is what a household deployment runs: a browser request
# with no proxy header must resolve to nobody rather than to the owner.
harness.setup(JELLYFIN_USER="", PLAYLIST_OWNER="matt")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import compat_nextread, jellyfin, store  # noqa: E402
from app.books import engine, shelves  # noqa: E402
from app.books import store as book_store  # noqa: E402

check = harness.Check("playlist upkeep")
store.init()
book_store.init()

USER = jellyfin.User(id="user-matt", name="matt", is_admin=False)
jellyfin.user_from_token = lambda token: USER

written: list[list[str]] = []


def fake_set_playlist(user_id, name, item_ids):
    written.append(list(item_ids))
    return "playlist-1"


def fake_run(user, update_playlist=True):
    # Through the module attribute, not the function object: the real engine
    # calls `jellyfin.set_playlist`, and a fake that closes over its own
    # replacement cannot be intercepted by patching that attribute -- which is
    # exactly what the refusal check below does.
    if update_playlist:
        try:
            jellyfin.set_playlist(user.id, "Next Read", ["i1", "i2"])
        except Exception as exc:  # noqa: BLE001 - as the real engine does
            print(f"  (playlist write refused: {exc})")
    return {
        "own": [{"id": "i1", "title": "One", "why": []},
                {"id": "i2", "title": "Two", "why": []}],
        "discover": [{"asin": "A1", "title": "Suggested"}],
        "owned_index": ({"B0OWNED"}, {"one": {"someone"}}),
        "playlist_name": "Next Read",
        "playlist_id": "playlist-1",
        "seeds": 1, "library": 2, "ratings": 0,
    }


engine.run = fake_run
jellyfin.set_playlist = fake_set_playlist

app = FastAPI()
app.include_router(compat_nextread.router)
client = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-Emby-Token": "a-real-looking-token"}

first = client.get("/nextread/api/v1/shelves", headers=AUTH)
check.equal(first.status_code, 200, "the shelves read is answered")
check.equal(written, [["i1", "i2"]],
            "and it settles the playlist, in the shelf's own order")

# Once per recomputation, not once per request. A cached shelf carries whether
# its write is still outstanding, so polling clients cost nothing.
for _ in range(3):
    client.get("/nextread/api/v1/shelves", headers=AUTH)
check.equal(len(written), 1, "a cached read does not rewrite the playlist")

# A recomputation owes it again.
shelves.invalidate(USER.key)
client.get("/nextread/api/v1/shelves", headers=AUTH)
check.equal(len(written), 2, "a rebuilt shelf writes the playlist again")

# --- a playlist Jellyfin refuses must not cost anybody their shelf -----------
#
# This write only reached a browser page before. On the API path a raise is a
# 500 where a 200 was owed, and the books row disappears for that account.
def refuse(user_id, name, item_ids):
    raise RuntimeError("Access is not allowed for this item")


jellyfin.set_playlist = refuse
shelves.invalidate(USER.key)
refused = client.get("/nextread/api/v1/shelves", headers=AUTH)
check.equal(refused.status_code, 200,
            "a refused playlist write still serves the shelf")
check.equal(len(refused.json()["owned"]), 2, "with its contents intact")
jellyfin.set_playlist = fake_set_playlist

# --- whose playlist keeps the bare name --------------------------------------
#
# It used to be whoever JELLYFIN_USER named, which is empty here and in every
# household deployment, so the account that has been writing "Next Read" for
# months would have quietly started writing "Next Read — matt" instead --
# leaving every client subscribed to a playlist nothing updates any more.
check.equal(engine._playlist_name(USER), "Next Read",
            "the owning account keeps the name its playlist already has")
check.equal(engine._playlist_name(jellyfin.User(id="u2", name="kadija")),
            "Next Read — kadija",
            "and everybody else gets their own, because ten accounts sharing "
            "one playlist is ten accounts overwriting each other")

harness.cleanup()
raise SystemExit(check.report())
