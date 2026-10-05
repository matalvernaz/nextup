"""Bringing in a list somebody already has.

Somebody arrives with a file: the albums on their shelf, a playlist exported
from a streaming service, the films they have collected. This turns that file
into requests as it goes.

Each row is looked up in the catalogue, and a row that matches exactly is asked
for there and then. A row that only nearly matches is not: a wrong match here
is a download, not a bad suggestion, so near misses are kept for the person to
tick or leave. Rows already in the library or already asked for are skipped,
and rows nothing matched are listed with a way to search for them by hand.

The work runs in a thread, a row at a time, and every row is written down as
it is done, so a page can show what a long list has turned into while the rest
of it is still being looked up. Lists take turns row by row, so one long list
does not hold up somebody else's short one, and a list a restart interrupted
carries on from where it got to.

Requests go through `wants.want`, one row at a time, exactly as the search
page does. The daily allowance, the duplicate check and the ledger entry are
that module's to enforce, and an importer that wrote itself a faster path
would be precisely the second caller its docstring exists to prevent.
"""
import csv
import io
import json
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass

from . import config, jellyfin, logs, media, playlists, songs, store, wants

log = logs.get("imports")


class Unreadable(Exception):
    """The file could not be turned into rows, and why."""


#: A batch's life. `reading` and `asking` are the two worked phases, and each
#: has a row count that grows; the other three are resting places.
READING = "reading"
READY = "ready"
ASKING = "asking"
DONE = "done"
FAILED = "failed"

#: What became of one row. `matched` is ticked for the reader, `uncertain` is
#: shown unticked, and neither `held` nor `missing` is offered at all.
MATCHED = "matched"
UNCERTAIN = "uncertain"
HELD = "held"
MISSING = "missing"

#: What a row the catalogue had nothing for says. Named because the review
#: page leaves it unsaid under its "No match" heading and shows any other.
NO_MATCH = "No match found."

#: What became of a row that was acted on, kept beside its state. The state is
#: what the lookup found; an exact match that was asked for is still MATCHED.
ASKED = "asked"
ALREADY = "already"
WAITING = "waiting"
REFUSED = "refused"
#: A near miss the person chose not to ask for.
LEFT = "left"
#: A ticked near miss on its way to being asked for, claimed so a second press
#: of the button cannot pick it up again.
SENDING = "sending"

#: One catalogue search at a time, whoever's list it is for. Every row is a
#: search against somebody else's rate limit, and two lists at full speed is
#: the way to find out where that limit is. Taken per row rather than per list,
#: so two lists take turns instead of a long one holding the other for an hour.
_turn = threading.Semaphore(1)


# --------------------------------------------------------------------------
# Reading the file
# --------------------------------------------------------------------------

#: Column headings this recognises, folded to letters and digits first, so
#: "Track Name", "track_name" and "TRACKNAME" are one entry. Headings are
#: matched by name rather than guessed at by content: a column of release
#: dates and a column of titles look alike to a guess, and the cost of getting
#: it wrong is a file of requests for films called "2019-04-03".
#:
#: `title` is the generic one -- Letterboxd calls a film's column Name and
#: Discogs calls a release's column Title -- and it is used wherever the
#: medium's own heading is absent.
_ROLES = {
    "artist": ("artist", "artists", "artistname", "artistnames", "albumartist",
               "author", "authors", "performer", "performers", "band",
               "composer", "credit", "creator"),
    "album": ("album", "albumname", "albumtitle", "release", "releasetitle",
              "albums"),
    "track": ("track", "trackname", "tracktitle", "song", "songname",
              "songtitle", "recording", "tracks", "songs"),
    # The words a person heads their own list with come after the ones an
    # export writes. "Series" is last because a book export uses it for the
    # series a book belongs to, next to a Title column that should win.
    "title": ("title", "name", "booktitle", "filmtitle", "movietitle", "movie",
              "film", "book", "show", "showname", "tvshow", "tvshowname",
              "seriesname", "podcast", "movies", "films", "books", "shows",
              "podcasts", "series"),
    # Each tuple is in order of preference, and "date" is last in this one on
    # purpose: a Letterboxd export has both a Date column (when the film was
    # logged) and a Year column (when it came out), and taking whichever came
    # first in the file asked for a hundred films made in 2024.
    "year": ("year", "releaseyear", "originalreleaseyear", "released",
             "releasedate", "firstreleased", "published", "date"),
}

#: A single-column file whose first line is one of these has a heading rather
#: than a first entry. Derived from the table above so the two cannot drift.
_HEADING_WORDS = frozenset(name for names in _ROLES.values() for name in names)

_LETTERS = re.compile(r"[^a-z0-9]+")
_YEAR = re.compile(r"(\d{4})")
_ASIDE = re.compile(r"[(\[][^)\]]*[)\]]")
_DIGITS = re.compile(r"\d+")

#: What separates a credit from a title in a list that has no columns at all.
#: " - " with the spaces, because a hyphen inside a title ("Jack-in-the-Box")
#: is ordinary and a spaced one almost never is.
_PLAIN_SPLIT = " - "


def _fold_heading(text: str) -> str:
    return _LETTERS.sub("", text.strip().casefold())


@dataclass(frozen=True, slots=True)
class Sheet:
    """A file, read but not yet understood as any particular medium."""
    #: The heading row as it was written, empty when the file had none.
    headings: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    #: Which line of the file each of those rows ended on, in step with it.
    #: Kept rather than counted later because blank lines are dropped and a
    #: quoted field may hold a newline of its own, so the position of a row in
    #: this list stopped being the line a person can go and look at -- which
    #: is the only thing saying "line 41" is for.
    lines: tuple[int, ...]
    #: role -> column index, for the roles this file turned out to carry.
    roles: dict[str, int]
    delimiter: str

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)


