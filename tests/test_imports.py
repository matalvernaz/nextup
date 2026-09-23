"""Reading somebody else's list, matching it, and asking for what was ticked."""
import time
import unicodedata

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4",
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_ARTIST_COST="3", MUSIC_ALBUM_COST="1",
    MUSIC_TRACK_COST="1", MOVIE_DAILY_CAP="2",
    IMPORT_PAUSE_SECONDS="0", IMPORT_MAX_ROWS="6",
)

from app import (arr, buskarr, imports, jellyfin, media, radarr,  # noqa: E402
                 store, wants)

check = harness.Check("imports")
store.init()

MATT = jellyfin.User(id="u-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="u-kid", name="kid", is_admin=False)
OTHER = jellyfin.User(id="u-other", name="other", is_admin=False)

media._registry = {
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                              ("lib-music",)),
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 2,
                              ("lib-movies",)),
}
# Stated rather than derived, and marked fresh: `available()` rebuilds a
# registry whose build time it does not believe, which here means probing a
# Radarr that is not there and a Jellyfin the network guard refuses.
media._registry_built_at = time.monotonic()
media._registry_settled = True
media._owned._value = jellyfin.Owned()
media._owned._built_at = time.monotonic()

#: What the stubbed catalogue holds, by unit. Searched by substring, the way a
#: real one behaves closely enough for this: a query finds several things and
#: the matcher has to pick.
CATALOGUE = {
    "album": [
        {"title": "Kind of Blue", "artist": "Miles Davis", "ref": "a1"},
        {"title": "Kind of Blue (2011 Remaster)", "artist": "Miles Davis",
         "ref": "a2"},
        {"title": "Rumours", "artist": "Fleetwood Mac", "ref": "a3"},
        {"title": "Rumours Live", "artist": "Fleetwood Mac", "ref": "a4"},
        {"title": "Blue", "artist": "Joni Mitchell", "ref": "a5"},
        {"title": "Café Bleu", "artist": "The Style Council", "ref": "a6"},
    ],
    "track": [
        {"title": "Lovely Day", "artist": "Bill Withers", "duration": 254},
    ],
}

searched: list[tuple[str, str]] = []


def fold(text: str) -> str:
    """Case and accents away, the way a real catalogue's search behaves."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in plain if not unicodedata.combining(ch))


def fake_search(query: str, unit: str, limit: int) -> list[dict]:
    """Anything sharing a real word with the query.

    Words of three letters or fewer are ignored, because "a" and "of" are a
    substring of most things and a catalogue that answered every query with
    its whole contents would make every test below pass for the wrong reason.
    """
    searched.append((query, unit))
    words = [word for word in fold(query).split() if len(word) > 3]
    rows = []
    for row in CATALOGUE.get(unit, []):
        haystack = fold(f"{row['artist']} {row['title']}")
        if any(word in haystack for word in words):
            rows.append(dict(row, source="deezer"))
    return [buskarr._result(row, unit) for row in rows[:limit]]


buskarr.search = fake_search

asked: list[tuple[str, str]] = []


def fake_add(unit: str, hit: dict, by: str) -> arr.AddResult:
    asked.append((unit, hit.get("title", "")))
    return arr.AddResult(True, "Sent to buskarr.", "job:1", hit.get("title", ""))


buskarr.add = fake_add


def wait_for(user, import_id: str, state: str, seconds: float = 10.0) -> dict:
    """The batch, once its thread has moved it into `state`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        batch = imports.get(user, import_id)
        if batch and batch["state"] == state:
            return batch
        time.sleep(0.01)
    batch = imports.get(user, import_id)
    raise AssertionError(
        f"import stayed in {batch and batch['state']!r}, wanted {state!r}")


# --------------------------------------------------------------------------
# Reading a file
# --------------------------------------------------------------------------

SPOTIFY = ('﻿"Track Name","Artist Name(s)","Album Name","Added At"\r\n'
           '"Lovely Day","Bill Withers","Still Bill","2024-01-02"\r\n')

sheet = imports.read(SPOTIFY.encode("utf-8"))
check.equal(sheet.delimiter, ",", "a Spotify export is read as comma separated")
check.that("track" in sheet.roles and "artist" in sheet.roles,
           "and its Track Name and Artist Name(s) columns are recognised")
