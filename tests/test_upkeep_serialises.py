"""The scheduled shelf pass does its work before it says it did.

Ten accounts got shelves on 2026-09-06 and the next upkeep pass started ten
reads of the whole book library at once. Every one of them timed out at the
client's thirty seconds, so `once()` reported refreshing ten accounts having
rebuilt none -- and it did that silently, because the playlists were still
being written from the stored copies and a stale playlist looks exactly like a
current one. That is the failure the whole module exists to prevent.
"""
import threading
import time

import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid:4545", JELLYFIN_USER="")

from app import jellyfin, store  # noqa: E402
from app.books import engine, shelves, upkeep  # noqa: E402
from app.books import store as book_store  # noqa: E402

check = harness.Check("upkeep serialises")
store.init()

ACCOUNTS = {f"person{n}": f"user-{n}" for n in range(1, 11)}
jellyfin.all_users = lambda: dict(ACCOUNTS)
jellyfin.user = lambda name=None: jellyfin.User(
    id=ACCOUNTS[name], name=name, is_admin=False)
jellyfin.set_playlist = lambda uid, name, ids: "playlist-1"

SHELF = {
    "own": [{"id": "i1", "title": "A Book", "authors": ["Someone"],
             "why": ["because"]}],
    "discover": [],
    "owned_index": ({"B0OWNED"}, {"a book": {"someone"}}),
    "playlist_name": "Next Read", "playlist_id": None,
    "seeds": 1, "library": 1, "ratings": 0,
}

# Every account has a shelf on disk, which is the state that produced the
# fan-out: `result` finds a stored copy, calls it stale, and hands the rebuild
# to a thread rather than doing it.
for uid in ACCOUNTS.values():
    book_store.put_shelf(uid, {k: v for k, v in SHELF.items()
                               if k != "owned_index"})
shelves.invalidate()
for uid in ACCOUNTS.values():
    book_store.put_shelf(uid, {k: v for k, v in SHELF.items()
                               if k != "owned_index"})

concurrent = 0
peak = 0
started: list[str] = []
#: Appended on the way *out*, which is the whole point: a pass that has
#: started ten builds has not refreshed ten accounts.
finished: list[str] = []
guard = threading.Lock()


def counted_run(user, update_playlist=True):
    """Stands in for the build, and records how many run at once.

    The real one is a read of the whole book library -- 3,550 items with
    `People` on this deployment, nine of its twelve seconds -- which is why
    how many happen together is the thing worth measuring.
    """
    global concurrent, peak
    with guard:
        concurrent += 1
        peak = max(peak, concurrent)
        started.append(user.key)
    try:
        time.sleep(0.1)
        return {k: v for k, v in SHELF.items()}
    finally:
        with guard:
            concurrent -= 1
            finished.append(user.key)


engine.run = counted_run

refreshed = upkeep.once()

check.equal(refreshed, len(ACCOUNTS),
            "the pass reports every account it was given")
check.equal(len(finished), len(ACCOUNTS),
            "and every one of them had really been rebuilt by the time it "
            "said so, rather than handed to a thread still reading")
check.equal(sorted(set(started)), sorted(ACCOUNTS.values()),
            "each account exactly once, none of them twice")
check.equal(peak, 1,
            "one read of the book library at a time: ten at once is what "
            "timed out on the live server, all ten of them, every pass")

# Nothing left running behind it either. A pass that returns while threads are
# still reading the library is the same fault wearing a different number.
time.sleep(0.2)
check.equal(concurrent, 0, "and nothing is still reading when it returns")

harness.cleanup()
raise SystemExit(check.report())
