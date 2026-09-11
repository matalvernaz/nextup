"""One account's own daily allowance, where it differs from the shared one.

Caps were a setting and nothing else, so the only way to let one person ask
for more films was to let everybody. An override stored per account fixes that
without freezing the household: no row means the configured cap, so raising
the setting still raises it for everybody who has not been given a number.

The three readers are the point. The allowance arithmetic, the refusal message
and the figure published to clients each used to read the setting directly, and
a lowered cap that only some of them know about refuses a request and then
explains it in terms of a limit the person has not got.
"""
import harness

harness.setup()

from fastapi import FastAPI  # noqa: E402  (after harness.setup)
from fastapi.testclient import TestClient  # noqa: E402

from app import api, jellyfin, media, store, wants  # noqa: E402

check = harness.Check("account allowances")
store.init()

KEYHOLDER = jellyfin.User(id="admin-1", name="matt", is_admin=True)
MEMBER = jellyfin.User(id="member-1", name="alex", is_admin=False)
OTHER = jellyfin.User(id="member-2", name="sam", is_admin=False)

MOVIE = media.MOVIE
SHARED_CAP = 3


def serve(cap=SHARED_CAP):
    """This server offers films, at the shared cap of the moment."""
    media.available = lambda: {MOVIE: media.Medium(
        key=MOVIE, label="Films", units=("movie",), daily_cap=cap,
        library_ids=("lib",))}
    media.get = lambda medium: media.available().get(medium)


serve()
media.cost = lambda medium, unit: 1


def _account(reference):
    """Resolve a name or an id the way Jellyfin does, and raise as it does.

    A function and not a `next(..., default)`: the default is evaluated
    eagerly, so a throwing one raises on every call including the successful
    ones -- which reads as the endpoint refusing every account it is given.
    """
    for candidate in (KEYHOLDER, MEMBER, OTHER):
        if (reference or "").casefold() in {candidate.name, candidate.id}:
            return candidate
    raise LookupError(f"no Jellyfin account named {reference!r}")


jellyfin.account = _account

#: One token per account: the router caches an introspection result against a
#: digest of the token, so a shared string serves every later request as
#: whoever asked first.
TOKENS = {KEYHOLDER.key: "keyholder-token", MEMBER.key: "member-token",
          OTHER.key: "other-token"}


def call(user, method, path, body=None):
    jellyfin.user_from_token = lambda _token, who=user: who
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app, raise_server_exceptions=False)
    return getattr(client, method)(
        path, **({"json": body} if body is not None else {}),
        headers={"X-Emby-Token": TOKENS[user.key]})


print("=== the default is the setting, and stays the setting ===")
check.equal(wants.daily_cap(MEMBER, MOVIE), SHARED_CAP,
            "an account with no override follows the shared cap")
check.equal(wants.daily_cap(KEYHOLDER, MOVIE), None, "a keyholder is uncapped")
serve(cap=5)
check.equal(wants.daily_cap(MEMBER, MOVIE), 5,
            "and moves when the setting does")
serve()

print("\n=== an override applies to that account and no other ===")
store.set_cap(MEMBER.key, MOVIE, 10)
check.equal(wants.daily_cap(MEMBER, MOVIE), 10, "the account gets its number")
check.equal(wants.allowance(MEMBER, MOVIE), 10, "and the day's allowance with it")
check.equal(wants.daily_cap(OTHER, MOVIE), SHARED_CAP,
            "nobody else moved")
serve(cap=5)
check.equal(wants.daily_cap(MEMBER, MOVIE), 10,
            "raising the setting leaves an overridden account alone")
check.equal(wants.daily_cap(OTHER, MOVIE), 5,
            "and raises everybody who has not got one")
serve()

print("\n=== spending counts against the account's own number ===")
for index in range(4):
    store.record(MEMBER.key, MOVIE, f"tmdb:{index}", "movie", "A film", "", 1, "")
check.equal(wants.allowance(MEMBER, MOVIE), 6,
            "four spent out of ten leaves six")
check.equal(wants.allowance(OTHER, MOVIE), SHARED_CAP,
            "and says nothing about anybody else")

print("\n=== zero is a real allowance, and absent is the only default ===")
store.set_cap(OTHER.key, MOVIE, 0)
check.equal(wants.daily_cap(OTHER, MOVIE), 0, "zero is stored as zero")
check.equal(wants.allowance(OTHER, MOVIE), 0, "and allows nothing")
serve(cap=9)
check.equal(wants.daily_cap(OTHER, MOVIE), 0,
            "a raised setting does not overrule it")
serve()
refused = None
try:
    store.set_cap(OTHER.key, MOVIE, -1)
except ValueError as exc:
    refused = str(exc)
check.that(refused, "a negative cap is refused, not stored")
check.equal(store.cap_override(OTHER.key, MOVIE), 0, "leaving the old one")

print("\n=== clearing puts an account back on the shared cap ===")
check.that(store.clear_cap(OTHER.key, MOVIE), "clearing reports it had one")
check.equal(store.cap_override(OTHER.key, MOVIE), None, "no row remains")
check.equal(wants.daily_cap(OTHER, MOVIE), SHARED_CAP, "so the setting applies")
serve(cap=7)
check.equal(wants.daily_cap(OTHER, MOVIE), 7, "and moves with it again")
serve()
check.that(not store.clear_cap(OTHER.key, MOVIE), "clearing twice changes nothing")

