"""A list of songs made into a Jellyfin playlist, and songs already here not asked for again.

What is pinned: a song the library holds is not asked for (nor charged), whether or not there is a
playlist; "Song (Live)" in the library is not "Song" in the file. With a playlist name, the songs
the library holds go into a playlist of the importer's in the order of the file; the rest are asked
for and go in at their place when they turn up. A playlist this service made is found again by
name and nothing in it is added twice; a playlist of that name it did not make is refused rather
than added to, since it may be somebody else's; a deleted one is made again.
"""
import time

import harness

harness.setup(
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_ALBUM_COST="1", MUSIC_ARTIST_COST="3",
    MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, arr, buskarr, imports, jellyfin, main, media,  # noqa: E402
                 playlists, sessions, store, wants)

check = harness.Check("playlist import")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="user-kid", name="kid", is_admin=False)
OTHER = jellyfin.User(id="user-other", name="other", is_admin=False)
USERS = {"matt": MATT, "kid": KID, "other": OTHER}
jellyfin.all_users = lambda: {name: user.id for name, user in USERS.items()}
jellyfin.user = lambda name: USERS[name]
jellyfin.user_from_token = lambda token: {"kid-token": KID}[token]
jellyfin.credential_rejected = lambda force=True: False
media.owned = lambda *args, **kwargs: jellyfin.Owned()
media._registry = {
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                              ("lib-music",)),
}
media._registry_built_at = time.monotonic()
media._registry_settled = True

# --- a Jellyfin that holds a music library and playlists ---------------------
LIBRARY = [
    {"Id": "lib-lovely", "Name": "Lovely Day", "Artists": ["Bill Withers"],
     "AlbumArtist": "Bill Withers", "Album": "Menagerie"},
    {"Id": "lib-dreams", "Name": "Dreams", "Artists": ["Fleetwood Mac"],
     "AlbumArtist": "Fleetwood Mac", "Album": "Rumours"},
    {"Id": "lib-wellerman-live", "Name": "Wellerman (Live)",
     "Artists": ["The Longest Johns"], "AlbumArtist": "The Longest Johns",
     "Album": "Live"},
]
PLAYLISTS: dict[str, dict] = {}


def create_playlist(uid, name):
    pid = f"pl-{len(PLAYLISTS) + 1}"
    PLAYLISTS[pid] = {"name": name, "owner": uid, "items": [], "open": False}
    return pid


def visible_playlists(uid):
    return [{"Id": pid, "Name": p["name"]} for pid, p in PLAYLISTS.items()
            if p["owner"] == uid or p["open"]]


def playlist_items(uid, pid):
    return list(PLAYLISTS[pid]["items"]) if pid in PLAYLISTS else None


def add_to_playlist(uid, pid, ids, position=None):
    items = PLAYLISTS[pid]["items"]
    at = len(items) if position is None else position
    items[at:at] = ids


library_reads = [0]


def audio_items():
    library_reads[0] += 1
    return list(LIBRARY)


jellyfin.audio_items = audio_items
jellyfin.audio_count = lambda: len(LIBRARY)
jellyfin.create_playlist = create_playlist
jellyfin.visible_playlists = visible_playlists
jellyfin.playlist_items = playlist_items
jellyfin.add_to_playlist = add_to_playlist

# --- a catalogue and buskarr for the songs the library lacks -----------------
SONGS = {"Bill Withers": ["Lovely Day", "Ain't No Sunshine"],
         "Fleetwood Mac": ["Dreams", "Go Your Own Way"],
         "The Longest Johns": ["Wellerman"],
         "Kate Bush": ["Running Up That Hill", "Wuthering Heights"]}


def fake_search(q, unit, limit, sources=()):
    return [buskarr._result({"title": title, "artist": artist,
                             "ref": f"r-{title}", "source": "deezer",
                             "duration": 200}, unit)
            for artist, titles in SONGS.items() for title in titles
            if f"{artist} {title}".casefold() == q.casefold()]


buskarr.search = fake_search
asked: list[str] = []


def fake_add(unit, hit, by, bulk=False):
    asked.append(hit.get("title", ""))
    return arr.AddResult(True, "Sent to buskarr.", f"want:{hit.get('title')}",
                         hit.get("title", ""))


