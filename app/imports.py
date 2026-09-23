"""Bringing in a list somebody already has.

Somebody arrives with a file: the albums on their shelf, a playlist exported
from a streaming service, the films they have collected. This turns that file
into requests -- in two steps, and never in one.

Step one reads the file and matches every row against the catalogue. Step two
asks for the rows a person has ticked, and only happens after somebody has
read what each row was matched to. Everywhere else in this service a request
is one deliberate tap on one named thing; a file of five hundred rows cannot
be deliberate row by row, and a wrong match here is a download rather than a
bad suggestion. So a row that matches exactly is ticked for you, a row that
merely looks plausible is shown unticked beside what it looked like, and
nothing at all is asked for that nobody ticked.

Both steps run in a thread and report how far they have got. Each row costs a
catalogue search, and then, if it was ticked, a call to an acquisition tool;
five hundred of either does not fit inside one page load.

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

from . import config, jellyfin, logs, media, store, wants

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

#: Only one import is matched at a time, whoever started it. Every row is a
#: catalogue search against somebody else's rate limit, and two files at once
#: is the way to find out where that limit is. A batch waiting its turn says
#: it has read nothing yet, which is true.
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
    "album": ("album", "albumname", "albumtitle", "release", "releasetitle"),
    "track": ("track", "trackname", "tracktitle", "song", "songname",
              "songtitle", "recording"),
    "title": ("title", "name", "booktitle", "filmtitle", "movie", "seriesname"),
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
    delimiter = _sniff(text[:4096])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    parsed: list[tuple[int, tuple[str, ...]]] = []
    for record in reader:
        cells = tuple(cell.strip() for cell in record)
        if any(cells):
            # `line_num` is the physical line the record ended on, which is
            # the one a person counting down their file will land on.
            parsed.append((reader.line_num, cells))
    if not parsed:
        raise Unreadable("That file has no rows in it.")

    first = parsed[0][1]
    known = set().union(*_ROLES.values())
    recognised = sum(_fold_heading(cell) in known for cell in first)
    if len(first) == 1 or recognised < 2:
        # Preserve complete physical titles, including commas and semicolons.
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
        raise Unreadable("That file has a heading row and nothing under it.")
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
                "None of the column headings say which column holds the "
                "title. Name one of them " + _understood(order if medium == media.MUSIC else ("title",)) + ", or give "
                "a file with a single column and nothing else in it.")
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


def _understood(order: tuple[str, ...]) -> str:
    """The headings that would have worked, as an English list."""
    names = []
    for role in order:
        names.extend(_ROLES[role][:2])
    quoted = [f"“{name}”" for name in dict.fromkeys(names)]
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
    try:
        hits = wants.search(_query(row, medium, unit), medium, unit, user)
    except Exception as exc:  # noqa: BLE001 - see the docstring.
        log.warning("import row %d could not be looked up: %s", row.line, exc)
        found["detail"] = "The catalogue could not be asked about this one."
        return found
    if not hits:
        found["detail"] = "Nothing in the catalogue matched."
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
        found["detail"] = "The closest thing in the catalogue, which is not " \
                          "quite what the file says."
        return found
    found["state"] = MATCHED
    found["detail"] = (f"{len(exact)} catalogue entries match this exactly; "
                       "the first is the one ticked." if len(exact) > 1 else "")
    return found


#: What of a search hit is kept in the batch. Everything `wants.want` reads
#: back out of a hit, plus what the review page shows. Duration especially:
#: buskarr's matcher reads an absent duration as "cannot judge" and will
#: accept a file of any length, so dropping it here would turn a thirty-second
#: track into a request that any two-second file satisfies.
_KEPT = ("itemKey", "medium", "unit", "title", "year", "artist", "album",
         "source", "ref", "durationSeconds", "overview", "authors", "owned", "requested")


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

def start(user: jellyfin.User, medium: str, unit: str, filename: str,
          data: bytes | str) -> str:
    """Read a file, start matching it, and return the batch's id.

    The reading is done here, where somebody is waiting and can be told that
    their file has no title column. Only the matching -- one network call per
    row -- goes to a thread.
    """
    found = media.get(medium)
    if found is None:
        raise Unreadable("This server cannot be asked for that kind of thing.")
    if len(data) > config.IMPORT_MAX_BYTES:
        raise Unreadable(
            f"That file is larger than this accepts "
            f"({config.IMPORT_MAX_BYTES // 1000} kB).")
    sheet = read(data)
    # An unstated unit is worked out from the headings, which is the default
    # the form offers: a file with a Track Name column is a list of songs and
    # saying so twice is a chance to disagree with yourself.
    if unit not in found.units:
        unit = (suggest_unit(sheet) if medium == media.MUSIC
                else found.units[0])
    items, duplicates, blanks = rows(sheet, medium, unit)
    if not items:
        raise Unreadable("No row in that file had anything in its title column.")
    if len(items) > config.IMPORT_MAX_ROWS:
        raise Unreadable(
            f"That file has {len(items)} rows and this accepts "
            f"{config.IMPORT_MAX_ROWS} at a time. Split it up.")

    import_id = uuid.uuid4().hex
    payload = {
        "medium": medium, "unit": unit, "filename": filename,
        "duplicates": duplicates, "blanks": blanks,
        "headings": list(sheet.headings), "rows": [],
    }
    store.prune_imports(time.time() - config.IMPORT_RETENTION_HOURS * 3600)
    store.put_import(import_id, user.key, medium, READING, len(items), payload)
    log.info("import %s started user=%s medium=%s unit=%s rows=%d file=%r",
             import_id, user.key, medium, unit, len(items), filename)
    threading.Thread(target=_read_through, name=f"import-{import_id[:8]}",
                     args=(import_id, user, medium, unit, items),
                     daemon=True).start()
    return import_id


def _read_through(import_id: str, user: jellyfin.User, medium: str, unit: str,
                  items: list[Row]) -> None:
    """Match every row, then leave the batch waiting for a person."""
    # Waiting for the turn is not the same as being stuck, and `get` cannot
    # tell them apart from the outside: both look like a batch whose row count
    # has not moved. So a queued batch keeps its clock wound while it waits,
    # and "has not moved in fifteen minutes" goes on meaning what it says.
    while not _turn.acquire(timeout=60):
        store.touch_import(import_id, 0)
    try:
        matched: list[dict] = []
        try:
            requested = store.outstanding_keys(user.key, medium)
            for done, row in enumerate(items, start=1):
                matched.append(match(user, medium, unit, row, requested))
                store.touch_import(import_id, done)
                if config.IMPORT_PAUSE_SECONDS and done < len(items):
                    # These searches land on third-party catalogues, and the
                    # way to find their rate limit is to send five hundred
                    # queries at machine speed.
                    time.sleep(config.IMPORT_PAUSE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - the thread must not die
            # silently, or the page waits for a phase that has stopped.
            log.exception("import %s failed while reading: %s", import_id, exc)
            _close(import_id, FAILED, {"rows": matched,
                                       "error": "Something went wrong reading "
                                                "this file. Nothing was asked "
                                                "for."})
            return
        # Inside the try, not after it. Out there a failing write leaves the
        # batch reading forever, and the only thing that would ever move it is
        # the fifteen-minute stale clock -- which would call a finished match
        # an interrupted one.
        _close(import_id, READY, {"rows": matched})
        log.info("import %s read: %s", import_id, _tally(matched))
    finally:
        _turn.release()


def _tally(matched: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in matched:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return " ".join(f"{state}={count}" for state, count in sorted(counts.items()))


def _close(import_id: str, state: str, changes: dict) -> None:
    """Write the worked-out half of a batch back, keeping the rest."""
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
    if state in (READING, ASKING) and stale:
        # A container restarted mid-phase leaves a batch that will never
        # finish, and a page that says "still working" forever is worse than
        # one that says it stopped.
        log.warning("import %s was %s and has not moved; calling it failed",
                    import_id, state)
        # Written before it is stored, not after: the page reads `error` out
        # of the payload, so setting it on the way out would explain this once
        # and then show a blank reason on every load afterwards.
        payload["error"] = ("This stopped part way through — the service was "
                            "probably restarted. Nothing further was asked "
                            "for.")
        store.close_import(import_id, FAILED, payload)
        state = FAILED
    return {"id": import_id, "state": state, "done": row["done"],
            "total": row["total"], "created_at": row["created_at"], **payload}


def groups(batch: dict) -> dict[str, list[dict]]:
    """The batch's rows, gathered by what became of each."""
    out: dict[str, list[dict]] = {MATCHED: [], UNCERTAIN: [], HELD: [],
                                  MISSING: []}
    for row in batch.get("rows", []):
        out.setdefault(row["state"], []).append(row)
    return out