check.equal(imports.suggest_unit(sheet), "track",
            "a file with a track column is suggested as a list of tracks")
rows, dupes, blanks = imports.rows(sheet, media.MUSIC, "track")
check.equal([(r.title, r.artist) for r in rows], [("Lovely Day", "Bill Withers")],
            "the byte-order mark Excel writes does not end up inside the "
            "first heading")

semi = imports.read("Artist;Album;Year\nMiles Davis;Kind of Blue;1959\n")
check.equal(semi.delimiter, ";", "a semicolon-separated file is read as one")
rows, _, _ = imports.rows(semi, media.MUSIC, "album")
check.equal((rows[0].title, rows[0].artist, rows[0].year),
            ("Kind of Blue", "Miles Davis", "1959"),
            "and its three columns land in the right places")

tabbed = imports.read("Artist\tAlbum\nFleetwood Mac\tRumours\n")
check.equal(tabbed.delimiter, "\t", "so is a tab-separated one")

dated = imports.read("Title,Released\nRumours,1977-02-04\n")
rows, _, _ = imports.rows(dated, media.MUSIC, "album")
check.equal(rows[0].year, "1977",
            "a release date is read for the year inside it, not taken whole")

messy = imports.read("Artist,Album\n\nMiles Davis,Kind of Blue\n,\n"
                     "MILES DAVIS,kind of blue\nFleetwood Mac,\n")
rows, dupes, blanks = imports.rows(messy, media.MUSIC, "album")
check.equal([r.title for r in rows], ["Kind of Blue"],
            "a blank line, an empty row and a differently-cased repeat all "
            "come to one row")
check.equal((dupes, blanks), (1, 1),
            "and both the duplicate and the row with no title are counted, "
            "not silently dropped")

plain = imports.read("Bill Withers - Lovely Day\nJoni Mitchell - Blue\n")
check.equal(plain.headings, (),
            "a list with no headings is read as data, not as a heading row")
rows, _, _ = imports.rows(plain, media.MUSIC, "track")
check.equal([(r.artist, r.title) for r in rows],
            [("Bill Withers", "Lovely Day"), ("Joni Mitchell", "Blue")],
            "and 'Artist - Title' is read apart where there is no artist "
            "column to say otherwise")

hyphenated = imports.read("Artist,Track\nThe B-52's,Jack-in-the-Box\n")
rows, _, _ = imports.rows(hyphenated, media.MUSIC, "track")
check.equal((rows[0].artist, rows[0].title), ("The B-52's", "Jack-in-the-Box"),
            "a hyphen inside a title is not a credit separator: only ' - ' is, "
            "and only where no artist column exists")

headed = imports.read("Title\nRumours\nBlue\n")
check.equal(len(headed.rows), 2,
            "a single-column file whose first line names a column loses that "
            "line to the heading")

spaced = imports.read("Artist,Album\n\n\nMiles Davis,Kind of Blue\n\n"
                      "Fleetwood Mac,Rumours\n")
rows, _, _ = imports.rows(spaced, media.MUSIC, "album")
check.equal([r.line for r in rows], [4, 6],
            "a row is numbered by the line of the file it is actually on, "
            "not by its position among the rows that were not blank -- the "
            "number is only there so somebody can go and look at it")

quoted = imports.read('Artist,Album\n"Crosby, Stills & Nash",Déjà Vu\n')
rows, _, _ = imports.rows(quoted, media.MUSIC, "album")
check.equal((rows[0].artist, rows[0].title),
            ("Crosby, Stills & Nash", "Déjà Vu"),
            "a comma inside a quoted field is part of the name, not a column "
            "break")

letterboxd = imports.read("Date,Name,Year,Letterboxd URI\n"
                          "2024-01-01,Dune,2021,https://boxd.it/x\n")
rows, _, _ = imports.rows(letterboxd, media.MOVIE, "movie")
check.equal((rows[0].title, rows[0].year), ("Dune", "2021"),
            "a Letterboxd export's Name column is the film's title, and its "
            "Date column is not mistaken for the year")

sequel = imports.read("Title,Year\n"
                      "Mission: Impossible - Dead Reckoning Part One,2023\n")
rows, _, _ = imports.rows(sequel, media.MOVIE, "movie")
check.equal((rows[0].title, rows[0].artist),
            ("Mission: Impossible - Dead Reckoning Part One", ""),
            "a film's title is never split on ' - ': only music writes a "
            "credit into the title that way, and a film does not have one")

