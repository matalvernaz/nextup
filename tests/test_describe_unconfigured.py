"""A server with no describarr offers nothing, and says so if asked anyway.

Its own file because `harness.setup` decides the environment once per process,
and the whole point here is the absence of a setting the other describe test
provides.
"""
import harness

harness.setup()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, describarr, jellyfin  # noqa: E402

check = harness.Check("describe unconfigured")

USER = jellyfin.User(id="u1", name="matt", is_admin=True)
jellyfin.user_from_token = lambda _token: USER

check.equal(describarr.configured(), False,
            "no DESCRIBARR_URL means not configured")

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)

caps = client.get("/api/v1/capabilities",
                  headers={"X-Emby-Token": "token"}).json()
# The contract the client relies on: it shows a control for what is supported
# and nothing at all for what is not. Present-and-false rather than absent, so
# a client can tell "this server does not do it" from "this server is too old
# to have been asked" -- the same distinction `media` draws by listing nothing.
check.equal(caps["describe"]["supported"], False,
            "and capabilities says so rather than leaving it out")

# Asked anyway -- an older client, or a server that lost the setting after the
# screen was drawn. Refused before Jellyfin is troubled for a path.
asked = []
jellyfin.item_with_path = lambda item_id: asked.append(item_id)
refused = client.post("/api/v1/describe", json={"itemId": "m1"},
                      headers={"X-Emby-Token": "token"})
check.equal(refused.status_code, 503, "the endpoint refuses outright")
check.that("no describarr" in refused.json()["detail"],
           "saying which part of the server is missing")
check.equal(asked, [], "and does not look the item up first")

harness.cleanup()
raise SystemExit(check.report())