def affordable(user: jellyfin.User, batch: dict, lines: set[int]) -> int | None:
    """How many of these rows today's allowance covers, None if uncapped.

    Said before anybody presses the button, because the alternative is a
    report in which the first thirty rows were asked for and the rest say
    "you have had your three for today" -- which is the truth arriving in the
    least useful order.
    """
    left = wants.allowance(user, batch["medium"])
    if left is None:
        return None
    covered = 0
    for row in batch.get("rows", []):
        if row["line"] not in lines or row["state"] not in (MATCHED, UNCERTAIN):
            continue
        price = media.cost(batch["medium"], (row.get("hit") or {}).get("unit")
                           or batch["unit"])
        if price > left:
            break
        left -= price
        covered += 1
    return covered


def confirm(user: jellyfin.User, import_id: str, lines: set[int]) -> dict | None:
    """Start asking for the ticked rows. Returns the batch, or None.

    Ticks are checked against the batch rather than trusted: a posted line
    number that was never matched, or was matched to something already in the
    library, is dropped here. The form cannot offer those, which means a
    request for one did not come from the form.
    """
    # Read and moved on under one lock. Without it a double-tap on the button
    # -- or a client retrying a request it thought had timed out -- passes the
    # `READY` check twice and starts two threads asking for the same rows:
    # `wants.want` would refuse the second of each as a repeat, but both would
    # write a report and one of them would be the truth.
    with store.key_lock("import", import_id):
        batch = get(user, import_id)
        if batch is None:
            return None
        if batch["state"] != READY:
            return batch
        chosen = [row for row in batch.get("rows", [])
                  if row["line"] in lines
                  and row["state"] in (MATCHED, UNCERTAIN) and row.get("hit")
                  and not row["hit"].get("owned") and not row["hit"].get("requested")]
        if not chosen:
            return batch
        _close(import_id, ASKING, {"chosen": len(chosen)})
        store.touch_import(import_id, 0)
    log.info("import %s confirmed user=%s rows=%d", import_id, user.key,
             len(chosen))
    threading.Thread(target=_ask_for, name=f"import-ask-{import_id[:8]}",
                     args=(import_id, user, batch["medium"], chosen),
                     daemon=True).start()
    return get(user, import_id)