remakes = imports.read("Title,Year\nDune,1984\nDune,2021\n")
rows, dupes, _ = imports.rows(remakes, media.MOVIE, "movie")
check.equal([(r.title, r.year) for r in rows], [("Dune", "1984"),
                                                ("Dune", "2021")],
            "two films of one name in different years are two rows, not a row "
            "and a duplicate")
check.equal(dupes, 0, "and neither is reported as ignored")

utf16 = "Artist\tAlbum\nMiles Davis\tKind of Blue\n".encode("utf-16")
rows, _, _ = imports.rows(imports.read(utf16), media.MUSIC, "album")
check.equal((rows[0].artist, rows[0].title), ("Miles Davis", "Kind of Blue"),
            "and Excel's 'Unicode Text' export, which is UTF-16 with a byte "
            "order mark, is read rather than decoded into interleaved nulls")

accented = "Artist,Album\nSigur Rós,Ágætis byrjun\n".encode("cp1252")
rows, _, _ = imports.rows(imports.read(accented), media.MUSIC, "album")
check.equal(rows[0].artist, "Sigur Rós",
            "a file a Windows spreadsheet saved decodes with its accents "
            "intact rather than as mojibake")

check.raises(imports.Unreadable, lambda: imports.read(""),
             "an empty file is refused with something to read")
check.raises(imports.Unreadable, lambda: imports.read("Artist,Album\n"),
             "so is a file that is nothing but a heading row")
check.raises(
    imports.Unreadable,
    lambda: imports.rows(imports.read("Column A,Column B\nfoo,bar\n"),
                         media.MUSIC, "album"),
    "and a file whose headings name no column this understands is refused "
    "rather than guessed at")


# --------------------------------------------------------------------------
# Deciding whether a hit is the row
# --------------------------------------------------------------------------

def hit(title, artist="", year="", unit="album", owned=False, requested=False):
    return {"itemKey": f"bk:{title}", "unit": unit, "title": title,
            "artist": artist, "year": year, "owned": owned,
            "requested": requested}


row = imports.Row(2, "Kind of Blue", "Miles Davis")
check.that(imports.is_strict(row, hit("Kind of Blue", "Miles Davis"), media.MUSIC),
           "an exact title and an exact credit is a strict match")
check.that(imports.is_strict(row, hit("KIND OF BLUE", "miles davis"), media.MUSIC),
           "and case and spacing are not what makes two spellings differ")
check.that(
    imports.is_strict(row, hit("Kind of Blue (2011 Remaster)", "Miles Davis"),
                      media.MUSIC),
    "a parenthesised remaster tag is an aside, not a different record")
check.that(
    not imports.is_strict(row, hit("Kind of Blue", "Bill Evans"), media.MUSIC),
    "the same title under a different credit is not a match")
check.that(
    not imports.is_strict(imports.Row(2, "Kind of Blue"),
                          hit("Kind of Blue", "Miles Davis"), media.MUSIC),
    "and a file that named no artist can never be certain which one it meant")
check.that(
    not imports.is_strict(imports.Row(2, "First Line of Defense", "S. Author"),
                          hit("First Line of Defense: Book 2", "S. Author"),
                          media.BOOK),
    "a volume number in the catalogue's title and not in the file's is the "
    "difference between one book and another")
check.that(
    not imports.is_strict(imports.Row(2, "Dune", "", "1984"),
                          hit("Dune", year="2021", unit="movie"), media.MOVIE),
    "two films of one name are told apart by the year the file gave")
check.that(
    imports.is_strict(imports.Row(2, "Dune", "", ""),
                      hit("Dune", year="2021", unit="movie"), media.MOVIE),
    "and a file with no year at all does not disqualify the only Dune there is")

wants_search = wants.search
wants.search = lambda q, medium, unit="", user=None: [
    hit("Kind of Blue", "Miles Davis", owned=True)]
found = imports.match(MATT, media.MUSIC, "album", row, set())
check.equal((found["state"], found["detail"]), (imports.HELD,
                                                "Already in the library."),
            "a strict match the library already holds is held back rather "
            "than offered")

wants.search = lambda q, medium, unit="", user=None: [
    hit("Kind of Blue", "Miles Davis")]
