"""Imported albums and artists are charged the songs they actually add.

An album is held at the tracks its catalogue entry lists and an artist at
IMPORT_ARTIST_SONGS, because nobody knows an artist's count until buskarr has
walked the discography. Once buskarr has made its list, the hold becomes the
number of songs it added. The fakes answer the way buskarr's API does: `/add`
gives a job reference, and `/states` says `total: None` while the job runs and
the count of songs it queued once it has, which leaves out songs already in
the library and songs somebody already asked for.
"""
import threading
import time

import harness

harness.setup(BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              MUSIC_DAILY_CAP="3", MUSIC_ALBUM_COST="1", MUSIC_ARTIST_COST="3",
              MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0",
              IMPORT_MUSIC_DAILY_SONGS="200", IMPORT_ALBUM_SONGS="12",
              IMPORT_ARTIST_SONGS="100", JELLYFIN_USER="")

from app import arr, buskarr, imports, jellyfin, media, playlists, store, wants  # noqa: E402

check = harness.Check("import charges")
store.init()

KID = jellyfin.User(id="user-kid", name="kid", is_admin=False)
SIB = jellyfin.User(id="user-sib", name="sib", is_admin=False)
PAL = jellyfin.User(id="user-pal", name="pal", is_admin=False)
media._registry = {media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                                             ("lib-music",))}
media._registry_built_at = time.monotonic()
media._registry_settled = True

#: buskarr's answer for each job reference, as `/states` gives it.
JOBS: dict[str, dict] = {}


def fake_add(unit, hit, by, bulk=False):
    return arr.AddResult(True, "Queued.", f"job:{hit['ref']}", hit.get("title", ""))


buskarr.add = fake_add
buskarr.states = lambda refs: {ref: JOBS[ref] for ref in refs if ref in JOBS}


def running() -> dict:
    return {"state": "running", "have": 0, "total": None, "message": "Adding tracks now."}


def done(songs: int) -> dict:
    return {"state": "pending" if songs else "have", "have": 0, "total": songs,
            "message": f"0 of {songs} tracks in the library."}


def failed() -> dict:
    return {"state": "error", "have": 0, "total": 0, "message": "The add failed."}


def artist(ref: str) -> dict:
    return buskarr._result({"source": "deezer", "ref": ref, "name": ref.title()}, "artist")


def album(ref: str, tracks=None) -> dict:
    return buskarr._result({"source": "deezer", "ref": ref, "title": ref.title(),
                            "artist": "Band", "tracks": tracks}, "album")


def ask(user, hit, imported=True):
    return wants.want(user, media.MUSIC, hit["itemKey"], hit["unit"], dict(hit),
                      imported=imported)


def charge(user, hit) -> tuple[int, int]:
    found = store.get(user.key, media.MUSIC, hit["itemKey"])
    return found["cost"], found["provisional"]


# What is held when somebody asks.
check.equal(wants.import_cost("track"), 1, "a song is one song")
check.equal(wants.import_cost("artist", artist("anyone")), 100, "an artist is held at the estimate")
check.equal(wants.import_cost("album", album("ep", 4)), 4,
            "an album is held at the tracks its catalogue entry lists")
check.equal(wants.import_cost("album", album("unlisted")), 12,
            "an album with no listed count is held at the estimate")
for listed in (0, -3, "x", [5]):
    check.equal(wants.import_cost("album", {"trackCount": listed}), 12,
                f"a count that is not a number of tracks is not believed: {listed!r}")
check.equal(wants.import_cost("album", {"trackCount": 900}), 200,
            "nothing is held at more than a day's allowance")

# An artist: held, then charged what buskarr added.
small = artist("small band")
ask(KID, small)
check.equal(charge(KID, small), (100, 1), "an imported artist is held at 100, marked as a hold")
check.equal(wants.import_allowance(KID, media.MUSIC), 100, "the hold comes off the day's 200")
JOBS["job:small band"] = running()
check.equal(wants.settle_import_charges(), 0, "a job still running has no count yet")
check.equal(charge(KID, small), (100, 1), "so the hold stays")
JOBS["job:small band"] = done(15)
check.equal(wants.settle_import_charges(), 1, "once buskarr has counted, the charge settles")
check.equal(charge(KID, small), (15, 0), "to the 15 songs it added")
check.equal(wants.import_allowance(KID, media.MUSIC), 185, "and the other 85 come back")
JOBS["job:small band"] = done(40)
check.equal(wants.settle_import_charges(), 0, "a settled charge is not settled again")
check.equal(charge(KID, small), (15, 0), "whatever buskarr says later")

# An album: held at its listed tracks, charged the ones it added.
ep = album("four songs", 4)
ask(KID, ep)
check.equal(charge(KID, ep), (4, 1), "an imported album is held at its four listed tracks")
JOBS["job:four songs"] = done(3)
wants.settle_import_charges()
check.equal(charge(KID, ep), (3, 0), "a song of it already in the library is not charged")

# A discography bigger than a day.
big = artist("prolific")
ask(KID, big)
JOBS["job:prolific"] = done(922)
wants.settle_import_charges()
check.equal(charge(KID, big), (200, 0), "922 songs is charged as a whole day, the most one ask costs")
check.equal(wants.import_allowance(KID, media.MUSIC), 0, "which uses the day up")

# Nothing added, or a failed add, costs nothing.
owned = artist("all owned")
broken = artist("broken")
ask(SIB, owned)
ask(SIB, broken)
JOBS["job:all owned"] = done(0)
JOBS["job:broken"] = failed()
check.equal(wants.settle_import_charges(), 2, "both settle")
check.equal((charge(SIB, owned), charge(SIB, broken)), ((0, 0), (0, 0)),
            "an artist whose songs are all here already, and an add that failed, cost nothing")