def _ask_for(import_id: str, user: jellyfin.User, medium: str,
             chosen: list[dict]) -> None:
    """Ask for each ticked row, and keep a line about every one of them."""
    asked: list[str] = []
    refused: list[str] = []
    already: list[str] = []
    try:
        for done, row in enumerate(chosen, start=1):
            hit = row["hit"]
            label = hit_label(hit)
            try:
                state, message = wants.want(
                    user, medium, hit.get("itemKey", ""),
                    hit.get("unit", ""), dict(hit))
            except wants.Denied as denied:
                refused.append(f"{label}: {denied}")
            except Exception as exc:  # noqa: BLE001 - one row's failure is
                # not the file's, and this runs where nobody is watching.
                log.warning("import %s could not ask for %r: %s",
                            import_id, label, exc)
                refused.append(f"{label}: something went wrong asking for it.")
            else:
                (already if state == wants.IN_LIBRARY or message.startswith("Already ") else asked).append(
                    label)
                log.info("import %s asked for %r state=%s", import_id, label,
                         state)
            store.touch_import(import_id, done)
    except Exception as exc:  # noqa: BLE001 - as above, for the loop itself.
        log.exception("import %s failed while asking: %s", import_id, exc)
    _close(import_id, DONE, {"report": {"asked": asked, "refused": refused,
                                        "already": already}})
    log.info("import %s done: asked=%d refused=%d already=%d", import_id,
             len(asked), len(refused), len(already))
