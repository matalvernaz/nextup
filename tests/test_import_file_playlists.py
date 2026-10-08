"""One file, every playlist it names: a whole-library export makes each of its playlists.

TuneMyMusic writes a whole Apple Music or Spotify library into one file, with a "Playlist name"
column: the library's own songs under "Library Songs", then each playlist's songs under its name,
most of them songs already listed under Library Songs. The form takes one playlist name, so a
real export of twelve playlists (2026-10-05) made none of them, and every playlist song was
dropped as a duplicate of a library song before anything could put it anywhere.

Pinned here: each named playlist is made once and filled in its own order, a song on several
playlists goes into each at its own place, the library's own views are not playlists, a name
already taken by a playlist this did not make is skipped without refusing the file, and a song
an earlier list already asked for or queued is waited for rather than asked for again.
"""
import json
import time

import harness

harness.setup(
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="",
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (api, arr, buskarr, imports, itunes, jellyfin, main, media,  # noqa: E402
                 playlists, sessions, store, wants)

check = harness.Check("import file playlists")
store.init()

ME = jellyfin.User(id="user-me", name="me", is_admin=False)
SOMEONE = jellyfin.User(id="user-someone", name="someone", is_admin=False)
USERS = {"me": ME, "someone": SOMEONE}
jellyfin.all_users = lambda: {name: user.id for name, user in USERS.items()}
jellyfin.user = lambda name: USERS[name]
jellyfin.user_from_token = lambda token: ME
jellyfin.credential_rejected = lambda force=True: False
media.owned = lambda *args, **kwargs: jellyfin.Owned()
media._registry = {media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("lib",))}
media._registry_built_at = time.monotonic()
media._registry_settled = True
itunes.songs_by_id = lambda ids: {}

LIBRARY = [
    {"Id": "lib-dreams", "Name": "Dreams", "Artists": ["Fleetwood Mac"],
     "AlbumArtist": "Fleetwood Mac", "Album": "Rumours"},
    {"Id": "lib-chain", "Name": "The Chain", "Artists": ["Fleetwood Mac"],
     "AlbumArtist": "Fleetwood Mac", "Album": "Rumours"},
    {"Id": "lib-sunshine", "Name": "Ain't No Sunshine", "Artists": ["Bill Withers"],
     "AlbumArtist": "Bill Withers", "Album": "Just As I Am"},
]
PLAYLISTS: dict[str, dict] = {}


def create_playlist(uid, name):
    pid = f"pl-{len(PLAYLISTS) + 1}"
    PLAYLISTS[pid] = {"name": name, "owner": uid, "items": [], "open": False}
    return pid


jellyfin.create_playlist = create_playlist
jellyfin.visible_playlists = lambda uid: [{"Id": pid, "Name": p["name"]}
                                          for pid, p in PLAYLISTS.items()
                                          if p["owner"] == uid or p["open"]]
jellyfin.playlist_items = lambda uid, pid: list(PLAYLISTS[pid]["items"]) if pid in PLAYLISTS else None


def add_to_playlist(uid, pid, ids, position=None):
    items = PLAYLISTS[pid]["items"]
    at = len(items) if position is None else position
    items[at:at] = ids


jellyfin.add_to_playlist = add_to_playlist
jellyfin.audio_items = lambda: list(LIBRARY)
jellyfin.audio_count = lambda: len(LIBRARY)

LENGTHS = {"Go Your Own Way": 223, "Lovely Day": 254, "Wuthering Heights": 268,
           "Running Up That Hill": 300, "Dreams": 257, "The Chain": 270,
           "Ain't No Sunshine": 125}
ARTISTS = {"Go Your Own Way": "Fleetwood Mac", "Lovely Day": "Bill Withers",
           "Wuthering Heights": "Kate Bush", "Running Up That Hill": "Kate Bush",
           "Dreams": "Fleetwood Mac", "The Chain": "Fleetwood Mac",
           "Ain't No Sunshine": "Bill Withers"}


def fake_search(q, unit, limit, sources=()):
    return [buskarr._result({"title": title, "artist": artist, "ref": f"r-{title}",
                             "source": "deezer", "duration": LENGTHS[title],
                             "album": "Somewhere"}, unit)
            for title, artist in ARTISTS.items() if f"{artist} {title}".casefold() == q.casefold()]


buskarr.search = fake_search
asked: list[str] = []


def fake_add(unit, hit, by, bulk=False):
    asked.append(hit.get("title", ""))
    return arr.AddResult(True, "Sent to buskarr.", f"want:{hit.get('title')}", hit.get("title", ""))


buskarr.add = fake_add
wants.states = lambda user, **_: []


