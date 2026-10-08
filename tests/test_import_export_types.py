"""A music export's own Type column decides which rows are songs, albums and artists.

The file below has the shape of a real one: an Apple Music library exported
through TuneMyMusic, with that export's headings, byte-order mark, quoting and
Type words. Only the rows are made up. The first reading of the Type column was
written against guessed words ("track", "album") and threw away every row of
the real file, because that export calls a library song "Favorite" and a song
in one of the person's playlists "Playlist". The whole upload was refused.
"""
import time
from urllib.parse import parse_qs, urlsplit

import harness

harness.setup(BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              MUSIC_DAILY_CAP="3", MUSIC_TRACK_COST="1",
              IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="")

from fastapi.testclient import TestClient
from app import buskarr, imports, itunes, jellyfin, main, media, sessions, store, wants

check = harness.Check("import export types")
store.init()

HEADINGS = "Track name,Artist name,Album,Playlist name,Type,ISRC,Apple - id"
RECORDS = [
    ("Bohemian Rhapsody", "Queen", "A Night at the Opera", "Library Songs", "Favorite",
     "GBUM71029604", "1440806041"),
    ("Dreams", "Fleetwood Mac", "Rumours", "Library Songs", "Favorite",
     "USWB10400049", "651345829"),
    ("Rumours", "Fleetwood Mac", "", "Library Albums", "Album", "00081227967075", "l.AbCdEfG"),
    ("A Night at the Opera", "Queen", "", "Library Albums", "Album", "00602547288219", "l.HiJkLmN"),
    ("Queen", "", "", "Library Artists", "Artist", "", "r.AbCdEfG"),
    ("Fleetwood Mac", "", "", "Library Artists", "Artist", "", "r.HiJkLmN"),
    # A playlist's song is usually in the library as well, and is then the
    # same song twice. One that is only in a playlist is a song of its own.
    ("Dreams", "Fleetwood Mac", "Rumours", "Road Trip", "Playlist",
     "USWB10400049", "651345829"),
    ("Go Your Own Way (Live)", "Fleetwood Mac", "The Dance", "Road Trip", "Playlist",
     "USWB19700123", "i.OpQrStUvWxYzAb"),
    ("Under Pressure", "Queen & David Bowie", "Hot Space", "Road Trip", "Playlist",
     "GBUM71029620", "1440806599"),
]


def export(records, headings=HEADINGS) -> bytes:
    """The bytes as the export writes them: a mark, every cell quoted, no last newline."""
    lines = [headings] + [",".join(f'"{cell}"' for cell in record) for record in records]
    return ("﻿" + "\n".join(lines)).encode("utf-8")


sheet = imports.read(export(RECORDS))
check.equal(imports.suggest_unit(sheet), "track", "a Track name column suggests songs")

skipped: list[dict] = []
songs_found, duplicates, blanks = imports.rows(sheet, media.MUSIC, "track", skipped)
check.equal([row.title for row in songs_found],
            ["Bohemian Rhapsody", "Dreams", "Go Your Own Way (Live)", "Under Pressure"],
            "library songs and playlist songs are both read as songs")
check.equal([row.line for row in songs_found], [2, 3, 9, 10],
            "each song keeps the line it is on in the file")
check.equal([row.record_type for row in songs_found],
            ["Favorite", "Favorite", "Playlist", "Playlist"], "the Type is kept on each row")
check.equal(duplicates, 1, "a playlist song that is also a library song is a duplicate")
check.equal([(row["line"], row["type"]) for row in skipped],
            [(4, "Album"), (5, "Album"), (6, "Artist"), (7, "Artist")],
            "albums and artists are left out of a list of songs, and said to be")

skipped = []
albums, _, _ = imports.rows(sheet, media.MUSIC, "album", skipped)
check.equal([(row.title, row.artist) for row in albums],
            [("Rumours", "Fleetwood Mac"), ("A Night at the Opera", "Queen")],
            "an album row's title is in the Track name column, its album cell empty")
check.equal(len(skipped), 7, "songs and artists are left out of a list of albums")