found = imports.match(MATT, media.MUSIC, "album", row, {"bk:Kind of Blue"})
check.equal(found["state"], imports.HELD,
            "and so is one this account has already asked for")

wants.search = lambda q, medium, unit="", user=None: []
check.equal(imports.match(MATT, media.MUSIC, "album", row, set())["state"],
            imports.MISSING, "a row the catalogue has never heard of is missing")


def explode(*args, **kwargs):
    raise RuntimeError("the catalogue fell over")


wants.search = explode
found = imports.match(MATT, media.MUSIC, "album", row, set())
check.equal(found["state"], imports.MISSING,
            "a search that throws loses its own row and not the whole file")
check.that("could not be asked" in found["detail"],
           "and says so rather than claiming the catalogue had nothing")
wants.search = wants_search


# --------------------------------------------------------------------------
# A whole batch
# --------------------------------------------------------------------------

LIST = ("Artist,Album\n"
        "Miles Davis,Kind of Blue\n"       # exact
        "Fleetwood Mac,Rumours\n"          # exact, with a near neighbour
        "The Style Council,Cafe Bleu\n"    # the accent is the catalogue's
        "Bill Evans,Kind of Blue\n"        # right title, wrong credit
        "Nobody At All,A Record Nobody Pressed\n")

import_id = imports.start(MATT, media.MUSIC, "album", "collection.csv", LIST)
batch = wait_for(MATT, import_id, imports.READY)
grouped = imports.groups(batch)
check.equal(sorted(r["title"] for r in grouped[imports.MATCHED]),
            ["Cafe Bleu", "Kind of Blue", "Rumours"],
            "the rows the catalogue holds are matched, and an accent the file "
            "does not type is not a difference")
check.equal(
    [r["hit"]["title"] for r in grouped[imports.MATCHED]
     if r["title"] == "Rumours"],
    ["Rumours"],
    "the near neighbour Rumours Live does not claim the row Rumours matched")
check.equal([r["title"] for r in grouped[imports.MISSING]],
            ["A Record Nobody Pressed"],
            "a row nothing matched is reported rather than dropped")
check.equal([(r["artist"], r["title"]) for r in grouped[imports.UNCERTAIN]],
            [("Bill Evans", "Kind of Blue")],
            "and a row whose title the catalogue has under another credit is "
            "offered as a near miss rather than asked for")
check.equal(grouped[imports.UNCERTAIN][0]["hit"]["artist"], "Miles Davis",
            "with the credit the catalogue actually holds shown, so the "
            "difference is visible before anything is ticked")
check.that(all(row["hit"] is None or "itemKey" in row["hit"]
               for row in batch["rows"]),
           "every kept hit carries the identifier the request will be made on")

check.that(imports.get(OTHER, import_id) is None,
           "another account cannot read somebody else's imported list")
check.that(imports.confirm(OTHER, import_id, {2, 3}) is None,
           "nor confirm one")

check.equal(imports.affordable(MATT, batch, {2, 3, 4}), None,
            "an administrator is told there is no limit rather than a number")

asked.clear()
ticked = {row["line"] for row in grouped[imports.MATCHED]}
imports.confirm(MATT, import_id, ticked)
batch = wait_for(MATT, import_id, imports.DONE)
check.equal(sorted(title for _, title in asked),
            ["Café Bleu", "Kind of Blue", "Rumours"],
            "confirming asks for exactly the ticked rows, under the spelling "
            "the catalogue gave rather than the one the file used")
check.equal(len(batch["report"]["asked"]), 3,
            "and the report names all three")
check.equal(batch["report"]["refused"], [],
            "with nothing refused on an uncapped account")
check.that(all(store.get(MATT.key, media.MUSIC, f"bk:album:deezer:a{n}")
               is not None for n in (1, 3, 6)),
           "each one is in the ledger afterwards, keyed the way a request "
           "made from the search page would be")

# A second confirm of the same batch must not ask again: the batch has moved
# on, and a reloaded page that re-posts is the ordinary way to find that out.
asked.clear()
imports.confirm(MATT, import_id, ticked)
check.equal(asked, [],
            "confirming a list that has already been asked for does nothing")


# --------------------------------------------------------------------------
# What a capped account gets
# --------------------------------------------------------------------------