def _decode(data: bytes | str) -> str:
    """Text out of an upload, whatever a spreadsheet saved it as.

    UTF-16 on its byte-order mark, then UTF-8 with the mark Excel writes, then
    Windows-1252, which is what "CSV (Comma delimited)" produces on an English
    Windows and which decodes an accented name as mojibake under UTF-8 rather
    than failing. Last resort replaces the bytes it cannot read: a file that is
    99% legible should import 99% of its rows, not none of them.

    UTF-16 is decided on the mark alone and tried first because it is what
    Excel's "Unicode Text" export writes -- exactly the file a spreadsheet
    person hands over -- and cp1252 below will decode it without complaining
    into every character interleaved with a null. A guess that reached that
    line would produce nonsense instead of an error.
    """
    if isinstance(data, str):
        return data.lstrip("﻿")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _sniff(sample: str) -> str:
    """Which character separates the columns.

    Semicolons because that is what a spreadsheet saves in a locale where the
    comma is the decimal point, and tabs because a column copied out of a
    spreadsheet and pasted into the box arrives tab-separated.
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # One column and no separator at all is the commonest list there is.
        return ","


def _is_heading_row(cells: tuple[str, ...]) -> bool:
    """Whether a first line of several cells is column headings, not data.

    Two different headings this recognises settle it. One is enough only when
    nothing else on the line looks like data: "Title,Rating" is a heading row,
    but "Movie, 1999" and "Film, Film, Film" are the first entries of a list
    that has none, and taking them for headings would drop them unseen.
    """
    known = [name for name in map(_fold_heading, cells) if name in _HEADING_WORDS]
    if len(set(known)) > 1:
        return True
    if len(known) != 1:
        return False
    # A heading is a word. A cell with no letters in it is a year, a rating
    # or a date, which is what a row of data has and a heading row does not.
    return not any(cell.strip() and not any(ch.isalpha() for ch in cell)
                   for cell in cells)


def read(data: bytes | str) -> Sheet:
    """A file as columns and rows. Raises `Unreadable` with a sentence to show.

    The heading row is identified by recognising it, not by counting or by
    `csv.Sniffer.has_header`, which decides on shape: a file of two text
    columns looks exactly like its own heading row. A first line that names no
    column this understands is treated as data, so a headingless list of
    titles reads as titles.
    """
    text = _decode(data)
    if not text.strip():
        raise Unreadable("That file is empty.")
    # The csv module refuses a bare carriage return outside quotes, and old
    # Mac files end every line with one.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    delimiter = _sniff(text[:4096])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    parsed: list[tuple[int, tuple[str, ...]]] = []
    ended = 0
    try:
        for record in reader:
            ended = reader.line_num
            cells = tuple(cell.strip() for cell in record)
            if any(cells):
                # `line_num` is the physical line the record ended on, which
                # is the one a person counting down their file will land on.
                parsed.append((ended, cells))
    except csv.Error as exc:
        # An opening quote that never closes swallows the rest of the file
        # into one field, and past the field size limit that is an error
        # rather than a very long title. The field began on the line after
        # the last record that ended.
        raise Unreadable(
            f"Could not read that file from line {ended + 1} on. That line "
            f"may have a quotation mark that is never closed.") from exc
    if not parsed:
        raise Unreadable("That file has no rows in it.")

    first = parsed[0][1]
    if len(first) == 1 or not _is_heading_row(first):
        # A list with no heading row is one title per line, commas and all:
        # "Crouching Tiger, Hidden Dragon" is one film, not two columns.
        # A quoted CSV field is decoded only when it encloses the whole line.
        parsed = []
        for line, raw in enumerate(text.splitlines(), start=1):
            value = raw.strip()
            if value:
                if value.startswith('"') and value.endswith('"'):
                    decoded = next(csv.reader([value]))
                    if len(decoded) == 1:
                        value = decoded[0]
                parsed.append((line, (value,)))
        first = parsed[0][1]
    folded = [_fold_heading(cell) for cell in first]
    roles: dict[str, int] = {}
    for role, names in _ROLES.items():
        # The better name wins over the earlier column. Scanning the columns
        # first would let a Date column two places to the left of a Year
        # column claim the year, which is how a film logged in 2024 comes to
        # be asked for as a film made in 2024.
        for name in names:
            found = [index for index, cell in enumerate(folded) if cell == name]
            if found:
                roles[role] = found[0]
                break
    # A single column whose one heading is a word from the table above is a
    # heading even though the loop above has already claimed it as a role --
    # both facts are true, and the row must still be dropped from the data.
    heading_row = bool(roles) or (len(first) == 1 and folded[0] in _HEADING_WORDS)
    if heading_row:
        headings, body = first, parsed[1:]
    else:
        headings, body = (), parsed
    if not body:
        raise Unreadable("That file has headings but no rows under them.")
    return Sheet(tuple(headings), tuple(cells for _, cells in body),
                 tuple(line for line, _ in body), roles, delimiter)


@dataclass(frozen=True, slots=True)
class Row:
    """One line of somebody's list, as a thing that could be asked for."""
    #: Which line of the file this was, counting the heading. For saying which
    #: row a message is about, in the only terms the sender can check.
    line: int
    title: str
    artist: str = ""
    year: str = ""

    @property
    def label(self) -> str:
        """The row as one readable phrase, for a list a screen reader reads."""
        parts = self.title
        if self.artist:
            parts += f" by {self.artist}"
        if self.year:
            parts += f" ({self.year})"
        return parts


#: Which column holds the thing being asked for, per medium and unit, best
#: first. Music is the medium with three units and therefore three answers;
#: everything else takes the generic heading and falls back to the one a
#: music export would have used, because "Name" and "Title" are shared.
_TITLE_ROLES = {
    "artist": ("artist",),
    "album": ("album", "title", "track"),
    "track": ("track", "title", "album"),
}
_DEFAULT_TITLE_ROLES = ("title", "track", "album")


def suggest_unit(sheet: Sheet) -> str:
    """Which of music's three units a file looks like a list of.

    A suggestion for the form to preselect, never a decision: a column called
    Title says nothing about whether its rows are albums or songs, and the
    person who brought the file knows.
    """
    if "track" in sheet.roles:
        return "track"
    if "album" in sheet.roles:
        return "album"
    if "artist" in sheet.roles and "title" not in sheet.roles:
        return "artist"
    return "album"


def _year_of(cell: str) -> str:
    """The year in a cell that may be a year, a date, or a date and a time."""
    found = _YEAR.search(cell)
    return found.group(1) if found else ""


