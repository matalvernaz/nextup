"""A listener's pull to refresh has to be able to change the shelf.

The shelf is cached for an hour, and until now nothing could ask past that
cache. EchoFin's Recommended list re-hydrated the same frozen set of item ids
on every pull, announced its count, and reported success -- so a book finished
a minute earlier went on being recommended and the gesture looked like it had
worked. Proven from the Traefik log on 2026-09-12: one `/shelves` read at
08:37:41 and five `/Items?ids=` re-reads after it, the last at 08:40:22.

The other half is that a forced rebuild costs twelve seconds, and Matt pulled
three times in fifteen. Three rebuilds for one answer is the fan-out that
`test_upkeep_serialises` exists to prevent, arriving by a different door.
"""
import threading
import time

import harness

harness.setup(JELLYFIN_USER="")

from app import jellyfin, store  # noqa: E402
from app.books import engine, shelves  # noqa: E402

check = harness.Check("shelf refresh on request")
store.init()

matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)

#: Bumped per build so each answer is distinguishable, which is how "served the
#: cache" and "rebuilt" are told apart without timing anything.
builds: list[str] = []
gate = threading.Event()
gate.set()


def fake_run(user, update_playlist=True):
    gate.wait()
    builds.append(user.key)
    return {
        "own": [{"id": f"item-{len(builds)}", "title": "A Book", "why": []}],
        "discover": [],
        "owned_index": ({"B0OWNED"}, {"a book": {"someone"}}),
        "playlist_name": "Next Read",
        "playlist_id": None,
        "seeds": 1, "library": 1, "ratings": 0,
    }


engine.run = fake_run
shelves.write_playlist = lambda user, data: None

first = shelves.result(matt, update_playlist=False)
check.equal(len(builds), 1, "the first read builds the shelf")

cached = shelves.result(matt, update_playlist=False)
check.equal(len(builds), 1, "and the second is served from the hour cache")
check.equal(cached["own"][0]["id"], first["own"][0]["id"],
            "which is the same shelf, down to the ids the client hydrates")

forced = shelves.result(matt, force=True, update_playlist=False)
check.equal(len(builds), 2, "a forced read rebuilds through a fresh cache")
check.that(forced["own"][0]["id"] != first["own"][0]["id"],
           "and answers with the new shelf, not the one it replaced")

# Three pulls in fifteen seconds is what the log shows. Held shut so all three
# are genuinely in flight together, which is the only state that can fan out.
gate.clear()
before = len(builds)
answers: list[dict] = []
answer_guard = threading.Lock()


def pull() -> None:
    got = shelves.result(matt, force=True, update_playlist=False)
    with answer_guard:
        answers.append(got)


pulls = [threading.Thread(target=pull) for _ in range(3)]
for thread in pulls:
    thread.start()
# Long enough for all three to be waiting on the lock or the gate rather than
# still being started, so the coalescing is what is measured.
time.sleep(0.2)
gate.set()
for thread in pulls:
    thread.join(timeout=10)

check.equal(len(builds) - before, 1,
            "three pulls at once cost one rebuild, not three: nobody who "
            "arrived before it started could have wanted anything fresher")
check.equal(len({answer["own"][0]["id"] for answer in answers}), 1,
            "and all three listeners are told the same thing")

# A pull that arrives after a rebuild has finished is a new question and gets
# its own answer -- otherwise the second gesture of a session would be a no-op.
after = len(builds)
shelves.result(matt, force=True, update_playlist=False)
check.equal(len(builds) - after, 1, "a later pull still rebuilds")


# The route has to actually carry the gesture through. A `force` the handler
# accepts and drops would pass every test above and change nothing on a phone.
from app import compat_nextread  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

shelves.owned_index = lambda user: ({"B0OWNED"}, {"a book": {"someone"}})
book_wants.states = lambda key, index=None: []

asked: list[bool] = []
real_result = shelves.result


def recording_result(user, force=False, update_playlist=True):
    asked.append(force)
    return real_result(user, force=force, update_playlist=update_playlist)


shelves.result = recording_result

compat_nextread.get_shelves(user=matt)
check.equal(asked[-1], False,
            "a read with no force asked for is the cached one every installed "
            "build already gets")

compat_nextread.get_shelves(force=True, user=matt)
check.equal(asked[-1], True, "and the gesture's read reaches the rebuild")

shelves.result = real_result

harness.cleanup()
raise SystemExit(check.report())