SHORT = ("Artist,Album\n"
         "Miles Davis,Kind of Blue\n"
         "Fleetwood Mac,Rumours\n"
         "Joni Mitchell,Blue\n")

import_id = imports.start(KID, media.MUSIC, "album", "theirs.csv", SHORT)
batch = wait_for(KID, import_id, imports.READY)
lines = {row["line"] for row in imports.groups(batch)[imports.MATCHED]}
check.equal(len(lines), 3, "three of the capped account's rows matched")
check.equal(imports.affordable(KID, batch, lines), 3,
            "and today's allowance of three covers all three")

asked.clear()
imports.confirm(KID, import_id, lines)
batch = wait_for(KID, import_id, imports.DONE)
check.equal(len(batch["report"]["asked"]), 3,
            "so all three are asked for")
check.equal(wants.allowance(KID, media.MUSIC), 0,
            "and the day's allowance is spent exactly, not overspent")

import_id = imports.start(KID, media.MUSIC, "album",
                          "theirs-again.csv",
                          "Artist,Album\nThe Style Council,Cafe Bleu\n")
batch = wait_for(KID, import_id, imports.READY)
lines = {row["line"] for row in imports.groups(batch)[imports.MATCHED]}
check.equal(imports.affordable(KID, batch, lines), 0,
            "with the allowance gone, the page says up front that it covers "
            "none of them")
asked.clear()
imports.confirm(KID, import_id, lines)
batch = wait_for(KID, import_id, imports.DONE)
check.equal(asked, [], "and confirming asks the acquisition tool for nothing")
check.equal(len(batch["report"]["refused"]), 1,
            "the refusal is reported rather than swallowed")
check.that("Café Bleu" in batch["report"]["refused"][0],
           "naming which row it was, so it can be imported again tomorrow")

# A line that was never on offer -- held back, or matched to nothing -- cannot
# be smuggled in by posting its number.
import_id = imports.start(MATT, media.MUSIC, "album", "sneaky.csv",
                          "Artist,Album\nNobody At All,A Record Nobody "
                          "Pressed\n")
batch = wait_for(MATT, import_id, imports.READY)
check.equal([r["state"] for r in batch["rows"]], [imports.MISSING],
            "the one row of this list matched nothing")
asked.clear()
imports.confirm(MATT, import_id, {2})
check.equal(imports.get(MATT, import_id)["state"], imports.READY,
            "so confirming it leaves the list where it was")
check.equal(asked, [], "and asks for nothing")


# --------------------------------------------------------------------------
# A pass that stopped
# --------------------------------------------------------------------------

import_id = imports.start(MATT, media.MUSIC, "album", "interrupted.csv",
                          "Artist,Album\nMiles Davis,Kind of Blue\n")
wait_for(MATT, import_id, imports.READY)
with store.db() as conn:
    conn.execute("UPDATE imports SET state='reading', touched_at=? "
                 "WHERE import_id=?",
                 (time.time() - 10_000, import_id))
stopped = imports.get(MATT, import_id)
check.equal(stopped["state"], imports.FAILED,
            "a pass whose row count has not moved for far longer than it "
            "should is reported as stopped, not as still working")
check.that("restarted" in stopped["error"],
           "with a reason to read")
check.that("restarted" in imports.get(MATT, import_id)["error"],
           "and the reason survives a reload, having been written down rather "
           "than made up on the way out of one call")


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

long_list = "Artist,Album\n" + "".join(
    f"Artist {n},Album {n}\n" for n in range(7))
check.raises(
    imports.Unreadable,
    lambda: imports.start(MATT, media.MUSIC, "album", "long.csv", long_list),
    "a file longer than the row limit is refused before anything is searched")
check.raises(
    imports.Unreadable,
    lambda: imports.start(MATT, media.MUSIC, "album", "big.csv",
                          b"x" * 2_000_001),
    "and one larger than the byte limit is refused before it is even decoded")
check.raises(
    imports.Unreadable,
    lambda: imports.start(MATT, "cheese", "", "x.csv", "Title\nBrie\n"),
    "a medium this server does not serve cannot be imported into")

before = store.get_import(import_id)
check.that(before is not None, "an import is in the database while it is live")
store.prune_imports(time.time() + 1)
check.that(store.get_import(import_id) is None,
           "and is forgotten once it is older than the retention window")

harness.cleanup()
raise SystemExit(check.report())