def rows(sheet: Sheet, medium: str, unit: str) -> tuple[list[Row], int, int]:
    """The sheet as things to ask for: the rows, duplicates, and blanks.

    Duplicates are dropped rather than asked for twice. `wants.want` is free
    to repeat, so asking twice would cost nothing -- but a report that says
    "asked for 400" when the file held 120 distinct albums is a report nobody
    can check, and a music track's ledger key is a digest of its credit and
    title, so the second one was never going to be a second request anyway.
    """
    order = _TITLE_ROLES.get(unit, _DEFAULT_TITLE_ROLES) if medium == media.MUSIC \
        else _DEFAULT_TITLE_ROLES
    title_at = next((sheet.roles[role] for role in order if role in sheet.roles),
                    None)
    if title_at is None:
        if sheet.width == 1:
            title_at = 0
        else:
            raise Unreadable(
                "Could not tell which column has the titles. Give it the "
                "heading " + _understood(order if medium == media.MUSIC
                                         else ("title",))
                + ", or use a file with only one column.")
    # An artist column is not used for the artist unit: there the credit *is*
    # the title, and reading both would ask for "Bruce Springsteen by Bruce
    # Springsteen".
    artist_at = sheet.roles.get("artist") if unit != "artist" else None
    if artist_at == title_at:
        artist_at = None
    year_at = sheet.roles.get("year")

    found: list[Row] = []
    seen: set[tuple[str, str, str]] = set()
    duplicates = 0
    blanks = 0
    for index, row in enumerate(sheet.rows):
        def cell(at: int | None) -> str:
            return row[at].strip() if at is not None and at < len(row) else ""

        title = cell(title_at)
        artist = cell(artist_at)
        if not title:
            blanks += 1
            continue
        # A list with no columns writes the credit into the title: "Bill
        # Withers - Lovely Day". Only split where there is no artist column to
        # contradict, never for a unit that has no credit of its own, and
        # never outside music -- a film's title is allowed to contain " - "
        # and nothing else is hiding in front of it. Without that last
        # condition "Mission: Impossible - Dead Reckoning Part One" is asked
        # for as a film called "Dead Reckoning Part One" by somebody called
        # "Mission: Impossible", and films are matched without a credit, so a
        # same-named hit would tick itself.
        if (medium == media.MUSIC and not artist and unit != "artist"
                and _PLAIN_SPLIT in title):
            artist, _, title = title.partition(_PLAIN_SPLIT)
            artist, title = artist.strip(), title.strip()
        year = _year_of(cell(year_at))
        # The year is part of what makes two rows different, not decoration on
        # one of them: a film list holding Dune 1984 and Dune 2021 is two
        # films, and a key without the year keeps one of them and reports the
        # other as a duplicate somebody never sees again.
        key = (_key(artist), _key(title), year)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        found.append(Row(sheet.lines[index], title, artist,
                         _year_of(cell(year_at))))
    return found, duplicates, blanks


#: How a role's heading is written when telling somebody what to call it.
#: The folded names above ("artistnames") are for matching, not for reading.
_SHOWN = {"artist": ("Artist",), "album": ("Album",), "track": ("Track",),
          "title": ("Title", "Name")}


def _understood(order: tuple[str, ...]) -> str:
    """The headings that would have worked, as an English list."""
    names = [name for role in order for name in _SHOWN[role]]
    quoted = [f"“{name}”" for name in dict.fromkeys(names)]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


# --------------------------------------------------------------------------
# Deciding what a row is
# --------------------------------------------------------------------------

def _key(text: str) -> str:
    """A title reduced to what two spellings of it have in common.

    Case, accents, punctuation and a parenthesised aside all go: "Peaches (feat.
    Daniel Caesar)" and "Peaches" are the same song, and an import that
    insisted otherwise would leave every remaster and every explicit-content
    tag for a person to tick by hand.

    Nothing else goes. A leading "The", a subtitle after a colon and a roman
    numeral all stay, because dropping them is how "First Line of Defense"
    comes to match "First Line of Defense: Book 2" -- and on this side of the
    service that mistake is a download of the wrong book rather than a
    suggestion somebody can ignore.
    """
    plain = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    plain = _ASIDE.sub(" ", plain.casefold()).replace("&", " and ")
    return " ".join(_LETTERS.sub(" ", plain).split())


def _numbers(text: str) -> set[str]:
    """Every number in a title, leading zeros levelled.

    Read after the parenthesised asides have gone, so a remaster's year is not
    one of them and a volume number is.
    """
    plain = _ASIDE.sub(" ", text.casefold())
    return {found.lstrip("0") or "0" for found in _DIGITS.findall(plain)}


def is_strict(row: Row, hit: dict, medium: str) -> bool:
    """Whether a catalogue hit is certainly the row, rather than plausibly it.

    Deliberately narrow. A row this refuses is still shown, still says what it
    nearly matched, and still has a tick box -- so the cost of refusing a true
    match is one tick, and the cost of accepting a false one is a download
    nobody wanted and an allowance spent on it.
    """
    if _key(row.title) != _key(hit.get("title") or ""):
        return False
    if _numbers(row.title) != _numbers(hit.get("title") or ""):
        return False
    credit = (hit.get("artist") or "").strip()
    if medium in (media.MUSIC, media.BOOK) and hit.get("unit") != "artist":
        # No credit in the file means no way to tell this artist's "Home" from
        # the other nine. A person can still tick it.
        if not row.artist or not credit:
            return False
        if _key(row.artist) != _key(credit):
            return False
    hit_year = str(hit.get("year") or "").strip()
    if row.year and hit_year and row.year != hit_year:
        return False
    return True


def _query(row: Row, medium: str, unit: str) -> str:
    """What to ask the catalogue for this row.

    The credit goes into the query for music and books, where a title alone
    reaches the wrong Home and the wrong Dune, and stays out of it for films
    and series, whose catalogues match a title and treat extra words as a
    title that does not exist.
    """
    if medium in (media.MUSIC, media.BOOK) and row.artist and unit != "artist":
        return f"{row.artist} {row.title}".strip()
    return row.title


#: The catalogue a music row is looked up in first, and the units that do it.
#: Deezer answers in about a third of a second; asking all three of buskarr's
#: catalogues took two seconds a row, most of it MusicBrainz, which also allows
#: only one query a second. Only a row Deezer has nothing certain for goes on to
#: the rest. Artists are left out: MusicBrainz leads an artist search because
#: it is the one that tells two acts of the same name apart.
FIRST_LOOK = ("deezer",)
FIRST_LOOK_UNITS = ("track", "album")


