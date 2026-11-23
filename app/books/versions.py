"""Versions of one book: one search row per book, with its recordings beside it.

Audible lists a book once per recording and once per store. Searching
"domestication" gave three rows for one book: Michael Kramer's reading twice
(the Canadian and American stores' copies of one recording, and only one of
them marked as owned) and GraphicAudio's dramatization (Matt, 2026-11-22:
"There shouldn't be anything duplicated, it should just give you the option
later."). Here a search's rows are folded into books, a book's rows into
recordings, and the two stores' copies of one recording into one.

GraphicAudio is its own case. A household with its own GraphicAudio library
gets those dramatizations there, not from Audible, whose copies are worse
("I don't get it through audible since their versions are lower quality").
`GRAPHIC_AUDIO_LIBRARY_IDS` names that library, so a dramatization in it is
recognised as held although it has no ASIN and is named differently; and
`REFUSE_GRAPHIC_AUDIO` stops one ever being asked for (see `listenarr.add`).
Both unset is a server that behaves as it always did.
"""
import re

from .. import config, external_books
from . import engine, series

#: A publisher that makes nothing but dramatizations, with the spaces taken
#: out; one listing names it both "Graphic Audio LLC" and "GraphicAudio".
GRAPHIC_AUDIO = "graphicaudio"

#: Words in a title that name one recording rather than the book, once
#: normalised: "(Dramatized Adaptation)", "[Unabridged]", "Full-Cast Edition".
_RECORDING_WORDS = ("dramatized adaptation", "dramatised adaptation",
                    "full cast edition", "full cast", "unabridged", "abridged",
                    "an audiobook", "audiobook")

#: How far apart two copies' lengths may be and still be one recording. The
#: two stores round differently, and a re-recording is never this close.
SAME_LENGTH_MINUTES = 10
SAME_LENGTH_SHARE = 0.03

#: More other versions than this are counted rather than listed.
MAX_NAMED_VERSIONS = 3

_LEADING_NUMBER = re.compile(r"^\d+(?:\.\d+)?\s+")


def is_graphic_audio(row: dict) -> bool:
    """Whether a catalogue row is GraphicAudio's, by its publisher alone.

    Not "any dramatization": the Harry Potter full-cast editions are
    dramatized and are Pottermore's, and a household that gets GraphicAudio
    elsewhere may still want those from Audible.
    """
    publisher = engine._norm(str(row.get("publisher") or "")).replace(" ", "")
    return GRAPHIC_AUDIO in publisher


def refuses(row: dict) -> bool:
    """Whether this server never asks for this row: GraphicAudio, where it says so."""
    return config.REFUSE_GRAPHIC_AUDIO and is_graphic_audio(row)


def base_title(title: str) -> str:
    """The book a title names, without its subtitle or the words for one recording."""
    padded = f" {external_books.main_title(title or '')} "
    for words in _RECORDING_WORDS:
        padded = padded.replace(f" {words} ", " ")
    return " ".join(padded.split())


def same_book(one: str, other: str) -> bool:
    """Whether two titles with one base title can name the same book.

    The base title drops the subtitle, and a subtitle can name a book:
    "Mistborn: The Final Empire" and "Mistborn: The Well of Ascension" share
    "mistborn" and are books one and two, and five Disney Princess stories
    share their head. So the subtitles decide, by the owned index's own rules
    (`engine._could_be_one_book`): two that describe, or that share most of
    their words, are one book; a plain title is one book with a subtitle that
    only describes it, and not with one that names a sequel.
    """
    first = engine._head_and_tail(one or "")
    second = engine._head_and_tail(other or "")
    tail_one = first[1] if first else ""
    tail_two = second[1] if second else ""
    if not tail_one and not tail_two:
        return True
    if not tail_one or not tail_two:
        return engine._describes_rather_than_names(tail_one or tail_two)
    return engine._could_be_one_book(tail_one, tail_two)


def _names(values) -> list[str]:
    out = []
    for value in values or []:
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def _authors_of(row: dict) -> set[str]:
    return {engine._norm_author(name) for name in _names(row.get("authors")) if name}


def _minutes(row: dict) -> int | None:
    for field in ("lengthMinutes", "runtimeMinutes", "runtimeLengthMin"):
        value = row.get(field)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _close_lengths(one: int | None, other: int | None) -> bool:
    if one is None or other is None:
        return one is None and other is None
    return abs(one - other) <= max(SAME_LENGTH_MINUTES, SAME_LENGTH_SHARE * max(one, other))


