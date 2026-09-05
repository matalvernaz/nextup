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

harness.cleanup()
raise SystemExit(check.report())