def match(user: jellyfin.User, medium: str, unit: str, row: Row,
          requested: set[str]) -> dict:
    """One row, looked up and judged. Never raises: a row that failed says so.

    A search that throws is one row's problem. Five hundred rows against three
    third-party catalogues will find a timeout somewhere, and losing the whole
    file to it -- after the other four hundred and ninety-nine have already
    been paid for -- is the wrong answer to a network blip.
    """
    found: dict = {"line": row.line, "title": row.title, "artist": row.artist,
                   "year": row.year, "label": row.label, "state": MISSING,
                   "detail": "", "hit": None, "others": 0}
    query = _query(row, medium, unit)
    try:
        hits = []
        if medium == media.MUSIC and unit in FIRST_LOOK_UNITS:
            first = wants.search(query, medium, unit, user, sources=FIRST_LOOK)
            if any(is_strict(row, hit, medium) for hit in first):
                hits = first
        if not hits:
            hits = wants.search(query, medium, unit, user)
    except Exception as exc:  # noqa: BLE001 - see the docstring.
        log.warning("import row %d could not be looked up: %s", row.line, exc)
        found["detail"] = "The search for this one failed. Try it again later."
        return found
    if not hits:
        found["detail"] = NO_MATCH
        return found

    exact = [hit for hit in hits if is_strict(row, hit, medium)]
    chosen = exact[0] if exact else hits[0]
    found["hit"] = _keep(chosen)
    found["others"] = len(hits) - 1
    if chosen.get("owned"):
        found["state"] = HELD
        found["detail"] = "Already in the library."
        return found
    if chosen.get("requested") or chosen.get("itemKey") in requested:
        found["state"] = HELD
        found["detail"] = "Already asked for."
        return found
    if not exact:
        found["state"] = UNCERTAIN
        found["detail"] = "Closest result, not an exact match."
        return found
    found["state"] = MATCHED
    found["detail"] = (f"{len(exact)} exact matches. This asks for the first."
                       if len(exact) > 1 else "")
    return found


#: What of a search hit is kept in the batch. Everything `wants.want` reads
#: back out of a hit, plus what the review page shows. Duration especially:
#: buskarr's matcher reads an absent duration as "cannot judge" and will
#: accept a file of any length, so dropping it here would turn a thirty-second
#: track into a request that any two-second file satisfies.
_KEPT = ("itemKey", "medium", "unit", "title", "year", "artist", "album",
         "source", "ref", "durationSeconds", "overview", "authors", "owned", "requested",
         "imageUrl", "thumbnailUrl")


def _keep(hit: dict) -> dict:
    return {name: hit[name] for name in _KEPT if hit.get(name) is not None}


def hit_label(hit: dict) -> str:
    """A catalogue hit as one readable phrase."""
    label = hit.get("title") or ""
    if hit.get("artist") and hit.get("unit") != "artist":
        label += f" by {hit['artist']}"
    if hit.get("year"):
        label += f" ({hit['year']})"
    return label


# --------------------------------------------------------------------------
# A batch, from upload to report
# --------------------------------------------------------------------------

def max_rows(medium: str) -> int:
    """The most rows one list of this medium may hold."""
    return (config.IMPORT_MAX_ROWS_MUSIC if medium == media.MUSIC
            else config.IMPORT_MAX_ROWS)


def start(user: jellyfin.User, medium: str, unit: str, filename: str,
          data: bytes | str, playlist: str = "") -> str:
    """Read a file, start working through it, and return the batch's id.

    The reading is done here, where somebody is waiting and can be told that
    their file has no title column. The looking up and asking -- network calls
    per row -- go to a thread.

    `playlist` names a Jellyfin playlist of the person's to put the songs in,
    made if need be (see `playlists`). Only a list of songs can be one.
    """
    sheet: Sheet | None = None
    try:
        found = media.get(medium)
        if found is None:
            raise Unreadable("This server does not take requests for that.")
        if len(data) > config.IMPORT_MAX_BYTES:
            raise Unreadable(
                f"That file is too big. The limit is "
                f"{config.IMPORT_MAX_BYTES // 1000:,} kB.")
        sheet = read(data)
        # An unstated unit is worked out from the headings, which is the
        # default the form offers: a file with a Track Name column is a list
        # of songs and saying so twice is a chance to disagree with yourself.
        if unit not in found.units:
            unit = (suggest_unit(sheet) if medium == media.MUSIC
                    else found.units[0])
        items, duplicates, blanks = rows(sheet, medium, unit)
        if not items:
            raise Unreadable(
                "Every row in that file has an empty title column.")
        if len(items) > max_rows(medium):
            raise Unreadable(
                f"That file has {len(items):,} rows. The limit is "
                f"{max_rows(medium):,} per file, so split it into "
                f"smaller files.")
        playlist_id = ""
        if playlists.clean_name(playlist):
            if medium != media.MUSIC or unit != "track":
                raise Unreadable(
                    "A playlist is made of songs, and this list is not read as "
                    "songs. For music, choose “A track”, or leave the playlist "
                    "name empty.")
            try:
                playlist_id = playlists.resolve(user, playlist)
            except playlists.Refused as refused:
                raise Unreadable(str(refused)) from refused
            except jellyfin.JellyfinUnavailable as exc:
                raise Unreadable("Jellyfin could not be reached to make the "
                                 "playlist. Try again in a moment.") from exc
    except Unreadable as exc:
        # The person is told why on the page. This puts the reason, and the
        # headings the file had, where a report of "importing does not work"
        # can be checked afterwards.
        log.info("import refused user=%s medium=%s file=%r headings=%r: %s",
                 user.key, medium, filename,
                 list(sheet.headings) if sheet else None, exc)
        raise

    import_id = uuid.uuid4().hex
    payload = {
        "medium": medium, "unit": unit, "filename": filename,
        "duplicates": duplicates, "blanks": blanks,
        "headings": list(sheet.headings),
        # The parsed rows, so a list a restart interrupted carries on from
        # where it got to instead of being looked up again from the top.
        "items": [{"line": r.line, "title": r.title, "artist": r.artist,
                   "year": r.year} for r in items],
    }
    if playlist_id:
        payload["playlist"] = {"name": playlists.clean_name(playlist),
                               "id": playlist_id}
    store.prune_imports(time.time() - config.IMPORT_RETENTION_HOURS * 3600)
    store.put_import(import_id, user.key, medium, READING, len(items), payload)
    log.info("import %s started user=%s medium=%s unit=%s rows=%d file=%r",
             import_id, user.key, medium, unit, len(items), filename)
    _start_working(import_id, user, medium, unit, items)
    return import_id


