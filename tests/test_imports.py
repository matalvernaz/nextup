"""Reading somebody else's list, matching it, and asking for what was ticked."""
import json
import time
import unicodedata

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4",
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_ARTIST_COST="3", MUSIC_ALBUM_COST="1",
    MUSIC_TRACK_COST="1", MOVIE_DAILY_CAP="2",
    IMPORT_PAUSE_SECONDS="0", IMPORT_MAX_ROWS="6", IMPORT_MAX_ROWS_MUSIC="8",
)

from app import (arr, buskarr, config, imports, jellyfin, media, radarr,  # noqa: E402
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
        # Not on Deezer, so only the full search can find it.
        {"title": "Hounds of Love", "artist": "Kate Bush", "ref": "a7",
         "source": "itunes"},
    ],
    "track": [
        {"title": "Lovely Day", "artist": "Bill Withers", "duration": 254},
    ],
}

searched: list[tuple[str, str, tuple[str, ...]]] = []


def fold(text: str) -> str:
    """Case and accents away, the way a real catalogue's search behaves."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in plain if not unicodedata.combining(ch))


def fake_search(query: str, unit: str, limit: int,
                sources: tuple[str, ...] = ()) -> list[dict]:
    """Anything sharing a real word with the query, from the named sources.

    Words of three letters or fewer are ignored, because "a" and "of" are a
    substring of most things and a catalogue that answered every query with
    its whole contents would make every test below pass for the wrong reason.
    A row is on Deezer unless it says otherwise.
    """
    searched.append((query, unit, tuple(sources)))
    words = [word for word in fold(query).split() if len(word) > 3]
    rows = []
    for row in CATALOGUE.get(unit, []):
        haystack = fold(f"{row['artist']} {row['title']}")
        source = row.get("source", "deezer")
        if sources and source not in sources:
            continue
        if any(word in haystack for word in words):
            rows.append(dict(row, source=source))
    return [buskarr._result(row, unit) for row in rows[:limit]]


buskarr.search = fake_search

asked: list[tuple[str, str]] = []


#: Whether each add above was marked bulk, in step with `asked`.
bulk_marks: list[bool] = []


def fake_add(unit: str, hit: dict, by: str, bulk: bool = False) -> arr.AddResult:
    asked.append((unit, hit.get("title", "")))
    bulk_marks.append(bulk)
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
    lambda: imports.rows(imports.read("Year,Artist\n2020,Someone\n"),
                         media.MUSIC, "album"),
    "and a file whose headings name no column this understands is refused "
    "rather than guessed at")

# One recognised heading is still a heading row. Reading such a file line by
# line turned "Dune,5" into a film called "Dune,5" and asked the catalogue for
# the heading row as well.
rated = imports.read("Title,Rating\nDune,5\nAlien,4\n")
rows, _, _ = imports.rows(rated, media.MOVIE, "movie")
check.equal([r.title for r in rows], ["Dune", "Alien"],
            "a file with a title column and columns this does not know reads "
            "the title column, not whole lines")
check.equal(rated.headings, ("Title", "Rating"),
            "and its first line is the heading row, not a film")

for text, medium, unit, expected in (
        ("Film,Year\nDune,2021\n", media.MOVIE, "movie", ("Dune", "", "2021")),
        ("Book,Author\nDune,Frank Herbert\n", media.BOOK, "book",
         ("Dune", "Frank Herbert", "")),
        ("Show,Season\nLost,1\n", media.SERIES, "series", ("Lost", "", ""))):
    rows, _, _ = imports.rows(imports.read(text), medium, unit)
    check.equal([(r.title, r.artist, r.year) for r in rows], [expected],
                f"a file headed {text.splitlines()[0]!r} reads its columns")

# The other side of that: a list with no headings whose first entry happens to
# contain a heading word must not lose that entry to a heading row nobody sees.
for text, expected in (
        ("Movie, 1999\nAlien\n", ["Movie, 1999", "Alien"]),
        ("Film, 2021\nDune, 2021\n", ["Film, 2021", "Dune, 2021"]),
        ("Film, Film, Film\nDune\n", ["Film, Film, Film", "Dune"])):
    sheet = imports.read(text)
    rows, _, _ = imports.rows(sheet, media.MOVIE, "movie")
    check.equal(([r.title for r in rows], sheet.headings), (expected, ()),
                f"a list starting {text.splitlines()[0]!r} keeps that line as "
                f"a title")

movies = imports.read("Movies\nDune\nAlien\n")
check.equal(len(movies.rows), 2,
            "a single column headed with a plural loses that line to the "
            "heading too")

try:
    old_mac = imports.read("Title,Year\rDune,2021\rAlien,1979\r")
    rows, _, _ = imports.rows(old_mac, media.MOVIE, "movie")
    read_back = [(r.title, r.year, r.line) for r in rows]
except Exception as exc:  # noqa: BLE001 - the crash is what this records
    read_back = f"{type(exc).__name__}: {exc}"
check.equal(read_back, [("Dune", "2021", 2), ("Alien", "1979", 3)],
            "a file whose lines end in a bare carriage return is read, with "
            "its line numbers intact")

unclosed = 'Title,Year\n"Dune,2021\n' + "Alien,1979\n" * 13000
try:
    imports.read(unclosed)
except imports.Unreadable:
    check.that(True, "a file the csv module gives up on is refused with a "
                     "sentence")
except Exception as exc:  # noqa: BLE001 - the crash is what this records
    check.that(False, f"a file the csv module gives up on is refused with a "
                      f"sentence, not {type(exc).__name__}: {exc}")
else:
    check.that(False, "a file the csv module gives up on is refused with a "
                      "sentence")


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
wants.search = lambda q, medium, unit="", user=None, sources=(): [
    hit("Kind of Blue", "Miles Davis", owned=True)]
found = imports.match(MATT, media.MUSIC, "album", row, set())
check.equal((found["state"], found["detail"]), (imports.HELD,
                                                "Already in the library."),
            "a strict match the library already holds is held back rather "
            "than offered")

wants.search = lambda q, medium, unit="", user=None, sources=(): [
    hit("Kind of Blue", "Miles Davis")]
found = imports.match(MATT, media.MUSIC, "album", row, {"bk:Kind of Blue"})
check.equal(found["state"], imports.HELD,
            "and so is one this account has already asked for")

wants.search = lambda q, medium, unit="", user=None, sources=(): []
check.equal(imports.match(MATT, media.MUSIC, "album", row, set())["state"],
            imports.MISSING, "a row the catalogue has never heard of is missing")


def explode(*args, **kwargs):
    raise RuntimeError("the catalogue fell over")


wants.search = explode
found = imports.match(MATT, media.MUSIC, "album", row, set())
check.equal(found["state"], imports.MISSING,
            "a search that throws loses its own row and not the whole file")
check.that("failed" in found["detail"] and found["detail"] != imports.NO_MATCH,
           "and says so rather than claiming the catalogue had nothing")
wants.search = wants_search

# Deezer first. A music row it has an exact match for costs one quick lookup;
# only a row it has nothing certain for goes on to every catalogue.
searched.clear()
found = imports.match(MATT, media.MUSIC, "album",
                      imports.Row(2, "Kind of Blue", "Miles Davis"), set())
check.equal((found["state"], [entry[2] for entry in searched]),
            (imports.MATCHED, [("deezer",)]),
            "an exact match on Deezer is looked up once, on Deezer alone")
searched.clear()
found = imports.match(MATT, media.MUSIC, "album",
                      imports.Row(2, "Hounds of Love", "Kate Bush"), set())
check.equal((found["state"], [entry[2] for entry in searched]),
            (imports.MATCHED, [("deezer",), ()]),
            "a row Deezer has nothing certain for is looked up everywhere, "
            "and found there")
searched.clear()
imports.match(MATT, media.MUSIC, "artist", imports.Row(2, "Miles Davis"), set())
check.equal([entry[2] for entry in searched], [()],
            "an artist is looked up everywhere from the start, since telling "
            "two acts of one name apart is what MusicBrainz is for")


# --------------------------------------------------------------------------
# A whole batch
# --------------------------------------------------------------------------

LIST = ("Artist,Album\n"
        "Miles Davis,Kind of Blue\n"       # exact
        "Fleetwood Mac,Rumours\n"          # exact, with a near neighbour
        "The Style Council,Cafe Bleu\n"    # the accent is the catalogue's
        "Bill Evans,Kind of Blue\n"        # right title, wrong credit
        "Nobody At All,A Record Nobody Pressed\n")

asked.clear()
import_id = imports.start(MATT, media.MUSIC, "album", "collection.csv", LIST)
# Ready rather than done: the near miss is still waiting for a decision.
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
            "kept as a near miss")
check.equal(grouped[imports.UNCERTAIN][0]["hit"]["artist"], "Miles Davis",
            "with the credit the catalogue actually holds shown, so the "
            "difference is visible before anybody ticks it")
check.that(all(row["hit"] is None or "itemKey" in row["hit"]
               for row in batch["rows"]),
           "every kept hit carries the identifier the request will be made on")

check.equal(sorted(title for _, title in asked),
            ["Café Bleu", "Kind of Blue", "Rumours"],
            "the exact matches are asked for while the list is looked up, "
            "under the spelling the catalogue gave rather than the file's")
check.equal([r["outcome"] for r in grouped[imports.MATCHED]],
            [imports.ASKED] * 3, "and each row says so")
check.equal(grouped[imports.UNCERTAIN][0]["outcome"], "",
            "while the near miss is not asked for on a guess")
check.equal(len(imports.report(batch)["asked"]), 3,
            "the report names all three")
check.that(all(store.get(MATT.key, media.MUSIC, f"bk:album:deezer:a{n}")
               is not None for n in (1, 3, 6)),
           "each one is in the ledger afterwards, keyed the way a request "
           "made from the search page would be")

check.that(imports.get(OTHER, import_id) is None,
           "another account cannot read somebody else's imported list")
near = grouped[imports.UNCERTAIN][0]["line"]
check.that(imports.ask_for_lines(OTHER, import_id, {near}) is None,
           "nor ask for anything on it")

# Ticking the near miss asks for it, and with nothing left to decide the list
# ends by itself. Its match was already asked for by the first row, so the ask
# costs nothing and says so.
asked.clear()
imports.ask_for_lines(MATT, import_id, {near})
batch = wait_for(MATT, import_id, imports.DONE)
check.equal(asked, [], "a near miss whose match is already asked for is free")
check.equal([r["outcome"] for r in imports.groups(batch)[imports.UNCERTAIN]],
            [imports.ALREADY], "and is reported as already asked for")

# A second ask of the same line must not ask again: the row is decided, and a
# reloaded page that re-posts is the ordinary way to find that out.
asked.clear()
imports.ask_for_lines(MATT, import_id, {near})
check.equal(asked, [], "asking again for a row already dealt with does nothing")


# --------------------------------------------------------------------------
# What a capped account gets
# --------------------------------------------------------------------------

SHORT = ("Artist,Album\n"
         "Miles Davis,Kind of Blue\n"
         "Fleetwood Mac,Rumours\n"
         "Joni Mitchell,Blue\n")

# Music from a list has an allowance of its own, counted in songs, so that a
# list does not spend the three a day somebody has for searching and is not
# refused past it either: what does not fit waits for a later day.
config.IMPORT_MUSIC_DAILY_SONGS = 3 * config.IMPORT_ALBUM_SONGS

asked.clear()
bulk_marks.clear()
import_id = imports.start(KID, media.MUSIC, "album", "theirs.csv", SHORT)
batch = wait_for(KID, import_id, imports.DONE)
check.equal(len(imports.report(batch)["asked"]), 3,
            "all three are asked for, and with nothing to decide the list is "
            "done as soon as it is looked up")
check.equal(bulk_marks, [True, True, True],
            "each marked bulk, so buskarr works them after this person's "
            "own searches")
check.equal(wants.import_allowance(KID, media.MUSIC), 0,
            "the day's import allowance is spent exactly, not overspent")
check.equal(wants.allowance(KID, media.MUSIC), 3,
            "while the three a day for searching are untouched")

asked.clear()
import_id = imports.start(KID, media.MUSIC, "album",
                          "theirs-again.csv",
                          "Artist,Album\nThe Style Council,Cafe Bleu\n")
batch = wait_for(KID, import_id, imports.DONE)
check.equal(asked, [], "with the import allowance gone nothing more is sent")
check.equal(imports.report(batch)["refused"], [], "nor refused")
check.equal(imports.report(batch)["waiting"], ["Café Bleu by The Style Council"],
            "it waits, and the report names it")
check.equal(imports.queue_status(KID)["waiting"], 1,
            "in this account's queue")

check.equal(imports._release_for(KID), 0,
            "the queue asks for nothing while the allowance is still spent")
check.equal(store.queued_count(KID.key), 1, "and keeps the row")

# A day later the allowance has come back, and the queue asks by itself.
with store.db() as conn:
    conn.execute("UPDATE requests SET requested_at = requested_at - 90000 "
                 "WHERE user_key=?", (KID.key,))
asked.clear()
bulk_marks.clear()
check.equal(imports._release_for(KID), 1, "the next day the row goes in")
check.equal((asked, bulk_marks), ([("album", "Café Bleu")], [True]),
            "asked for, in bulk, as the import would have")
check.equal(imports.queue_status(KID), None, "and the queue is empty")

# A second list never jumps a first: while anything waits, a new list waits
# behind it even if the allowance has room.
store.queue_rows(KID.key, media.MUSIC, "earlier", [(2, "Earlier", {
    "itemKey": "bk:album:deezer:zz", "unit": "album", "title": "Earlier"})])
asked.clear()
import_id = imports.start(KID, media.MUSIC, "album", "third.csv",
                          "Artist,Album\nFleetwood Mac,Rumours Live\n")
batch = wait_for(KID, import_id, imports.DONE)
check.equal(asked, [], "a list behind waiting rows asks for nothing yet")
check.equal([row["label"] for row in store.queued(KID.key)],
            ["Earlier", "Rumours Live by Fleetwood Mac"],
            "and queues behind them, in order")
check.equal(imports.clear_queue(KID), 2,
            "stopping the queue drops every waiting row")
check.equal(store.queued_count(KID.key), 0, "so nothing is left to ask for")
config.IMPORT_MUSIC_DAILY_SONGS = 200

# A line that was never on offer -- matched to nothing, or already decided --
# cannot be smuggled in by posting its number.
import_id = imports.start(MATT, media.MUSIC, "album", "sneaky.csv",
                          "Artist,Album\nNobody At All,A Record Nobody "
                          "Pressed\n")
batch = wait_for(MATT, import_id, imports.DONE)
check.equal([r["state"] for r in batch["rows"]], [imports.MISSING],
            "the one row of this list matched nothing")
asked.clear()
imports.ask_for_lines(MATT, import_id, {2})
check.equal(asked, [], "so posting its line asks for nothing")


# --------------------------------------------------------------------------
# A list a restart interrupted
# --------------------------------------------------------------------------

jellyfin.all_users = lambda: {"matt": MATT.id, "kid": KID.id}
jellyfin.user = lambda name: {"matt": MATT, "kid": KID}[name]

asked.clear()
import_id = imports.start(MATT, media.MUSIC, "album", "interrupted.csv",
                          "Artist,Album\nJoni Mitchell,Blue\n"
                          "Fleetwood Mac,Rumours Live\n")
wait_for(MATT, import_id, imports.DONE)
# As though the container had stopped after the first row: the second row was
# never looked up, let alone asked for, and the list still says it is reading.
with store.db() as conn:
    conn.execute("DELETE FROM import_rows WHERE import_id=? AND line=3",
                 (import_id,))
    conn.execute("DELETE FROM requests WHERE user_key=? AND item_key=?",
                 (MATT.key, "bk:album:deezer:a4"))
    conn.execute("UPDATE imports SET state='reading', done=1, touched_at=? "
                 "WHERE import_id=?", (time.time() - 10_000, import_id))
asked.clear()
check.equal(imports.get(MATT, import_id)["state"], imports.READING,
            "a list that kept its rows is not called failed while it waits "
            "to carry on")
check.equal(imports.resume_interrupted(), 1, "and the next pass picks it up")
batch = wait_for(MATT, import_id, imports.DONE)
check.equal([row["line"] for row in batch["rows"]], [2, 3],
            "carrying on from the row it stopped at")
check.equal(asked, [("album", "Rumours Live")],
            "and asking only for what it had not reached, not starting over")

# One from before the rows were kept cannot carry on, and says it stopped.
import_id = imports.start(MATT, media.MUSIC, "album", "old.csv",
                          "Artist,Album\nMiles Davis,Kind of Blue\n")
wait_for(MATT, import_id, imports.DONE)
with store.db() as conn:
    payload = json.loads(conn.execute(
        "SELECT payload FROM imports WHERE import_id=?",
        (import_id,)).fetchone()["payload"])
    payload.pop("items")
    conn.execute("UPDATE imports SET state='reading', touched_at=?, payload=? "
                 "WHERE import_id=?",
                 (time.time() - 10_000, json.dumps(payload), import_id))
stopped = imports.get(MATT, import_id)
check.equal(stopped["state"], imports.FAILED,
            "a list that cannot carry on is reported as stopped, not as "
            "still working")
check.that("restarted" in stopped["error"], "with a reason to read")
check.that("restarted" in imports.get(MATT, import_id)["error"],
           "and the reason survives a reload, having been written down rather "
           "than made up on the way out of one call")


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

long_list = "Artist,Album\n" + "".join(
    f"Artist {n},Album {n}\n" for n in range(9))
check.raises(
    imports.Unreadable,
    lambda: imports.start(MATT, media.MUSIC, "album", "long.csv", long_list),
    "a file longer than the row limit is refused before anything is searched")
check.equal((imports.max_rows(media.MUSIC), imports.max_rows(media.MOVIE)),
            (8, 6), "music, where the long lists are, has a limit of its own")
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
