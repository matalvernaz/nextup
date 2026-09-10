"""Who may give an account its requests back, and what happens when they do.

The endpoint hands one account a fresh day, so the check that matters is the
one on the caller: a member who could reset their own cap does not have a cap.
"""
import harness

harness.setup()

from fastapi import FastAPI  # noqa: E402  (after harness.setup)
from fastapi.testclient import TestClient  # noqa: E402

from app import api, jellyfin, media, store, wants  # noqa: E402

check = harness.Check("allowance reset api")
store.init()

KEYHOLDER = jellyfin.User(id="admin-1", name="matt", is_admin=True)
MEMBER = jellyfin.User(id="member-1", name="alex", is_admin=False)

# One medium, so the test says nothing about which backends this host has.
BOOK = media.BOOK
media.available = lambda: {BOOK: media.Medium(
    key=BOOK, label="Books", units=("book",), daily_cap=3, library_ids=("lib",))}
media.get = lambda medium: media.available().get(medium)
jellyfin.account = lambda reference: (
    MEMBER if reference.casefold() in {"alex", "member-1"} else
    KEYHOLDER if reference.casefold() in {"matt", "admin-1"} else
    (_ for _ in ()).throw(LookupError(f"no Jellyfin account named {reference!r}")))


#: One token per account, because the router caches an introspection result
#: against a digest of the token. Sharing one string between the two callers
#: would serve every later request as whoever asked first -- which reads as
#: the endpoint refusing an administrator.
TOKENS = {KEYHOLDER.key: "keyholder-token", MEMBER.key: "member-token"}


def post(user, body):
    jellyfin.user_from_token = lambda _token, who=user: who
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app, raise_server_exceptions=False)
    return client.post("/api/v1/allowance/reset", json=body,
                       headers={"X-Emby-Token": TOKENS[user.key]})


# The member has spent the day.
for index in range(3):
    store.record(MEMBER.key, BOOK, f"asin{index}", "book", "A book", "", 1, "")
check.equal(wants.allowance(MEMBER, BOOK), 0, "the member starts with none left")

# The hazard: a capped account resetting its own cap.
denied = post(MEMBER, {"account": "alex", "medium": BOOK})
check.equal(denied.status_code, 403, "a member may not reset an allowance")
check.equal(wants.allowance(MEMBER, BOOK), 0,
            "and the refused call spent nothing")

unknown = post(KEYHOLDER, {"account": "nobody", "medium": BOOK})
check.equal(unknown.status_code, 404, "an account nobody has is refused")

unserved = post(KEYHOLDER, {"account": "alex", "medium": "puppets"})
check.equal(unserved.status_code, 404, "so is a medium this server does not serve")
check.equal(wants.allowance(MEMBER, BOOK), 0,
            "and neither refusal reset anything")

done = post(KEYHOLDER, {"account": "alex", "medium": BOOK})
check.equal(done.status_code, 200, "a keyholder may reset it")
body = done.json()
check.equal(body["account"]["name"], "alex", "the answer names the account reset")
check.equal(body["reset"], [{"medium": BOOK, "remainingToday": 3}],
            "and reports what it has left")
check.equal(wants.allowance(MEMBER, BOOK), 3, "which is what the cap now says")

# Named by account id rather than display name, which is what a client holds.
store.record(MEMBER.key, BOOK, "asin-later", "book", "A book", "", 1, "")
check.equal(wants.allowance(MEMBER, BOOK), 2, "a later request spends again")
check.equal(post(KEYHOLDER, {"account": "member-1", "medium": BOOK}).status_code,
            200, "an account id names the same account")
check.equal(wants.allowance(MEMBER, BOOK), 3, "and resets it")

# No medium means every medium, so nobody has to know which ones exist.
store.record(MEMBER.key, BOOK, "asin-last", "book", "A book", "", 1, "")
every = post(KEYHOLDER, {"account": "alex"})
check.equal(every.status_code, 200, "naming no medium is allowed")
check.equal([r["medium"] for r in every.json()["reset"]], [BOOK],
            "and covers every medium this server serves")
check.equal(wants.allowance(MEMBER, BOOK), 3, "clearing the day on each")

# An administrator was never capped, and a reset must not invent a number.
uncapped = post(KEYHOLDER, {"account": "matt", "medium": BOOK})
check.equal(uncapped.json()["reset"][0]["remainingToday"], None,
            "resetting a keyholder still reports no cap")

harness.cleanup()
raise SystemExit(check.report())