#: Lists a thread in this process is working through. One that says it is
#: reading and is not in here was interrupted by a restart.
_working: set[str] = set()
#: Lists somebody has stopped, checked by their thread before each row.
_stopping: set[str] = set()
_working_guard = threading.Lock()


def _start_working(import_id: str, user: jellyfin.User, medium: str, unit: str,
                   items: list[Row]) -> None:
    with _working_guard:
        if import_id in _working:
            return
        _working.add(import_id)
    try:
        threading.Thread(target=_work_through, name=f"import-{import_id[:8]}",
                         args=(import_id, user, medium, unit, items),
                         daemon=True).start()
    except Exception:
        # Left marked, nothing would ever pick this list up again.
        with _working_guard:
            _working.discard(import_id)
        raise


def _work_through(import_id: str, user: jellyfin.User, medium: str, unit: str,
                  items: list[Row]) -> None:
    """Look up every row, and ask for each exact match as soon as it is found.

    A list of songs is checked against the library first, so a song already
    here is not asked for again; with a playlist, those go straight into it,
    a few at a time in the order of the file, and every other song the list
    asks for is waited for and put in when it turns up.
    """
    playlist = _playlist_of(import_id)
    found_now: list[tuple[int, str]] = []
    try:
        requested = store.outstanding_keys(user.key, medium)
        library = _library_for(medium, unit)
        done = len(store.import_row_lines(import_id))
        for index, row in enumerate(items, start=1):
            if import_id in _stopping:
                log.info("import %s stopped after %d rows", import_id, done)
                break
            item_id = library.find(row.title, row.artist) if library else None
            if item_id:
                store.put_import_row(import_id, _in_library(row, item_id))
                done += 1
                store.touch_import(import_id, done)
                if playlist:
                    found_now.append((row.line, item_id))
                    if len(found_now) >= PLAYLIST_BATCH:
                        _add_found(user, playlist, import_id, found_now)
                        found_now = []
                continue
            with _turn:
                found = match(user, medium, unit, row, requested)
            outcome, detail = (_ask_one(import_id, user, medium, found)
                               if found["state"] == MATCHED else ("", ""))
            if outcome is None:
                log.info("import %s stopped after %d rows", import_id, done)
                break
            store.put_import_row(import_id, found, outcome, detail)
            if playlist:
                _wait_for(user, playlist, import_id, found, outcome)
            done += 1
            store.touch_import(import_id, done)
            if config.IMPORT_PAUSE_SECONDS and index < len(items):
                # These searches land on third-party catalogues, and the way
                # to find their rate limit is to send five thousand queries at
                # machine speed.
                time.sleep(config.IMPORT_PAUSE_SECONDS)
        if playlist:
            _add_found(user, playlist, import_id, found_now)
            found_now = []
    except Exception as exc:  # noqa: BLE001 - the thread must not die
        # silently, or the page waits for a phase that has stopped.
        log.exception("import %s failed while reading: %s", import_id, exc)
        if playlist and found_now:
            # Already marked as in the library, so nothing else would ever
            # put them in the playlist.
            _add_found(user, playlist, import_id, found_now)
        _close(import_id, FAILED, {
            "error": "Something went wrong while looking these up. Anything "
                     "asked for before it stopped is listed below."})
        return
    finally:
        with _working_guard:
            _working.discard(import_id)
            _stopping.discard(import_id)
    _settle(import_id)
    log.info("import %s read: %s", import_id,
             _tally(store.import_rows(import_id)))


#: Songs found in the library that go into a playlist in one call. A few at a
#: time rather than one by one, so a long list is not a call per song, and not
#: all at the end, so the playlist fills while the rest is looked up.
PLAYLIST_BATCH = 25


def _playlist_of(import_id: str) -> dict | None:
    row = store.get_import(import_id)
    return json.loads(row["payload"]).get("playlist") if row else None


def _library_for(medium: str, unit: str) -> songs.LibraryIndex | None:
    """The music library's songs, for a list of songs. None otherwise, or if unreadable."""
    if medium != media.MUSIC or unit != "track":
        return None
    try:
        return songs.LibraryIndex.load()
    except jellyfin.JellyfinUnavailable as exc:
        log.warning("library could not be read, so every song is looked up: %s", exc)
        return None


def _in_library(row: Row, item_id: str) -> dict:
    """A row whose song the library already holds."""
    return {"line": row.line, "title": row.title, "artist": row.artist,
            "year": row.year, "label": row.label, "state": HELD,
            "detail": "Already in the library.", "hit": None, "others": 0,
            "itemId": item_id}


def _add_found(user: jellyfin.User, playlist: dict, import_id: str,
               found: list[tuple[int, str]]) -> None:
    """Put library songs into the playlist; if that fails, wait for them instead."""
    if not found:
        return
    try:
        playlists.add_found(user, playlist["id"], import_id, found)
    except (jellyfin.JellyfinUnavailable, playlists.Refused) as exc:
        log.warning("import %s could not add %d song(s) to its playlist: %s",
                    import_id, len(found), exc)
        by_line = {row["line"]: row for row in store.import_rows(import_id)}
        for line, item_id in found:
            row = by_line.get(line) or {}
            playlists.wait_for(user, playlist["id"], import_id, line,
                               row.get("title") or "", row.get("artist") or "",
                               item_id=item_id)


def _wait_for(user: jellyfin.User, playlist: dict, import_id: str, row: dict,
              outcome: str) -> None:
    """Wait for a song of a playlist that is on its way, to put it in later."""
    on_its_way = (outcome in (ASKED, WAITING, ALREADY)
                  or (not outcome and row["state"] == HELD and row.get("hit")))
    if not on_its_way:
        return
    hit = row.get("hit") or {}
    playlists.wait_for(user, playlist["id"], import_id, row["line"],
                       hit.get("title") or row["title"],
                       hit.get("artist") or row["artist"],
                       hit.get("album") or "")