skipped = []
artists, _, _ = imports.rows(sheet, media.MUSIC, "artist", skipped)
check.equal([row.title for row in artists], ["Queen", "Fleetwood Mac"],
            "an artist row's name is in the Track name column")

# A Type word nothing here knows is not a reason to drop the row. The next
# export will have its own words for things, and dropping what it calls a song
# is how a whole file disappeared. Read as what the person chose, and said so.
unknown: dict[str, int] = {}
skipped = []
liked = [("Dreams", "Fleetwood Mac", "Rumours", "", "Liked", "", ""),
         ("Rumours", "Fleetwood Mac", "", "", "Album", "", ""),
         ("Under Pressure", "Queen & David Bowie", "Hot Space", "", "Liked", "", ""),
         ("Bohemian Rhapsody", "Queen", "A Night at the Opera", "", "", "", "")]
read, _, _ = imports.rows(imports.read(export(liked)), media.MUSIC, "track", skipped, unknown)
check.equal([row.title for row in read], ["Dreams", "Under Pressure", "Bohemian Rhapsody"],
            "unknown and empty Type cells are read as the unit chosen")
check.equal(unknown, {"Liked": 2}, "the unknown Type words are counted for the page")
check.equal([row["type"] for row in skipped], ["Album"], "a known other type is still left out")

# Through the upload form, as somebody would send it.
OWNER = jellyfin.User(id="owner", name="owner", is_admin=False)
jellyfin.user_from_token = lambda token: OWNER
jellyfin.credential_rejected = lambda force=True: False
media._registry = {media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("music",))}
media._registry_built_at = time.monotonic()
media._registry_settled = True
media.owned = lambda *args, **kwargs: jellyfin.Owned()
wants.states = lambda user, **_: []
wants.search = lambda *args, **kwargs: []
imports._library_for = lambda *args: None
# The export's Apple ids would be looked up; this test is about its Type column.
itunes.songs_by_id = lambda ids: {}
client = TestClient(main.app, follow_redirects=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue(OWNER.id, OWNER.id))


def upload(data: bytes, unit: str = ""):
    return client.post("/import", data={"medium": "music", "unit": unit},
                       files={"listing": ("My Apple Music Library Tracks.csv", data, "text/csv")})


def finished(location: str) -> dict:
    batch_id = location.rsplit("/", 1)[1]
    deadline = time.monotonic() + 5
    while imports.get(OWNER, batch_id)["state"] == imports.READING and time.monotonic() < deadline:
        time.sleep(.01)
    return imports.get(OWNER, batch_id)


sent = upload(export(RECORDS))
where = sent.headers.get("location", "")
check.that(where.startswith("/import/") and "msg=" not in where,
           f"the export is taken, not refused: {where}")
if where.startswith("/import/") and "msg=" not in where:
    batch = finished(where)
    check.equal((batch["unit"], batch["total"], batch["duplicates"], len(batch["skipped"])),
                ("track", 4, 1, 4), "read as four songs, one duplicate, four rows of other types")
    check.equal(batch["state"], imports.DONE, "and read to the end without failing")
    page = " ".join(client.get(where).text.split())
    check.that("Row 4 of your file: Rumours. Its type is Album, not a track." in page,
               "the page says which rows were left out and why")

sent = upload(export(liked))
where = sent.headers.get("location", "")
check.that(where.startswith("/import/") and "msg=" not in where, f"unknown types are taken: {where}")
if where.startswith("/import/") and "msg=" not in where:
    batch = finished(where)
    check.equal(batch.get("unknown_types"), {"Liked": 2}, "the batch keeps the unknown Type words")
    check.that("Rows whose type this did not recognise were read as tracks: Liked (2)."
               in " ".join(client.get(where).text.split()), "and the page says what it did with them")

albums_only = [record for record in RECORDS if record[4] == "Album"]
sent = upload(export(albums_only), unit="track")
said = parse_qs(urlsplit(sent.headers.get("location", "")).query).get("msg", [""])[0]
check.equal(said, "None of the rows in that file is a track. Its Type column says Album (2). "
                  "Choose “An album” to import those.",
            "a list with nothing of the chosen type says what it does have")

harness.cleanup()
raise SystemExit(check.report())
