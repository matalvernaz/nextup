"""Starting up before Jellyfin does, which is the ordinary one-box ordering.

`JELLYFIN_URL` here points at a host that does not resolve, so every call to
Jellyfin fails the way it fails on a real first `docker compose up` where the
two containers came up in the wrong order.
"""
import harness

harness.setup()

import httpx  # noqa: E402

from app import jellyfin, main, store  # noqa: E402

check = harness.Check("first boot")
store.init()

# A fresh ledger. The migration onto account ids has nothing to move, so it
# must not ask Jellyfin anything -- it used to, and raised, and the raise
# happens inside lifespan where it kills the process rather than failing a
# health check. `restart: unless-stopped` then makes it a loop.
check.equal(store.ledger_is_empty(), True, "a fresh install has no requests")
try:
    main._rekey_ledger_once()
    check.that(True, "first boot does not need Jellyfin")
except Exception as exc:
    check.that(False, f"first boot raised {type(exc).__name__}: {exc}")
check.equal(store.user_key_scheme(), "id",
            "and it adopts the account-id scheme outright")

# The second run is a no-op whatever Jellyfin is doing.
main._rekey_ledger_once()
check.equal(store.user_key_scheme(), "id", "a second run changes nothing")

# The fatal path is still fatal, and that is deliberate: rows keyed on display
# names served through id-keyed lookups read as no requests at all and no
# allowance spent, which invites a second request for something already on its
# way. Better to refuse to start and say so.
store.set_user_key_scheme("name")
store.record("matt", "book", "B01", "book", "A Novel", "", 1, "")
check.equal(store.ledger_is_empty(), False, "there is now something to migrate")
check.raises(RuntimeError, main._rekey_ledger_once,
             "a ledger that needs migrating still refuses to start blind")

# And the reason it refused is Jellyfin, not the store. `all_users` lets the
# transport error out raw -- its one caller turns any failure into the refusal
# above, and wrapping it would only restate that.
check.raises(httpx.HTTPError, jellyfin.all_users,
             "because Jellyfin cannot be reached at all")

# --- and the pages read like an outage, not like a bug ----------------------
#
# Every page resolves its viewer through Jellyfin. The five routes that do
# caught only "nobody resolved", so a Jellyfin that was merely down raised
# through them and became a 500 with a traceback -- on the same container that
# deliberately keeps reporting healthy through exactly that outage.
from fastapi.testclient import TestClient  # noqa: E402

from app import sessions  # noqa: E402

pages = TestClient(main.app, raise_server_exceptions=False)
signed_in = pages.post("/signin", data={"username": "matt", "password": "x"})
check.equal(signed_in.status_code, 503,
            "signing in against an absent Jellyfin says so")

# A cookie that verifies, carrying a token nothing can be asked about.
store.set_user_key_scheme("id")
pages.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", "u1"))
landing = pages.get("/")
check.equal(landing.status_code, 503,
            "and so does the ordinary page, rather than raising")
check.that("Jellyfin cannot be reached" in landing.text,
           "in words, on a page, with nothing lost")

harness.cleanup()
raise SystemExit(check.report())