def until(user, import_id, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        batch = imports.get(user, import_id)
        if batch["state"] not in (imports.READING, imports.ASKING):
            return batch
        time.sleep(0.01)
    return imports.get(user, import_id)


HEADINGS = "Track name,Artist name,Album,Playlist name,Type,ISRC,Apple - id"
EXPORT = "\n".join([
    HEADINGS,
    "Dreams,Fleetwood Mac,Rumours,Library Songs,Favorite,,",               # 2 library
    "Go Your Own Way,Fleetwood Mac,Rumours,Library Songs,Favorite,,",      # 3 asked
    "Lovely Day,Bill Withers,Menagerie,Library Songs,Favorite,,",          # 4 asked
    "Rumours,Fleetwood Mac,,Library Albums,Album,,",                       # 5 an album
    "Fleetwood Mac,,,Library Artists,Artist,,",                            # 6 an artist
    "The Chain,Fleetwood Mac,Rumours,Road Trip,Playlist,,",                # 7 library, own row
    "Go Your Own Way,Fleetwood Mac,Rumours,Road Trip,Playlist,,",          # 8 = line 3
    "Dreams,Fleetwood Mac,Rumours,Road Trip,Playlist,,",                   # 9 = line 2
    "Wuthering Heights,Kate Bush,The Kick Inside,Road Trip,Playlist,,",    # 10 asked
    "Dreams,Fleetwood Mac,Rumours,70s,Playlist,,",                         # 11 = line 2
    "Lovely Day,Bill Withers,Menagerie,70s,Playlist,,",                    # 12 = line 4
    "Ain't No Sunshine,Bill Withers,Just As I Am,70s,Playlist,,",          # 13 library
    "Wuthering Heights,Kate Bush,The Kick Inside,Taken,Playlist,,",        # 14 = line 10
])

print("the file is read as songs, each with its playlist")
sheet = imports.read(EXPORT)
read_rows, duplicates, _ = imports.rows(sheet, media.MUSIC, "track")
by_line = {row.line: row for row in read_rows}
check.equal(sorted(by_line), [2, 3, 4, 7, 10, 13], "one row per song, at its first line")
check.equal((by_line[2].playlist, by_line[7].playlist, by_line[13].playlist), ("", "Road Trip", "70s"),
            "a library view is no playlist; a playlist row keeps its own")
check.equal(by_line[2].placements, ((9, "Road Trip"), (11, "70s")),
            "a library song also on playlists keeps each of those lines")
check.equal(by_line[10].placements, ((14, "Taken"),), "and so does a playlist song on another")
check.equal(duplicates, 5, "the later lines still count as duplicates")
check.equal(imports._file_playlists(read_rows), ["Road Trip", "70s", "Taken"],
            "every playlist the file names, once, in file order")

print("\nan import makes each playlist and fills it in its own order")
# Somebody else's open playlist already has one of the names.
PLAYLISTS["pl-theirs"] = {"name": "Taken", "owner": SOMEONE.id, "items": ["x"], "open": True}
import_id = imports.start(ME, media.MUSIC, "track", "library.csv", EXPORT)
batch = until(ME, import_id)
made = {p["name"]: p["id"] for p in batch.get("playlists", [])}
check.equal(sorted(made), ["70s", "Road Trip"], "the two free names are made")
check.equal([p["name"] for p in batch.get("playlists_refused", [])], ["Taken"],
            "a name somebody else's playlist has is skipped, and said to be")
check.equal(batch["state"], imports.DONE, "the file is still read to the end")
check.equal(sorted(asked), ["Go Your Own Way", "Lovely Day", "Wuthering Heights"],
            "each song not in the library is asked for once, whatever playlists it is on")
road, seventies = PLAYLISTS[made["Road Trip"]], PLAYLISTS[made["70s"]]
check.equal(road["items"], ["lib-chain", "lib-dreams"],
            "Road Trip holds its library songs in its own order: line 7 before line 9")
check.equal(seventies["items"], ["lib-dreams", "lib-sunshine"],
            "70s holds Dreams from line 11 before Ain't No Sunshine from line 13")
check.equal(PLAYLISTS["pl-theirs"]["items"], ["x"], "and nothing is added to theirs")
with store.db() as conn:
    pending = sorted((r["playlist_id"], r["line"], r["title"]) for r in conn.execute(
        "SELECT playlist_id, line, title FROM playlist_pending WHERE import_id=?", (import_id,)))
check.equal(pending, sorted([(made["Road Trip"], 8, "Go Your Own Way"),
                             (made["Road Trip"], 10, "Wuthering Heights"),
                             (made["70s"], 12, "Lovely Day")]),
            "each song on its way waits at its own line in each of its playlists")
summaries = playlists.summaries(import_id, batch)
check.equal([(s["name"], s["inPlaylist"], s["pending"]) for s in summaries],
            [("Road Trip", 2, 2), ("70s", 2, 1)], "each playlist counts only its own")

print("\nsongs that turn up go in at their places")
LIBRARY.append({"Id": "lib-goyourownway", "Name": "Go Your Own Way", "Artists": ["Fleetwood Mac"],
                "AlbumArtist": "Fleetwood Mac", "Album": "Rumours"})
LIBRARY.append({"Id": "lib-lovely", "Name": "Lovely Day", "Artists": ["Bill Withers"],
                "AlbumArtist": "Bill Withers", "Album": "Menagerie"})
check.equal(playlists.resolve_pending(), 2, "both arrivals are put in")
check.equal(road["items"], ["lib-chain", "lib-goyourownway", "lib-dreams"],
            "line 8 between lines 7 and 9")
check.equal(seventies["items"], ["lib-dreams", "lib-lovely", "lib-sunshine"],
            "line 12 between lines 11 and 13")

print("\nthe page and the API say which playlists the list fills")
client = TestClient(main.app, follow_redirects=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue(ME.id, ME.id))
page = " ".join(client.get(f"/import/{import_id}").text.split())
check.that("Playlist “Road Trip”: 3 songs from this list in it" in page, "Road Trip is listed")
check.that("Playlist “70s”: 3 songs from this list in it" in page, "and 70s")
check.that("Not made: “Taken”." in page, "and the name it could not use")
api_app = FastAPI()
api_app.include_router(api.router)
detail = TestClient(api_app).get(f"/api/v1/import/{import_id}", headers={"X-Emby-Token": "t"}).json()
check.equal([p["name"] for p in detail.get("playlists", [])], ["Road Trip", "70s"],
            "the API lists every playlist")
check.equal([p["name"] for p in detail.get("playlistsRefused", [])], ["Taken"],
            "and the one not made")

print("\na song an earlier list asked for or queued is waited for, not asked for again")
asked.clear()
# Queued by an earlier list with another length bucket: a different ledger key, the same song.
store.queue_rows(ME.key, media.MUSIC, import_id, [(99, "Running Up That Hill by Kate Bush",
                                                   {"itemKey": "bk:track:earlier", "title": "Running Up That Hill",
                                                    "artist": "Kate Bush", "unit": "track", "medium": "music",
                                                    "durationSeconds": 298.0})])
LENGTHS["Running Up That Hill"] = 304
second = imports.start(ME, media.MUSIC, "track", "more.csv", "\n".join([
    HEADINGS, "Running Up That Hill,Kate Bush,Hounds of Love,Road Trip,Playlist,,"]))
batch = until(ME, second)
row = batch["rows"][0]
check.equal((row["state"], row["detail"]), (imports.HELD, "Already asked for."),
            "the queued song is recognised as already asked for")
check.equal(asked, [], "and nothing is asked for")
with store.db() as conn:
    waiting = conn.execute("SELECT title FROM playlist_pending WHERE import_id=?", (second,)).fetchall()
check.equal([r["title"] for r in waiting], ["Running Up That Hill"],
            "it still waits to go into the playlist")
check.equal(made["Road Trip"], {p["name"]: p["id"] for p in batch["playlists"]}["Road Trip"],
            "the same Road Trip, not a second one")

print("\na far longer take of the same title is another recording, and is asked for")
asked.clear()
LENGTHS["Use Me"], ARTISTS["Use Me"] = 225, "Bill Withers"
store.queue_rows(ME.key, media.MUSIC, import_id, [(98, "Use Me by Bill Withers",
                                                   {"itemKey": "bk:track:live", "title": "Use Me",
                                                    "artist": "Bill Withers", "unit": "track", "medium": "music",
                                                    "durationSeconds": 380.0})])
third = imports.start(ME, media.MUSIC, "track", "use-me.csv", "\n".join([
    HEADINGS, "Use Me,Bill Withers,Still Bill,,Favorite,,"]))
batch = until(ME, third)
check.that(batch["rows"][0]["detail"] != "Already asked for.",
           "a 225-second Use Me is not the 380-second one already queued")

print("\na file naming too many playlists is refused before any is made")
before = len(PLAYLISTS)
many = "\n".join([HEADINGS] + [f"Song {n},Somebody,,List {n},Playlist,,"
                               for n in range(imports.MAX_FILE_PLAYLISTS + 1)])
check.raises(imports.Unreadable, lambda: imports.start(ME, media.MUSIC, "track", "many.csv", many),
             "fifty-one playlists is one too many")
check.equal(len(PLAYLISTS), before, "and none was made")

print("\nadding out of file order still lands each song at its place")
PLAYLISTS["pl-order"] = {"name": "Order", "owner": ME.id, "items": [], "open": False}
playlists.add_found(ME, "pl-order", "order-list", [(10, "a"), (30, "c")])
playlists.add_found(ME, "pl-order", "order-list", [(20, "b"), (40, "d")])
check.equal(PLAYLISTS["pl-order"]["items"], ["a", "b", "c", "d"],
            "a batch whose lines straddle one already placed is split around it")
playlists.add_found(ME, "pl-order", "order-list", [(35, "c2"), (5, "z")])
check.equal(PLAYLISTS["pl-order"]["items"], ["z", "a", "b", "c", "c2", "d"],
            "and a batch handed in out of order goes in by line")

harness.cleanup()
raise SystemExit(check.report())