def _length(minutes: int | None) -> str:
    """A length as it is said: "12 hours", "10 hours 25 minutes", "45 minutes"."""
    if not minutes:
        return ""
    hours, rest = divmod(int(minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if rest and not (hours and rest < 5):
        parts.append(f"{rest} minute{'s' if rest != 1 else ''}")
    return " ".join(parts) if parts else f"{hours} hours"


# --- what the library holds of GraphicAudio ---------------------------------


def _ga_title_forms(name: str) -> set[str]:
    """The titles a GraphicAudio library item might be the book of.

    The library names them every which way: "Battle Mage Farmer 1:
    Domestication", "001 Pilgrimage To Hell", "DRUDEN". The whole name, the
    part after the colon, and the name without a leading number between them
    reach the catalogue's "Domestication (Dramatized Adaptation)".
    """
    forms = set()
    whole = engine._norm(name)
    if whole:
        forms.add(base_title(whole))
    if ":" in name:
        head, tail = name.split(":", 1)
        for part in (head, tail):
            if engine._norm(part):
                forms.add(base_title(part))
    unnumbered = _LEADING_NUMBER.sub("", name.strip())
    if unnumbered != name.strip() and engine._norm(unnumbered):
        forms.add(base_title(unnumbered))
    return {form for form in forms if form}


def _real_authors(item: dict) -> set[str]:
    """An item's authors, leaving out GraphicAudio itself where it is filed as one."""
    found = set()
    for name in engine._authors(item):
        if GRAPHIC_AUDIO in engine._norm(name).replace(" ", ""):
            continue
        found.add(engine._norm_author(name))
    return {name for name in found if name}


class GraphicAudioIndex:
    """The household's own GraphicAudio library, asked one thing: is this row there.

    Narrow on purpose. It decides whether a GraphicAudio row is held and
    nothing else: never whether a reading is owned, never Discover or a
    series. A title matches when the author is the same, or when the library
    item names no author at all, which in a library of nothing but
    GraphicAudio is a smaller risk than missing the book.
    """

    def __init__(self, items: list[dict]) -> None:
        self._titles: dict[str, list[set[str]]] = {}
        for item in items:
            authors = _real_authors(item)
            for form in _ga_title_forms(item.get("Name") or ""):
                self._titles.setdefault(form, []).append(authors)

    def __bool__(self) -> bool:
        return bool(self._titles)

    def holds(self, row: dict) -> bool:
        held = self._titles.get(base_title(str(row.get("title") or "")))
        if not held:
            return False
        wanted = _authors_of(row)
        return any(not authors or not wanted or wanted & authors for authors in held)


def is_graphic_audio_item(item: dict) -> bool:
    """Whether a library item came from a library named as GraphicAudio's."""
    return bool(config.GRAPHIC_AUDIO_LIBRARY_IDS) and (
        item.get("_library") in config.GRAPHIC_AUDIO_LIBRARY_IDS)


# --- what the library holds of everything else --------------------------------


class Recordings:
    """Which recording of a book the library holds, where it can be told.

    The owned index says a title is held, and most of this library carries no
    ASIN, so that says "some version" and not which one. The reader and the
    length do: two readings of one book are never by the same narrator at
    nearly the same length.
    """

    def __init__(self, items: list[dict]) -> None:
        self._by_work: dict[str, list[tuple[str, set[str], set[str], int | None]]] = {}
        for item in items:
            if is_graphic_audio_item(item):
                continue
            name = item.get("Name") or ""
            authors = {engine._norm_author(a) for a in engine._authors(item) if a}
            readers = set(series._readers(engine._people(item, "Narrator")))
            ticks = item.get("RunTimeTicks") or 0
            minutes = int(ticks / 600_000_000) if ticks else None
            for key in {base_title(name)} - {""}:
                self._by_work.setdefault(key, []).append((name, authors, readers, minutes))

    def holds(self, title: str, authors: set[str], readers: set[str],
              minutes: int | None) -> bool | None:
        """True or False where the library says, None where it cannot tell."""
        found = [entry for entry in self._by_work.get(base_title(title), [])
                 if same_book(entry[0], title)
                 and (not entry[1] or not authors or entry[1] & authors)]
        if not found:
            return False
        undecided = False
        for _, _, held_readers, held_minutes in found:
            if held_readers and readers:
                if held_readers & readers:
                    return True
                continue
            if held_minutes is not None and minutes is not None:
                if _close_lengths(held_minutes, minutes):
                    return True
                continue
            undecided = True
        return None if undecided else False


class Held:
    """What the library holds that its owned index cannot say: GraphicAudio's
    dramatizations in their own library, and which recording of a book it has.

    Built from the same listing as the owned index and kept beside it
    (`shelves._list_and_publish`), so a search costs no extra library read.
    """

    def __init__(self, library: list[dict]) -> None:
        self.graphic_audio = GraphicAudioIndex(
            [item for item in library if is_graphic_audio_item(item)])
        self.recordings = Recordings(library)


# --- one row per book -----------------------------------------------------------


class _Recording:
    """One recording of a book: one or both stores' copies of it."""

    def __init__(self, row: dict) -> None:
        self.rows = [row]
        self.version = series._row_version(row)
        self.minutes = _minutes(row)
        self.graphic_audio = is_graphic_audio(row)

    def same(self, row: dict) -> bool:
        return (series._row_version(row) == self.version
                and is_graphic_audio(row) == self.graphic_audio
                and _close_lengths(_minutes(row), self.minutes))

    @property
    def first(self) -> dict:
        return self.rows[0]

    @property
    def asins(self) -> list[str]:
        return [str(row.get("asin") or "").upper() for row in self.rows if row.get("asin")]


class _Book:
    def __init__(self, row: dict) -> None:
        self.full_title = str(row.get("title") or "")
        self.title = base_title(self.full_title)
        self.authors = _authors_of(row)
        self.recordings = [_Recording(row)]

    def takes(self, row: dict) -> bool:
        title = str(row.get("title") or "")
        if base_title(title) != self.title or not same_book(self.full_title, title):
            return False
        authors = _authors_of(row)
        return (authors & self.authors) or (not authors and not self.authors)

    def add(self, row: dict) -> None:
        for recording in self.recordings:
            if recording.same(row):
                recording.rows.append(row)
                return
        self.recordings.append(_Recording(row))


def group(rows: list[dict]) -> list[list[list[dict]]]:
    """Catalogue rows as books, each a list of recordings, each a list of copies.

    In the order the catalogue gave them: a book sits where its first row did.
    """
    books: list[_Book] = []
    for row in rows:
        if not row.get("asin"):
            continue
        for book in books:
            if book.takes(row):
                book.add(row)
                break
        else:
            books.append(_Book(row))
    return [[recording.rows for recording in book.recordings] for book in books]


def label(version: dict) -> str:
    """What one version is called where a choice is offered."""
    length = _length(version.get("runtimeMinutes"))
    readers = [name for name in version.get("narrators") or []
               if not series._is_cast_credit(name)]
    if version.get("graphicAudio"):
        words = "GraphicAudio dramatization"
    elif version.get("dramatized"):
        words = "Dramatization with a full cast"
    elif version.get("abridged"):
        words = "Abridged, read by " + ", ".join(readers[:2]) if readers else "Abridged"
    elif readers:
        words = "Read by " + ", ".join(readers[:2])
    else:
        words = "Another recording"
    return f"{words}, {length}" if length else words


def summary(others: list[dict]) -> str:
    """The sentence a row says about the versions it does not stand for."""
    if not others:
        return ""
    if len(others) > MAX_NAMED_VERSIONS:
        return f"Also {len(others)} other versions."
    parts = []
    for version in others:
        if version.get("graphicAudio"):
            if version.get("owned"):
                parts.append("a GraphicAudio dramatization, in your GraphicAudio library")
            elif not version.get("askable"):
                parts.append("a GraphicAudio dramatization, which NextUp does not get")
            else:
                parts.append("a GraphicAudio dramatization")
            continue
        readers = [name for name in version.get("narrators") or []
                   if not series._is_cast_credit(name)]
        if version.get("dramatized"):
            part = "a dramatization with a full cast"
        elif version.get("abridged"):
            part = "an abridged reading" + (f" by {readers[0]}" if readers else "")
        elif readers:
            part = "a reading by " + ", ".join(readers[:2])
        else:
            part = "another recording"
        if version.get("owned"):
            part += ", which you have"
        parts.append(part)
    return "Also " + "; ".join(parts) + "."