def _ask_one(import_id: str, user: jellyfin.User, medium: str,
             row: dict) -> tuple[str | None, str]:
    """Ask for one row's match. Returns its outcome and, for a refusal, why.

    The outcome is None for a list that has been stopped: the row is not
    asked for. That is decided under the same lock clearing the queue takes,
    so a row cannot be queued after somebody has stopped the list and emptied
    the queue.

    Music that does not fit in the day's import allowance, or that buskarr is
    not answering for, waits in this account's queue and goes in on a later
    pass. So does every row after the first one that waits: while anything is
    queued, a row goes in behind it, never ahead of it. Asks for one account
    are made one at a time, so two of its lists cannot overtake each other.
    """
    hit = row["hit"]
    label = hit_label(hit)
    with ask_lock(user):
        with _working_guard:
            if import_id in _stopping:
                return None, ""
        if medium == media.MUSIC and store.queued_count(user.key, medium):
            store.queue_rows(user.key, medium, import_id, [(row["line"], label, hit)])
            return WAITING, ""
        try:
            state, message = wants.want(
                user, medium, hit.get("itemKey", ""), hit.get("unit", ""),
                dict(hit), imported=True)
        except (wants.ImportLimitReached, wants.TryLater) as later:
            if medium != media.MUSIC:
                return REFUSED, str(later)
            store.queue_rows(user.key, medium, import_id,
                             [(row["line"], label, hit)])
            return WAITING, ""
        except wants.Denied as denied:
            return REFUSED, str(denied)
        except Exception as exc:  # noqa: BLE001 - one row's failure is not
            # the list's, and this runs where nobody is watching.
            log.warning("import %s could not ask for %r: %s", import_id, label, exc)
            return REFUSED, "Something went wrong."
    log.info("import %s asked for %r state=%s", import_id, label, state)
    return (ALREADY if state == wants.IN_LIBRARY or message.startswith("Already ")
            else ASKED), ""


def ask_lock(user: jellyfin.User):
    """Held while one of this account's rows is being asked for."""
    return store.key_lock("import-ask", user.key)


def _offerable(row: dict) -> bool:
    """Whether a row is still waiting for somebody to decide about it."""
    hit = row.get("hit") or {}
    return (row["state"] in (MATCHED, UNCERTAIN) and bool(hit)
            and not row.get("outcome")
            and not hit.get("owned") and not hit.get("requested"))


def _settle(import_id: str) -> None:
    """Once every row is looked up: ready while near misses wait, else done."""
    with store.key_lock("import", import_id):
        current = store.get_import(import_id)
        if current is None or current["state"] not in (READING, READY):
            return
        # A ticked row still being asked for is not decided yet: the ask
        # settles the list itself when it is done.
        undecided = any(_offerable(row) or row.get("outcome") == SENDING
                        for row in store.import_rows(import_id))
        _close(import_id, READY if undecided else DONE, {})


