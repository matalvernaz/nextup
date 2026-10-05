"""Importing as the list is looked up: turns between lists, and asking early.

Exact matches are asked for while a list is still being looked up, so what is
pinned here is the part a person can see while that happens: a short list
started behind a long one is not held for the long one's whole run, a near
miss can be ticked and asked for before the lookup is over, and pressing the
button twice asks once.
"""
import threading
import time

import harness

harness.setup(
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_ALBUM_COST="1", MUSIC_ARTIST_COST="3",
    MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="",
)

from app import arr, buskarr, imports, jellyfin, media, store  # noqa: E402

check = harness.Check("import automatic")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="user-kid", name="kid", is_admin=False)
media.owned = lambda *args, **kwargs: jellyfin.Owned()
media._registry = {
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                              ("lib-music",)),
}
media._registry_built_at = time.monotonic()
media._registry_settled = True

#: Seconds each lookup takes, so a long list is long enough to watch.
lookup_seconds = [0.0]


def fake_search(q, unit, limit, sources=()):
    time.sleep(lookup_seconds[0])
    artist, _, title = q.partition(" | ")
    if title.startswith("Near "):
        # Same title, another credit: a near miss.
        return [buskarr._result({"title": title[5:], "artist": "Someone Else",
                                 "ref": f"n-{title}", "source": "deezer"}, unit)]
    return [buskarr._result({"title": title, "artist": artist,
                             "ref": f"r-{title}", "source": "deezer"}, unit)]


buskarr.search = fake_search
# The query is "artist title"; a separator the fake can split on is cleaner
# than guessing where the credit ends.
imports._query = lambda row, medium, unit: f"{row.artist} | {row.title}"

asked: list[str] = []
add_seconds = [0.0]


def fake_add(unit, hit, by, bulk=False):
    time.sleep(add_seconds[0])
    asked.append(hit.get("title", ""))
    return arr.AddResult(True, "Sent to buskarr.", f"job:{hit.get('ref')}",
                         hit.get("title", ""))


buskarr.add = fake_add


