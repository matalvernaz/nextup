"""A song buskarr already has is not charged for, and a song asked for carries its album's year.

Two gaps found 2026-10-10 in a list of about a thousand songs imported one at a time:

* Music cannot tell from here whether the library has a track, so every imported song was handed
  to buskarr and charged. When buskarr found the recording already on disk it recorded the want as
  had and fetched nothing, but answered "Added.", and the import allowance paid for it (15 songs so
  far). buskarr now answers `inLibrary`, and the ask comes back "Already in the library." for free.
* buskarr's search sends each track's year, from the same catalogue as its album, and the track hit
  dropped it. Every song asked for arrived with no year, so its album directory had none either.
"""
import httpx

import harness

harness.setup(BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              MUSIC_DAILY_CAP="3", MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0",
              IMPORT_MUSIC_DAILY_SONGS="200", JELLYFIN_USER="")

import time  # noqa: E402

from app import arr, buskarr, jellyfin, media, store, wants  # noqa: E402

check = harness.Check("import already held")
store.init()

KID = jellyfin.User(id="user-kid", name="kid", is_admin=False)
media._registry = {media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                                             ("lib-music",))}
media._registry_built_at = time.monotonic()
media._registry_settled = True

# --- the track hit keeps the year ------------------------------------------------
row = {"unit": "track", "source": "deezer", "artist": "Gregory Porter", "title": "Holding On",
       "album": "Take Me to the Alley", "year": "2016", "duration": 250, "sources": ["deezer"]}
hit = buskarr._result(row, "track")
check.equal(hit.get("year"), "2016", "a track hit carries its album's year")
check.equal(buskarr._result(dict(row, year=None), "track").get("year"), None,
            "and no year when the catalogue had none, rather than an empty claim")

# --- buskarr's answer is read -----------------------------------------------------
answers = {}


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=answers["next"])


real_client = buskarr._client
buskarr._client = lambda: httpx.Client(base_url="http://buskarr.invalid",
                                       transport=httpx.MockTransport(handler))
answers["next"] = {"ok": True, "reference": "want:7", "created": True, "inLibrary": True,
                   "message": "Already in the library."}
result = buskarr.add("track", hit, "kid", bulk=True)
check.that(result.ok and result.in_library, "an inLibrary answer is carried on the result")
answers["next"] = {"ok": True, "reference": "want:8", "created": True, "message": "Added."}
result = buskarr.add("track", hit, "kid", bulk=True)
check.that(result.ok and not result.in_library,
           "a buskarr too old to say counts as not held, which charges as before")
buskarr._client = real_client

# --- and nothing is charged for it ------------------------------------------------
HELD = {"Holding On"}


def fake_add(unit, hit, by, bulk=False):
    title = hit.get("title", "")
    return arr.AddResult(True, "Already in the library." if title in HELD else "Added.",
                         f"want:{abs(hash(title)) % 1000}", title, in_library=title in HELD)


buskarr.add = fake_add
before = wants.import_allowance(KID, media.MUSIC)
state, message = wants.want(KID, media.MUSIC, hit["itemKey"], "track", dict(hit), imported=True)
check.equal((state, message), (wants.IN_LIBRARY, "Already in the library."),
            "a song already on disk comes back as in the library")
check.equal(wants.import_allowance(KID, media.MUSIC), before, "and costs nothing")
check.that(store.get(KID.key, media.MUSIC, hit["itemKey"]) is None,
           "and is not recorded as a request waiting to arrive")

other = buskarr._result(dict(row, title="Insanity", duration=330), "track")
state, _ = wants.want(KID, media.MUSIC, other["itemKey"], "track", dict(other), imported=True)
check.equal(state, wants.ON_ITS_WAY, "a song not held is asked for")
check.equal(wants.import_allowance(KID, media.MUSIC), before - 1, "and charged one song")

harness.cleanup()
raise SystemExit(check.report())