def _tally(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        key = row.get("outcome") or row["state"]
        counts[key] = counts.get(key, 0) + 1
    return " ".join(f"{state}={count}" for state, count in sorted(counts.items()))


def _close(import_id: str, state: str, changes: dict) -> None:
    """Move a batch on, with whatever it worked out, keeping the rest."""
    row = store.get_import(import_id)
    if row is None:
        log.warning("import %s vanished before it finished", import_id)
        return
    payload = json.loads(row["payload"])
    payload.update(changes)
    store.close_import(import_id, state, payload)


def get(user: jellyfin.User, import_id: str) -> dict | None:
    """One batch belonging to this account, or None.

    Not found and belonging to somebody else are the same answer on purpose.
    A batch id is a random 128 bits, so the only way to ask about one that is
    not yours is to have been given it -- and the reply should not confirm
    that it exists.
    """
    row = store.get_import(import_id)
    if row is None or row["user_key"] != user.key:
        return None
    payload = json.loads(row["payload"])
    state = row["state"]
    stale = time.time() - row["touched_at"] > config.IMPORT_STALE_SECONDS
    # A list still reading picks up after a restart by itself (see
    # `resume_interrupted`), unless it is from before it kept its rows.
    resumable = state == READING and payload.get("items")
    if state in (READING, ASKING) and stale and not resumable:
        log.warning("import %s was %s and has not moved; calling it failed",
                    import_id, state)
        # Written before it is stored, not after: the page reads `error` out
        # of the payload, so setting it on the way out would explain this once
        # and then show a blank reason on every load afterwards.
        payload["error"] = ("This stopped partway through, probably because "
                            "the server restarted. Nothing more was asked "
                            "for.")
        store.close_import(import_id, FAILED, payload)
        state = FAILED
    payload.pop("items", None)
    rows_now = store.import_rows(import_id) or payload.pop("rows", [])
    payload.pop("rows", None)
    return {"id": import_id, "state": state, "done": row["done"],
            "total": row["total"], "created_at": row["created_at"],
            **payload, "rows": rows_now}


def groups(batch: dict) -> dict[str, list[dict]]:
    """The batch's rows, gathered by what the lookup found for each."""
    out: dict[str, list[dict]] = {MATCHED: [], UNCERTAIN: [], HELD: [],
                                  MISSING: []}
    for row in batch.get("rows", []):
        out.setdefault(row["state"], []).append(row)
    return out


def view(batch: dict) -> dict[str, list[dict]]:
    """The batch's rows gathered by what became of them, for a page to show.

    `close` is the near misses still waiting for somebody to tick or leave.
    """
    out: dict[str, list[dict]] = {"asked": [], "already": [], "waiting": [],
                                  "refused": [], "close": [], "left": [],
                                  "missing": []}
    for row in batch.get("rows", []):
        outcome = row.get("outcome") or ""
        if outcome in (ASKED, SENDING):
            out["asked"].append(row)
        elif outcome == WAITING:
            out["waiting"].append(row)
        elif outcome == REFUSED:
            out["refused"].append(row)
        elif outcome == LEFT:
            out["left"].append(row)
        elif row["state"] == MISSING:
            out["missing"].append(row)
        elif _offerable(row):
            out["close"].append(row)
        else:
            out["already"].append(row)
    return out


def report(batch: dict) -> dict[str, list[str]]:
    """What became of the rows that were acted on, a line each."""
    seen = view(batch)

    def said(row: dict) -> str:
        return hit_label(row["hit"]) if row.get("hit") else row["label"]

    return {"asked": [said(row) for row in seen["asked"]],
            "already": [said(row) for row in seen["already"]],
            "waiting": [said(row) for row in seen["waiting"]],
            "refused": [f"{said(row)}: {row.get('outcomeDetail') or 'refused.'}"
                        for row in seen["refused"]]}


def ask_for_lines(user: jellyfin.User, import_id: str, lines: set[int],
                  final: bool = False) -> dict | None:
    """Ask for near misses somebody ticked. Returns the batch, or None.

    Ticks are checked against the batch rather than trusted: a posted line that
    was never offered, or has already been dealt with, is dropped here. The
    form cannot offer those, which means a request for one did not come from
    the form.

    `final` is a client that confirms once, the way the two-step importer
    worked: the list moves to asking and then ends, and whatever it did not
    tick is left. Otherwise it can be called as often as somebody likes,
    while the lookup is still running too, and the list ends by itself once
    every row is looked up and nothing is left to decide.
    """
    # Read and moved on under one lock. Without it a double-tap on the button
    # -- or a client retrying a request it thought had timed out -- could ask
    # for the same rows twice.
    with store.key_lock("import", import_id):
        batch = get(user, import_id)
        if batch is None:
            return None
        if batch["state"] not in ((READY,) if final else (READING, READY)):
            return batch
        chosen = [row for row in batch["rows"]
                  if row["line"] in lines and _offerable(row)]
        if final and not chosen:
            # Confirming nothing is the common case now that exact matches
            # are asked for as they are found: it leaves the near misses and
            # ends the list.
            store.leave_undecided(import_id)
            _close(import_id, DONE, {})
            return get(user, import_id)
        if not chosen:
            return batch
        if final:
            _close(import_id, ASKING, {"chosen": len(chosen)})
            store.touch_import(import_id, 0)
        else:
            # Claimed before the thread starts, so a second press cannot pick
            # the same rows up again while the first is still asking.
            for row in chosen:
                store.set_import_outcome(import_id, row["line"], SENDING)
    log.info("import %s asking for %d ticked row(s) user=%s final=%s",
             import_id, len(chosen), user.key, final)
    threading.Thread(target=_ask_chosen, name=f"import-ask-{import_id[:8]}",
                     args=(import_id, user, batch["medium"], chosen, final),
                     daemon=True).start()
    return get(user, import_id)


def _ask_chosen(import_id: str, user: jellyfin.User, medium: str,
                chosen: list[dict], final: bool) -> None:
    """Ask for each ticked row, and write down what became of it."""
    try:
        playlist = _playlist_of(import_id)
        for index, row in enumerate(chosen, start=1):
            outcome, detail = _ask_one(import_id, user, medium, row)
            # A list stopped while this was on its way: the row is offered
            # again rather than left claimed.
            store.set_import_outcome(import_id, row["line"], outcome or "",
                                     detail)
            if playlist and outcome:
                _wait_for(user, playlist, import_id, row, outcome)
            if final:
                store.touch_import(import_id, index)
    except Exception as exc:  # noqa: BLE001 - the thread must not die silently.
        log.exception("import %s failed while asking: %s", import_id, exc)
    if final:
        store.leave_undecided(import_id)
        _close(import_id, DONE, {})
    else:
        _settle_if_read(import_id)


def _settle_if_read(import_id: str) -> None:
    """End a list whose lookup is over and that has nothing left to decide."""
    current = store.get_import(import_id)
    if current is not None and current["state"] == READY:
        _settle(import_id)


def recent(user: jellyfin.User) -> list[dict]:
    """This account's lists still on the server, newest first, for a way back to them."""
    found = []
    for row in store.imports_for(user.key):
        payload = json.loads(row["payload"])
        found.append({"id": row["import_id"], "state": row["state"],
                      "done": row["done"], "total": row["total"],
                      "medium": row["medium"],
                      "filename": payload.get("filename") or ""})
    return found


def resume_interrupted() -> int:
    """Carry on with lists a restart stopped part way. Returns how many.

    A list that says it is reading, has no thread working on it here, and has
    not moved for a while was interrupted. Its parsed rows were kept, and the
    rows already done are in the table, so the rest is looked up from where it
    stopped. The account is resolved through Jellyfin, so it carries its real
    administrator flag; a list whose account is gone, or from before rows were
    kept, is marked failed rather than left reading for ever.
    """
    now = time.time()
    with _working_guard:
        working = set(_working)
    stalled = [row for row in store.imports_in_state(READING)
               if row["import_id"] not in working
               and now - row["touched_at"] > RESUME_AFTER_SECONDS]
    if not stalled:
        return 0
    by_id = {uid: name for name, uid in jellyfin.all_users().items()}
    resumed = 0
    for row in stalled:
        payload = json.loads(row["payload"])
        if payload.get("stopped"):
            _settle(row["import_id"])
            continue
        items = payload.get("items")
        name = by_id.get(row["user_key"])
        if not items or not name:
            _close(row["import_id"], FAILED, {
                "error": "This stopped partway through, probably because the "
                         "server restarted, and could not carry on. Anything "
                         "asked for before it stopped is listed below."})
            continue
        done = store.import_row_lines(row["import_id"])
        rest = [Row(item["line"], item["title"], item.get("artist", ""),
                    item.get("year", ""))
                for item in items if item["line"] not in done]
        log.info("import %s carries on after a restart: %d of %d rows left",
                 row["import_id"], len(rest), len(items))
        _start_working(row["import_id"], jellyfin.user(name), row["medium"],
                       payload.get("unit") or "", rest)
        resumed += 1
    return resumed


#: How long a reading list may go without moving before it is taken to have
#: been interrupted. A working list moves every few seconds, a row at a time.
RESUME_AFTER_SECONDS = 120


# --------------------------------------------------------------------------
# The queue of imported music waiting for a later day
# --------------------------------------------------------------------------

#: Waited out before the first pass. A container that has just started is
#: competing with its own first requests, and Jellyfin may not be up yet.
QUEUE_FIRST_DELAY_SECONDS = 120


def queue_status(user: jellyfin.User, medium: str = media.MUSIC) -> dict | None:
    """What this account has waiting, for a page or a client to show. None if nothing.

    `songs` counts an album and an artist at what they cost the allowance, so
    `days` is how long the queue takes at today's rate, give or take the day
    already under way.
    """
    rows = store.queued(user.key, medium)
    if not rows:
        return None
    songs = sum(wants.import_cost(json.loads(row["hit"]).get("unit") or "")
                for row in rows)
    per_day = config.IMPORT_MUSIC_DAILY_SONGS
    return {"waiting": len(rows), "songs": songs, "perDay": per_day,
            "days": -(-songs // per_day) if per_day else None,
            "leftToday": wants.import_allowance(user, medium),
            "nextAt": wants.import_frees_at(user, medium)}


def release_queue() -> int:
    """Ask for whatever waiting rows today's import allowances now cover.

    Accounts are resolved through Jellyfin, so a `User` here carries its real
    administrator flag. Returns how many rows went in.
    """
    owners = store.queue_owners()
    if not owners:
        return 0
    by_id = {uid: name for name, uid in jellyfin.all_users().items()}
    went = 0
    for key in owners:
        name = by_id.get(key)
        if not name:
            dropped = store.clear_queue(key)
            log.info("import queue: account %s is gone, dropped %d rows",
                     key, dropped)
            continue
        went += _release_for(jellyfin.user(name))
    return went


def queue_lock(user: jellyfin.User):
    """Held while one of this account's waiting rows is being asked for.

    Clearing the queue takes it too, so "stop" cannot land between a row being
    asked for and being taken out of the queue.
    """
    return store.key_lock("import-queue", user.key)


def clear_queue(user: jellyfin.User) -> tuple[int, int]:
    """Drop everything this account has waiting. Returns rows dropped and lists stopped.

    A music list still being looked up would only refill the queue a row
    later, so those are stopped first.
    """
    stopped = sum(1 for row in store.imports_for(user.key, limit=1000)
                  if row["state"] == READING and row["medium"] == media.MUSIC
                  and stop_list(user, row["import_id"]))
    # The ask lock too, so a row a stopped list was in the middle of asking
    # for is queued before this empties the queue, not after.
    with ask_lock(user), queue_lock(user):
        dropped = store.clear_queue(user.key)
    log.info("import queue cleared user=%s rows=%d lists_stopped=%d",
             user.key, dropped, stopped)
    return dropped, stopped


def stop_list(user: jellyfin.User, import_id: str) -> bool:
    """Stop looking up a list. What it already asked for stays asked for.

    True when there was a lookup to stop. The flag is written down as well as
    held here, so a restart does not carry on with a list somebody stopped.
    """
    with store.key_lock("import", import_id):
        batch = get(user, import_id)
        if batch is None or batch["state"] != READING:
            return False
        _close(import_id, READING, {"stopped": True})
        with _working_guard:
            _stopping.add(import_id)
            running = import_id in _working
    log.info("import %s stop asked user=%s", import_id, user.key)
    if not running:
        # Nothing here is working on it to see the mark: settle it now.
        with _working_guard:
            _stopping.discard(import_id)
        _settle(import_id)
    return True


def _release_for(user: jellyfin.User) -> int:
    """One account's waiting rows, oldest first, until the allowance runs out.

    A row leaves the queue only after it has been asked for or refused. Taken
    out first, a restart in between lost it, and the queue looked empty to a
    list confirmed while its last row was being asked for, which then went
    ahead of it. Asking again after a restart costs nothing: the ledger already
    has the row and `want` answers "Already asked for."
    """
    went = 0
    for row in store.queued(user.key):
        with queue_lock(user):
            if not store.is_queued(row["id"]):
                continue
            hit = json.loads(row["hit"])
            try:
                state, _ = wants.want(user, row["medium"], hit.get("itemKey", ""),
                                      hit.get("unit", ""), dict(hit),
                                      imported=True)
            except (wants.ImportLimitReached, wants.TryLater) as later:
                if isinstance(later, wants.TryLater):
                    log.warning("import queue: buskarr unavailable, trying "
                                "again later user=%s: %s", user.key, later)
                break
            except wants.Denied as denied:
                log.info("import queue: refused user=%s line=%d %r: %s",
                         user.key, row["line"], row["label"], denied)
                store.unqueue(row["id"])
                continue
            except Exception as exc:  # noqa: BLE001 - tried again next pass.
                log.warning("import queue: could not ask user=%s %r: %s",
                            user.key, row["label"], exc)
                break
            store.unqueue(row["id"])
        went += 1
        log.info("import queue: asked user=%s %r state=%s", user.key,
                 row["label"], state)
    return went


def watch() -> None:
    """Start the pass that carries on interrupted lists and empties the queue.

    First, what a previous process left half done: a row it had claimed for
    asking was not necessarily asked for, so it is offered again (asking for
    one that did get through only finds it already asked for), and lists
    kept the way the two-step importer kept them cannot be read as these are.
    """
    released = store.release_unsent()
    dropped = store.drop_legacy_imports()
    if released or dropped:
        log.info("imports after a restart: %d claimed row(s) offered again, "
                 "%d list(s) from the old importer dropped", released, dropped)
    threading.Thread(target=_queue_loop, name="import-queue", daemon=True).start()


def _queue_loop() -> None:
    time.sleep(QUEUE_FIRST_DELAY_SECONDS)
    while True:
        try:
            resumed = resume_interrupted()
            if resumed:
                log.info("imports: carried on with %d interrupted list(s)",
                         resumed)
        except Exception as exc:  # noqa: BLE001 - as below.
            log.warning("could not carry on with interrupted lists: %s", exc)
        try:
            went = release_queue()
            if went:
                log.info("import queue: %d row(s) asked for", went)
        except Exception as exc:  # noqa: BLE001 - the thread must not die, or
            # waiting rows silently stop going in.
            log.warning("import queue pass failed: %s", exc)
        try:
            playlists.resolve_pending()
        except Exception as exc:  # noqa: BLE001 - as above.
            log.warning("could not fill playlists: %s", exc)
        time.sleep(config.IMPORT_QUEUE_SECONDS)