def until(user, import_id, state, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        batch = imports.get(user, import_id)
        if batch["state"] == state:
            return batch
        time.sleep(0.01)
    return imports.get(user, import_id)


def listing(prefix, count, near=()):
    return "Artist,Album\n" + "".join(
        f"Band,{'Near ' if n in near else ''}{prefix} {n}\n"
        for n in range(count))


# --- a short list started behind a long one ----------------------------------
lookup_seconds[0] = 0.02
long_id = imports.start(MATT, media.MUSIC, "album", "long.csv",
                        listing("Long", 60))
time.sleep(0.1)
short_id = imports.start(KID, media.MUSIC, "album", "short.csv",
                         listing("Short", 2))
short = until(KID, short_id, imports.DONE)
long_now = imports.get(MATT, long_id)
check.equal(short["state"], imports.DONE, "the short list finishes")
check.that(long_now["state"] == imports.READING and long_now["done"] < 60,
           f"while the long one started before it is still being looked up "
           f"({long_now['done']} of 60): the two take turns a row at a time")
until(MATT, long_id, imports.DONE)

# --- a near miss asked for before the lookup is over --------------------------
lookup_seconds[0] = 0.02
asked.clear()
import_id = imports.start(MATT, media.MUSIC, "album", "early.csv",
                          listing("Early", 40, near={0}))
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    batch = imports.get(MATT, import_id)
    if any(row["state"] == imports.UNCERTAIN for row in batch["rows"]):
        break
    time.sleep(0.01)
near_line = next(row["line"] for row in batch["rows"]
                 if row["state"] == imports.UNCERTAIN)
imports.ask_for_lines(MATT, import_id, {near_line})
deadline = time.monotonic() + 10
while "Early 0" not in asked and time.monotonic() < deadline:
    time.sleep(0.01)
during = imports.get(MATT, import_id)
check.that("Early 0" in asked,
           "a near miss ticked while the list is still being looked up is "
           "asked for then, not at the end")
check.equal(during["state"], imports.READING,
            "and the lookup carries on underneath it")
done = until(MATT, import_id, imports.DONE)
check.equal(done["state"], imports.DONE,
            "with nothing left to decide once it is all looked up, the list ends")

# --- pressing the button twice ---------------------------------------------
lookup_seconds[0] = 0.0
add_seconds[0] = 0.3
asked.clear()
import_id = imports.start(MATT, media.MUSIC, "album", "twice.csv",
                          listing("Twice", 1, near={0}))
batch = until(MATT, import_id, imports.READY)
line = batch["rows"][0]["line"]
asked.clear()
presses = [threading.Thread(target=imports.ask_for_lines,
                            args=(MATT, import_id, {line})) for _ in range(2)]
for press in presses:
    press.start()
for press in presses:
    press.join()
until(MATT, import_id, imports.DONE)
# Every asking thread finished before anything is read: the list can be done
# while a second ask is still on its way to overwriting the first.
for thread in threading.enumerate():
    if thread.name.startswith("import-ask-"):
        thread.join()
finished = imports.get(MATT, import_id)
check.equal(asked, ["Twice 0"], "two presses together ask for the row once")
check.equal(finished["rows"][0]["outcome"], imports.ASKED,
            "and the row says it was asked for, not that a second ask found "
            "it already asked for")
add_seconds[0] = 0.0

# --- a list being worked through is never pruned ------------------------------
with store.db() as conn:
    conn.execute("UPDATE imports SET state='reading', created_at=0 "
                 "WHERE import_id=?", (import_id,))
store.prune_imports(time.time())
check.that(store.get_import(import_id) is not None,
           "however old it is, a list still reading is kept")

# --- stopping a list -------------------------------------------------------
lookup_seconds[0] = 0.02
asked.clear()
import_id = imports.start(MATT, media.MUSIC, "album", "stop.csv",
                          listing("Stop", 80))
time.sleep(0.3)
check.equal(imports.stop_list(MATT, import_id), True, "a list being looked up can be stopped")
stopped = until(MATT, import_id, imports.DONE)
seen = len(asked)
time.sleep(0.2)
check.that(stopped["state"] == imports.DONE and stopped["done"] < 80
           and stopped.get("stopped"),
           f"it ends where it was ({stopped['done']} of 80) and says it was stopped")
check.equal(len(asked), seen, "and nothing more is asked for after")
with store.db() as conn:
    conn.execute("UPDATE imports SET state='reading', touched_at=0 "
                 "WHERE import_id=?", (import_id,))
jellyfin.all_users = lambda: {"matt": MATT.id, "kid": KID.id}
jellyfin.user = lambda name: {"matt": MATT, "kid": KID}[name]
check.equal(imports.resume_interrupted(), 0,
            "a stopped list is not carried on after a restart")
check.equal(imports.get(MATT, import_id)["state"], imports.DONE,
            "it is settled instead")

# Stopping the queue stops a music list still filling it, or the next row
# would only refill it.
from app import config  # noqa: E402
config.IMPORT_MUSIC_DAILY_SONGS = 1
asked.clear()
import_id = imports.start(KID, media.MUSIC, "track", "fills.csv",
                          "Artist,Track\n" + "".join(f"Band,Fill {n}\n"
                                                     for n in range(80)))
deadline = time.monotonic() + 10
while store.queued_count(KID.key) < 3 and time.monotonic() < deadline:
    time.sleep(0.01)
dropped, lists_stopped = imports.clear_queue(KID)
check.equal(lists_stopped, 1, "stopping the queue stops the list feeding it")
until(KID, import_id, imports.DONE)
time.sleep(0.2)
check.equal(store.queued_count(KID.key), 0, "so the queue stays empty")
config.IMPORT_MUSIC_DAILY_SONGS = 200

# --- what a restart leaves behind -------------------------------------------
lookup_seconds[0] = 0.0
import_id = imports.start(MATT, media.MUSIC, "album", "claimed.csv",
                          listing("Claimed", 1, near={0}))
batch = until(MATT, import_id, imports.READY)
line = batch["rows"][0]["line"]
store.set_import_outcome(import_id, line, imports.SENDING)
check.equal(store.release_unsent(), 1,
            "a row a stopped process had claimed for asking is released")
check.equal(imports.get(MATT, import_id)["rows"][0]["outcome"], "",
            "and is offered again rather than reported as asked for")

store.put_import("old-style", MATT.id, "music", imports.READY, 1,
                 {"medium": "music", "unit": "album", "rows": [
                     {"line": 2, "title": "Old", "state": "matched"}]})
check.equal(store.drop_legacy_imports(), 1,
            "a list kept the old way, rows inside the batch, is dropped")
check.that(store.get_import(import_id) is not None, "and a new one is kept")

# --- finishing with nothing ticked, and with the second button ---------------
asked.clear()
check.equal(imports.ask_for_lines(MATT, import_id, set(), final=True)["state"],
            imports.DONE,
            "a final confirm with nothing ticked leaves the near misses and "
            "finishes the list")
check.equal(imports.get(MATT, import_id)["rows"][0]["outcome"], imports.LEFT,
            "the near miss is marked left")
check.equal(asked, [], "and nothing was asked for")

# --- a thread that cannot start ---------------------------------------------
real_thread = imports.threading.Thread


class _NoThread(real_thread):
    def start(self):
        raise RuntimeError("can't start new thread")


imports.threading.Thread = _NoThread
try:
    imports._start_working("never-started", MATT, media.MUSIC, "album", [])
except RuntimeError:
    pass
imports.threading.Thread = real_thread
check.that("never-started" not in imports._working,
           "a list whose thread could not start is not left marked as worked on")

# --- an old list part way through being confirmed is pruned like any other ----
store.put_import("stuck-asking", MATT.id, "music", imports.ASKING, 1,
                 {"medium": "music", "unit": "album"})
with store.db() as conn:
    conn.execute("UPDATE imports SET created_at=0 WHERE import_id='stuck-asking'")
store.prune_imports(time.time() - 3600)
check.that(store.get_import("stuck-asking") is None,
           "an old list left asking is pruned, not kept for ever")

harness.cleanup()
raise SystemExit(check.report())