buskarr.add = fake_add


def until(user, import_id, state, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        batch = imports.get(user, import_id)
        if batch["state"] == state:
            return batch
        time.sleep(0.01)
    return imports.get(user, import_id)


SONG_LIST = ("Artist Name(s),Track Name\n"
             "Bill Withers,Ain't No Sunshine\n"      # 2: not in the library
             "Bill Withers,Lovely Day\n"             # 3: in the library
             "The Longest Johns,Wellerman\n"         # 4: only a live cut is
             "Fleetwood Mac,Dreams\n")               # 5: in the library

# --- songs already here, no playlist -----------------------------------------
asked.clear()
import_id = imports.start(KID, media.MUSIC, "track", "songs.csv", SONG_LIST)
batch = until(KID, import_id, imports.DONE)
by_line = {row["line"]: row for row in batch["rows"]}
check.equal(sorted(asked), ["Ain't No Sunshine", "Wellerman"],
            "only the songs the library lacks are asked for")
check.equal((by_line[3]["detail"], by_line[3].get("itemId")),
            ("Already in the library.", "lib-lovely"),
            "a song the library holds is recognised as already here")
check.that(by_line[4]["state"] != imports.HELD,
           "a live cut in the library is not the studio song the file asks for")
check.equal(wants.import_allowance(KID, media.MUSIC), 200 - 2,
            "and the songs already here cost the import allowance nothing")

# --- a new playlist -----------------------------------------------------------
# Wellerman was asked for above and is on its way: it is waited for too.
asked.clear()
import_id = imports.start(KID, media.MUSIC, "track", "road.csv",
                          SONG_LIST.replace("Bill Withers,Ain't No Sunshine",
                                            "Fleetwood Mac,Go Your Own Way"),
                          playlist="Road Trip")
batch = until(KID, import_id, imports.DONE)
pid = batch["playlist"]["id"]
check.equal((PLAYLISTS[pid]["name"], PLAYLISTS[pid]["owner"]),
            ("Road Trip", KID.id), "a playlist of that name is made for the importer")
check.equal(PLAYLISTS[pid]["items"], ["lib-lovely", "lib-dreams"],
            "with the songs the library holds, in the order of the file")
summary = playlists.summary(import_id, batch["playlist"])
check.equal((summary["inPlaylist"], summary["pending"]), (2, 2),
            "and the two being asked for are waited for")

# One of them turns up in the library: it goes in at its place, which is
# before Lovely Day (line 2 comes before line 3), not at the end.
LIBRARY.append({"Id": "lib-goyourownway", "Name": "Go Your Own Way",
                "Artists": ["Fleetwood Mac"], "AlbumArtist": "Fleetwood Mac",
                "Album": "Rumours"})
check.equal(playlists.resolve_pending(), 1, "a song that turned up is put in")
check.equal(PLAYLISTS[pid]["items"], ["lib-goyourownway", "lib-lovely", "lib-dreams"],
            "at its place in the order of the file")
check.equal(playlists.summary(import_id, batch["playlist"])["pending"], 1,
            "and is no longer waited for")
check.equal(playlists.resolve_pending(), 0,
            "a song that has not turned up yet is still waited for, not added")

# --- the same name again ------------------------------------------------------
asked.clear()
import_id = imports.start(KID, media.MUSIC, "track", "road-again.csv",
                          "Artist Name(s),Track Name\n"
                          "Fleetwood Mac,Dreams\n"
                          "Kate Bush,Running Up That Hill\n",
                          playlist="road trip")
batch = until(KID, import_id, imports.DONE)
check.equal(batch["playlist"]["id"], pid,
            "a later list of the same name, in any case, goes into the same playlist")
check.equal(PLAYLISTS[pid]["items"].count("lib-dreams"), 1,
            "and a song already in it is not added a second time")
check.equal(len(PLAYLISTS), 1, "no second playlist is made")

# --- a playlist of that name this service did not make ------------------------
PLAYLISTS["pl-theirs"] = {"name": "Beep", "owner": OTHER.id, "items": [],
                          "open": True}
try:
    imports.start(KID, media.MUSIC, "track", "beep.csv", SONG_LIST, playlist="Beep")
    check.that(False, "a playlist name somebody else's playlist has is refused")
except imports.Unreadable as refused:
    check.that("did not make" in str(refused),
               f"a playlist name somebody else's playlist has is refused: {refused}")
check.equal(PLAYLISTS["pl-theirs"]["items"], [], "and nothing is added to it")

# --- one this service made, deleted in Jellyfin since --------------------------
del PLAYLISTS[pid]
import_id = imports.start(KID, media.MUSIC, "track", "again.csv",
                          "Artist Name(s),Track Name\nBill Withers,Lovely Day\n",
                          playlist="Road Trip")
batch = until(KID, import_id, imports.DONE)
check.that(batch["playlist"]["id"] != pid and batch["playlist"]["id"] in PLAYLISTS,
           "a playlist deleted since is made again")
check.equal(store.pending_count(import_id), 0,
            "and a song the library holds goes straight in, never waited for")

# --- only a list of songs can be a playlist -----------------------------------
try:
    imports.start(KID, media.MUSIC, "album", "albums.csv",
                  "Artist,Album\nFleetwood Mac,Rumours\n", playlist="Albums")
    check.that(False, "a list of albums with a playlist name is refused")
except imports.Unreadable as refused:
    check.that("songs" in str(refused),
               f"a list of albums with a playlist name is refused: {refused}")

# --- a playlist deleted while songs are waited for ----------------------------
store.add_pending(KID.key, "pl-vanished", "x", 2, "Nothing", "Nobody")
playlists.resolve_pending()
check.equal(store.pending_count("x"), 0,
            "songs waited for by a playlist deleted since are dropped")

# --- the API and the page -----------------------------------------------------
api_app = FastAPI()
api_app.include_router(api.router)
client = TestClient(api_app, raise_server_exceptions=False)
caps = client.get("/api/v1/capabilities", headers={"X-Emby-Token": "kid-token"}).json()
check.equal(caps["importList"]["playlist"], True,
            "capabilities say a list of songs can be given a playlist name")
started = client.post("/api/v1/import", headers={"X-Emby-Token": "kid-token"},
                      json={"medium": "music", "unit": "track", "text":
                            "Artist Name(s),Track Name\nBill Withers,Lovely Day\n",
                            "playlist": "Via API"}).json()
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    body = client.get(f"/api/v1/import/{started['importId']}",
                      headers={"X-Emby-Token": "kid-token"}).json()
    if body["state"] == "done":
        break
    time.sleep(0.02)
check.equal({k: body["playlist"][k] for k in ("name", "inPlaylist", "pending")},
            {"name": "Via API", "inPlaylist": 1, "pending": 0},
            "the API takes a playlist name and says how it is filling")
check.that(body["playlist"]["id"] in PLAYLISTS, "with the playlist's id, to open it")

web = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
web.cookies.set(sessions.COOKIE_NAME, sessions.issue("kid-token", KID.id))
form = web.get("/import").text
check.that('name="playlist"' in form and 'aria-describedby="playlist-help"' in form,
           "the form has a playlist name field, with its explanation tied to it")
page = " ".join(web.get(f"/import/{started['importId']}").text.split())
check.that("Playlist “Via API”: 1 song from this list in it." in page,
           "and the list's page says how the playlist is filling")

# --- what the review round found ---------------------------------------------
from app import songs  # noqa: E402

check.that(songs.song_key("Song (Live, 2011 Remaster)") != songs.song_key("Song"),
           "a remaster tag beside a version word does not take the version with it")
check.equal(songs.song_key("Song - Remastered 2009"), songs.song_key("Song"),
            "while a remaster alone is the same recording")
index = songs.LibraryIndex([
    {"Id": "bs", "Name": "Home", "Artists": ["Belle and Sebastian"],
     "AlbumArtist": "Belle and Sebastian"},
    {"Id": "sg", "Name": "The Boxer", "Artists": ["Simon & Garfunkel"],
     "AlbumArtist": "Simon & Garfunkel"},
    {"Id": "ru", "Name": "Любовь", "Artists": ["Кино"], "AlbumArtist": "Кино"}])
check.equal(index.find("Home", "Sebastian"), None,
            "a band's name is not cut at 'and' to match another artist")
check.equal(index.find("The Boxer", "Simon"), None, "nor at '&'")
check.equal(index.find("The Boxer", "Simon and Garfunkel"), "sg",
            "while '&' and 'and' are the same join")
check.equal(index.find("Любовь", "Кино"), "ru",
            "and a song titled in another script is found")

# A song already in the playlist in the middle of a batch keeps the order.
PLAYLISTS["pl-anchor"] = {"name": "Anchor", "owner": KID.id, "items": ["B"],
                          "open": False}
playlists.add_found(KID, "pl-anchor", "anchor-list", [(2, "A"), (3, "B"), (4, "C")])
check.equal(PLAYLISTS["pl-anchor"]["items"], ["A", "B", "C"],
            "songs either side of one already in the playlist go either side of it")

# Where somebody has a song in twice, the new one goes beside the list's copy.
check.equal(playlists._position(["A", "X", "A", "C"], {2: "A", 4: "C"}, 3), 3,
            "after the last copy of the earlier song, before the later one")

# Two writers at once add a song once.
import threading  # noqa: E402
PLAYLISTS["pl-race"] = {"name": "Race", "owner": KID.id, "items": [], "open": False}
slow_items = jellyfin.playlist_items


def slow_playlist_items(uid, pid):
    found = slow_items(uid, pid)
    time.sleep(0.05)
    return found


jellyfin.playlist_items = slow_playlist_items
writers = [threading.Thread(target=playlists.add_found,
                            args=(KID, "pl-race", f"race-{n}", [(2, "S")]))
           for n in range(2)]
for writer in writers:
    writer.start()
for writer in writers:
    writer.join()
jellyfin.playlist_items = slow_items
check.equal(PLAYLISTS["pl-race"]["items"], ["S"], "two writers at once add a song once")

# A library song that could not be put in just then goes in later as that
# very item, not whatever a second search picks.
LIBRARY.append({"Id": "lib-dreams-live", "Name": "Dreams", "Artists": ["Fleetwood Mac"],
                "AlbumArtist": "Fleetwood Mac", "Album": "Live"})
PLAYLISTS["pl-flaky"] = {"name": "Flaky", "owner": KID.id, "items": [], "open": False}
real_add = jellyfin.add_to_playlist


def refusing_add(uid, pid, ids, position=None):
    raise jellyfin.JellyfinUnavailable("down")


jellyfin.add_to_playlist = refusing_add
imports._add_found(KID, {"id": "pl-flaky", "name": "Flaky"}, "flaky-list",
                   [(2, "lib-dreams-live")])
jellyfin.add_to_playlist = real_add
playlists.resolve_pending()
check.equal(PLAYLISTS["pl-flaky"]["items"], ["lib-dreams-live"],
            "it goes in as the item first found, not the first copy a search "
            "for its title turns up")

# The library is read again only when its count of songs changes.
songs.LibraryIndex.load()
reads = library_reads[0]
songs.LibraryIndex.load()
songs.LibraryIndex.load()
check.equal(library_reads[0] - reads, 0, "an unchanged library is not read again")
LIBRARY.append({"Id": "lib-new", "Name": "New", "Artists": ["Someone"],
                "AlbumArtist": "Someone", "Album": "New"})
songs.LibraryIndex.load()
check.equal(library_reads[0] - reads, 1, "a library with a new song is")

# A list that fails part way still puts in the library songs it had found.
real_ask = imports._ask_one


def failing_ask(import_id, user, medium, row):
    raise RuntimeError("something broke")


imports._ask_one = failing_ask
import_id = imports.start(KID, media.MUSIC, "track", "breaks.csv",
                          "Artist Name(s),Track Name\n"
                          "Bill Withers,Lovely Day\n"
                          "Kate Bush,Wuthering Heights\n",
                          playlist="Breaks")
batch = until(KID, import_id, imports.FAILED)
imports._ask_one = real_ask
check.equal(PLAYLISTS[batch["playlist"]["id"]]["items"], ["lib-lovely"],
            "a list that stopped on an error still put in what it had found")

harness.cleanup()
raise SystemExit(check.report())