print("\n=== the refusal explains the account's own limit ===")
store.set_cap(OTHER.key, MOVIE, 1)
store.record(OTHER.key, MOVIE, "tmdb:spent", "movie", "A film", "", 1, "")
denied = None
try:
    wants.want(OTHER, MOVIE, "tmdb:999", "movie", {"title": "Another"})
except wants.Denied as exc:
    denied = str(exc)
check.that(denied and "all the films for today" in denied,
             f"a spent account is told so: {denied!r}")

store.set_cap(OTHER.key, MOVIE, 0)
blocked = None
try:
    wants.want(OTHER, MOVIE, "tmdb:998", "movie", {"title": "Another"})
except wants.Denied as exc:
    blocked = str(exc)
check.that(blocked and "cannot ask for films" in blocked,
             f"an account allowed nothing is told that instead: {blocked!r}")
store.clear_cap(OTHER.key, MOVIE)

print("\n=== what a client is told matches what it will be refused on ===")
store.set_cap(MEMBER.key, MOVIE, 10)
caps = call(MEMBER, "get", "/api/v1/capabilities").json()
block = next(b for b in caps["media"] if b["medium"] == MOVIE)
check.equal(block["dailyCap"], 10,
            "capabilities publishes the account's own cap, not the setting")
check.equal(block["remainingToday"], wants.allowance(MEMBER, MOVIE),
            "and what is left agrees with it")
keyholder_caps = call(KEYHOLDER, "get", "/api/v1/capabilities").json()
keyholder_block = next(b for b in keyholder_caps["media"] if b["medium"] == MOVIE)
check.equal(keyholder_block["dailyCap"], None, "a keyholder is still uncapped")

print("\n=== setting one is a keyholder's job ===")
refused = call(MEMBER, "post", "/api/v1/allowance/cap",
               {"account": "sam", "medium": MOVIE, "dailyCap": 99})
check.equal(refused.status_code, 403, "a member may not set an allowance")
check.equal(store.cap_override(OTHER.key, MOVIE), None,
            "and the refused call stored nothing")

own = call(MEMBER, "post", "/api/v1/allowance/cap",
           {"account": "alex", "medium": MOVIE, "dailyCap": 99})
check.equal(own.status_code, 403, "not even for their own account")
check.equal(wants.daily_cap(MEMBER, MOVIE), 10, "which is the whole point")

unknown = call(KEYHOLDER, "post", "/api/v1/allowance/cap",
               {"account": "nobody", "dailyCap": 1})
check.equal(unknown.status_code, 404, "an account nobody has is refused")

unserved = call(KEYHOLDER, "post", "/api/v1/allowance/cap",
                {"account": "sam", "medium": "book", "dailyCap": 1})
check.equal(unserved.status_code, 404, "a medium this server has not got too")

negative = call(KEYHOLDER, "post", "/api/v1/allowance/cap",
                {"account": "sam", "medium": MOVIE, "dailyCap": -2})
check.equal(negative.status_code, 422, "and a negative number")
check.equal(store.cap_override(OTHER.key, MOVIE), None, "storing nothing")

print("\n=== and it takes effect on every surface at once ===")
set_it = call(KEYHOLDER, "post", "/api/v1/allowance/cap",
              {"account": "sam", "medium": MOVIE, "dailyCap": 2})
check.equal(set_it.status_code, 200, "a keyholder may set one")
check.equal(set_it.json()["allowances"][0]["dailyCap"], 2,
            "and is told what it now is")
check.equal(wants.daily_cap(OTHER, MOVIE), 2, "the arithmetic agrees")
sam_caps = call(OTHER, "get", "/api/v1/capabilities").json()
check.equal(next(b for b in sam_caps["media"] if b["medium"] == MOVIE)["dailyCap"],
            2, "and so does what that account's own client is told")

cleared = call(KEYHOLDER, "post", "/api/v1/allowance/cap",
               {"account": "sam", "medium": MOVIE, "dailyCap": None})
check.equal(cleared.status_code, 200, "null clears it")
check.equal(store.cap_override(OTHER.key, MOVIE), None, "removing the row")
check.equal(cleared.json()["allowances"][0]["dailyCap"], SHARED_CAP,
            "back on the shared cap, not on a copy of today's value")

print("\n=== reading one ===")
mine = call(MEMBER, "get", "/api/v1/allowance")
check.equal(mine.status_code, 200, "anybody may read their own")
check.equal(mine.json()["allowances"][0]["ownCap"], 10,
            "including whether it is their own number")
nosy = call(MEMBER, "get", "/api/v1/allowance?account=sam")
check.equal(nosy.status_code, 403, "and nobody else's")
theirs = call(KEYHOLDER, "get", "/api/v1/allowance?account=sam")
check.equal(theirs.status_code, 200, "a keyholder may read anybody's")
check.equal(theirs.json()["account"]["name"], "sam", "and gets the one asked for")

harness.cleanup()
raise SystemExit(check.report())