check.equal(wants.import_allowance(SIB, media.MUSIC), 200, "so the whole day is still there")

# Somebody else's add: free, never a hold, never charged their count.
buskarr.add = lambda unit, hit, by, bulk=False: arr.AddResult(
    True, "Already on the list.", "job:someone else", hit.get("title", ""), created=False)
shared = artist("shared")
ask(SIB, shared)
check.equal(charge(SIB, shared), (0, 0), "a repeat of another account's add costs nothing and is not a hold")
JOBS["job:someone else"] = done(150)
wants.settle_import_charges()
check.equal(charge(SIB, shared), (0, 0), "and that add's count is never charged to this account")
buskarr.add = fake_add

# Asked for from search, not a list: the ordinary allowance, never a hold.
plain = artist("searched for")
ask(SIB, plain, imported=False)
check.equal(charge(SIB, plain), (3, 0), "an artist from search costs the ordinary 3")
JOBS["job:searched for"] = done(50)
wants.settle_import_charges()
check.equal(charge(SIB, plain), (3, 0), "and is never settled")

# buskarr unreachable, then a buskarr too old for the batch call.
later = artist("later")
ask(SIB, later)
JOBS["job:later"] = done(10)
buskarr.states = lambda refs: {}
check.equal(wants.settle_import_charges(), 0, "unreachable: nothing is known, nothing changes")
check.equal(charge(SIB, later), (100, 1), "the hold stays")
buskarr.states = lambda refs: None
buskarr.state = lambda ref: JOBS.get(ref)
check.equal(wants.settle_import_charges(), 1, "an old buskarr is asked one reference at a time")
check.equal(charge(SIB, later), (10, 0), "and settles the same way")
buskarr.states = lambda refs: {ref: JOBS[ref] for ref in refs if ref in JOBS}

# An ask being decided for the same account is not changed under it.
waiting = artist("waiting")
ask(SIB, waiting)
JOBS["job:waiting"] = done(20)
deciding = store.key_lock(SIB.key, media.MUSIC)
deciding.acquire()
settler = threading.Thread(target=wants.settle_import_charges)
settler.start()
settler.join(.3)
check.that(settler.is_alive() and charge(SIB, waiting) == (100, 1),
           "a settle waits while an ask on the same account is being decided")
deciding.release()
settler.join(5)
check.equal(charge(SIB, waiting), (20, 0), "and settles once it is done")

# A hold from more than a day ago no longer counts, and is left alone.
stale = artist("stale")
ask(PAL, stale)
with store.db() as conn:
    conn.execute("UPDATE requests SET requested_at=? WHERE user_key=? AND item_key=?",
                 (time.time() - 2 * wants.DAY_SECONDS, PAL.key, stale["itemKey"]))
JOBS["job:stale"] = done(5)
check.equal(wants.settle_import_charges(), 0, "a hold older than a day is not looked up")

# Through a real imported list: the album's listed count survives to the charge.
CATALOGUE = {"Band Seven Tracks": album("seven tracks", 7)}
buskarr.search = lambda q, unit, limit, sources=(): (
    [CATALOGUE[q]] if unit == "album" and q in CATALOGUE else [])
imports._library_for = lambda *args: None
import_id = imports.start(PAL, media.MUSIC, "album", "albums.csv",
                          "Artist,Album\nBand,Seven Tracks\n")
deadline = time.monotonic() + 5
while (imports.get(PAL, import_id)["state"] == imports.READING
       and time.monotonic() < deadline):
    time.sleep(.01)
check.equal(charge(PAL, CATALOGUE["Band Seven Tracks"]), (7, 1),
            "an album from a list is held at the seven tracks its catalogue entry lists")
store.queue_rows(PAL.key, media.MUSIC, import_id,
                 [(3, "Waiting", imports._keep(album("eight tracks", 8)))])
check.equal(imports.queue_status(PAL)["songs"], 8,
            "a waiting album is counted at its listed tracks too")
store.clear_queue(PAL.key)

# The queue pass settles before it asks, and a settle that fails does not stop it.
calls: list[str] = []
real = (wants.settle_import_charges, imports.release_queue,
        imports.resume_interrupted, playlists.resolve_pending, imports.time)


class Stop(Exception):
    pass


class OnePass:
    """`time` for the loop: the first wait passes, the second ends the test."""
    def __init__(self):
        self.waits = 0

    def sleep(self, seconds):
        self.waits += 1
        if self.waits > 1:
            raise Stop

    def time(self):
        return time.time()


def run_one_pass():
    imports.time = OnePass()
    try:
        imports._queue_loop()
    except Stop:
        pass


wants.settle_import_charges = lambda: calls.append("settle") or 0
imports.release_queue = lambda: calls.append("release") or 0
imports.resume_interrupted = lambda: 0
playlists.resolve_pending = lambda: None
run_one_pass()
check.equal(calls, ["settle", "release"], "settled first, so what came back is used in the same pass")


def broken_settle():
    raise RuntimeError("buskarr said something odd")


calls.clear()
wants.settle_import_charges = broken_settle
run_one_pass()
check.equal(calls, ["release"], "a settle that fails does not stop the queue")
(wants.settle_import_charges, imports.release_queue, imports.resume_interrupted,
 playlists.resolve_pending, imports.time) = real

harness.cleanup()
raise SystemExit(check.report())
