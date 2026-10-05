"""Finding a song from somebody's list in the Jellyfin music library.

Two things use it. An imported list of songs checks the library before asking for anything, so a
song already here is not asked for again; and a playlist made from a list needs the library item
each of its songs is, to put it in.

Library lookup and catalogue imports share the same recording key. Only the
asides that name the same recording differently go: a featured
artist ("feat. X") and a remaster ("2011 Remaster"). "Live", "Acoustic", "Demo", "From The Vault"
and "Taylor's Version" stay, because each is a different recording and a playlist that asked for
one should not be given another. Spotify writes those asides after a dash ("Song - Live"), and the
library in brackets ("Song (Live)"); both are read as the same aside.

The credit matches when one name the export lists is a whole credit the library holds. An export
lists several artists separated by commas, semicolons or "&", and the library holds whole credits
("Simon & Garfunkel"), an album artist, and a lead before "feat.". The library's credits are not
cut at "and", "&" or "x", which are as often part of one name ("Belle and Sebastian") as a join
between two: cut there, "Sebastian" would hold Belle and Sebastian's "Home".

Keys keep letters of every script, so a song titled in Cyrillic or Japanese is found too.
"""
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import unquote

from . import jellyfin, logs

log = logs.get("songs")

#: An aside that is a credit rather than a version: dropped whole.
_CREDIT_ASIDE = re.compile(r"^(feat\.?|ft\.?|featuring|with|prod\.?|produced by)(\s|$)",
                           re.IGNORECASE)
#: A remaster, which is the same recording: cut out of an aside, leaving the rest of it, so
#: "(Live, 2011 Remaster)" still says live.
_REMASTER = re.compile(r"\b(\d{4}\s+)?(digitally\s+)?remaster(ed)?(\s+(version|\d{4}))*\b",
                       re.IGNORECASE)
_ASIDE = re.compile(r"[(\[]([^)\]]*)[)\]]")
#: Spotify's way of adding an aside: "Song - Remastered 2011", "Song - Live at Wembley".
_DASH_ASIDE = re.compile(r"\s+-\s+(.+)$")
#: Anything that is not a letter or digit, in any script.
_LETTERS = re.compile(r"[\W_]+")
#: What separates artists in an export's credit column.
_LIST_SPLIT = re.compile(r"\s*(?:,|;|&|\b(?:featuring|feat\.?|ft\.?|with)(?=\s|$))\s*",
                         re.IGNORECASE)
#: What separates a lead from the guests in a library credit.
_GUEST_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with)\s+", re.IGNORECASE)
_TITLE_GUEST = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.IGNORECASE)
_SINGLE = re.compile(r"\s+(?:-\s*)?single\s*$", re.IGNORECASE)


def _plain(text: str) -> str:
    """Case, accents and curly apostrophes away, and "&" read as "and"."""
    text = unescape(unquote(text or ""))
    text = unicodedata.normalize("NFKD", text).replace("’", "'").replace("‘", "'")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _key(text: str) -> str:
    return " ".join(_LETTERS.sub(" ", text.replace("&", " and ")).split())


def song_key(title: str) -> str:
    """A song title reduced to what two spellings of one recording share."""
    base, versions = title_parts(title)
    # Keep the boundary: a recording qualifier must not disappear into the
    # ordinary title when spaces are ignored.
    return base.replace(" ", "") + "".join("|" + v.replace(" ", "") for v in versions)


def title_parts(title: str) -> tuple[str, tuple[str, ...]]:
    """Comparable title and recording qualifiers, without changing the display text."""
    text = _SINGLE.sub("", _plain(title).replace("_", " ").strip())
    dash = _DASH_ASIDE.search(text)
    if dash:
        text = text[:dash.start()] + f" ({dash.group(1)})"
    kept = []

    def aside(match: re.Match) -> str:
        words = match.group(1).strip()
        if words and words != "single" and not _CREDIT_ASIDE.search(words):
            rest = _key(_REMASTER.sub(" ", words))
            if rest:
                kept.append(rest)
        return " "

    text = _ASIDE.sub(aside, text)
    text = _TITLE_GUEST.sub("", text)
    text = re.sub(r"\bn'?(?=\s|$)", "and", text)
    # The file's example drops an article as well as changing punctuation.
    # This rule is music-only; books and film titles keep their articles.
    base = " ".join(w for w in _key(text).split() if w != "the")
    return base, tuple(kept)


def search_title(title: str) -> str:
    """Readable query text with export encoding and release labels removed."""
    text = unescape(unquote(title or "")).replace("_", " ")
    text = _SINGLE.sub("", text.strip())
    return re.sub(r"\bN['’]?(?=\s|$)", "and", text, flags=re.IGNORECASE)


def name_key(credit: str) -> str:
    return _key(re.sub(r"^by\s+", "", _plain(credit).strip()))


def _same_name(a: str, b: str) -> bool:
    a, b = name_key(a), name_key(b)
    if not a or not b:
        return False
    if a == b:
        return True
    first, second = a.split(), b.split()
    # First/last-name inversion only, never an arbitrary rearrangement of a
    # band name, an initial, or a credit joined with 'and'.
    excluded = {"and", "the", "band", "orchestra", "dj"}
    return (len(first) == len(second) == 2 and first == second[::-1]
            and not excluded.intersection(first)
            and all(len(word) > 1 and word.isalpha() for word in first))


def same_credit(a: str, b: str) -> bool:
    """Whole credits, allowing first/last inversion within named contributors."""
    if _same_name(a, b):
        return True
    left = [p for p in _LIST_SPLIT.split(_plain(a)) if p.strip()]
    right = [p for p in _LIST_SPLIT.split(_plain(b)) if p.strip()]
    if len(left) < 2 or len(left) != len(right):
        return False
    # Do not turn one half of a band's name into a match. Both sides must
    # supply the same complete list of contributors.
    unused = list(right)
    for part in left:
        at = next((i for i, other in enumerate(unused) if _same_name(part, other)), None)
        if at is None:
            return False
        unused.pop(at)
    return True


def guests(title: str) -> str:
    credits = []
    for match in _ASIDE.finditer(_plain(title)):
        aside = match.group(1).strip()
        prefix = _CREDIT_ASIDE.match(aside)
        if prefix:
            credits.append(aside[prefix.end():].strip())
    tail = _TITLE_GUEST.search(_ASIDE.sub(" ", _plain(title)))
    if tail:
        credits.append(tail.group(1).strip())
    return "; ".join(credits)


def recording_matches(title: str, artist: str, other_title: str, other_artist: str) -> bool:
    """A title and its credited artist identify the same recording."""
    base, _ = title_parts(title)
    if not base or song_key(title) != song_key(other_title):
        return False
    if same_credit(artist, other_artist):
        left, right = guests(title), guests(other_title)
        return not (left and right) or same_credit(left, right)
    # A guest may be in the title on one export and the artist field on the
    # other. Compare the complete credit after moving that metadata across.
    left = artist + (" feat. " + guests(title) if guests(title) else "")
    right = other_artist + (" feat. " + guests(other_title) if guests(other_title) else "")
    return same_credit(left, right)


def close_score(title: str, artist: str, other_title: str, other_artist: str) -> float:
    """Rank plausible suggestions; unrelated artist/title hits score zero."""
    base, _ = title_parts(title)
    other, _ = title_parts(other_title)
    if not base or not other:
        return 0.0
    title_score = SequenceMatcher(None, base.replace(" ", ""), other.replace(" ", "")).ratio()
    if title_score < 0.7:
        return 0.0
    if artist and other_artist and not same_credit(artist, other_artist):
        if SequenceMatcher(None, name_key(artist), name_key(other_artist)).ratio() < 0.8:
            return 0.0
    return title_score


def listed_names(credit: str) -> set[str]:
    """The names an export's credit lists, as keys, and the whole credit as one more."""
    plain = _plain(credit)
    names = {_key(part) for part in _LIST_SPLIT.split(plain)}
    names.add(_key(plain))
    names.discard("")
    return names


def library_names(credits: list[str]) -> set[str]:
    """The library's credits for a song, whole, and each lead before its guests."""
    names = set()
    for credit in credits:
        plain = _plain(credit)
        names.add(_key(plain))
        names.add(_key(_GUEST_SPLIT.split(plain)[0]))
    names.discard("")
    return names


#: The longest a read of the library is reused while Jellyfin's count of songs
#: stays the same. The count catches a song arriving, which is what matters;
#: this catches one replaced by another.
REREAD_SECONDS = 3600

_cached: tuple[int, float, "LibraryIndex"] | None = None
_cached_guard = threading.Lock()


class LibraryIndex:
    """The music library's songs, keyed for finding a song from a list."""

    def __init__(self, items: list[dict]):
        self._by_title: dict[str, list[dict]] = {}
        self._ids: set[str] = set()
        for item in items:
            self._ids.add(item["Id"])
            key = song_key(item.get("Name") or "")
            if key:
                self._by_title.setdefault(key, []).append(item)
        self.size = len(items)

    @classmethod
    def load(cls) -> "LibraryIndex":
        """The library, read again only when its count of songs has changed.

        A whole read is thousands of songs and seconds of Jellyfin's time, and
        the pass that fills playlists asks every ten minutes for as long as a
        song is waited for. Counting is one cheap call.
        """
        global _cached
        count = jellyfin.audio_count()
        with _cached_guard:
            if (_cached is not None and _cached[0] == count
                    and time.monotonic() - _cached[1] < REREAD_SECONDS):
                return _cached[2]
        index = cls(jellyfin.audio_items())
        with _cached_guard:
            _cached = (count, time.monotonic(), index)
        return index

    def has(self, item_id: str) -> bool:
        return bool(item_id) and item_id in self._ids

    def find(self, title: str, artist: str, album: str = "") -> str | None:
        """The library item id for this song, or None.

        No credit, no match: a title alone reaches the wrong "Home". Where the
        library holds the song more than once, the copy on the named album wins,
        and otherwise the first.
        """
        if not artist:
            return None
        found = [item for item in self._by_title.get(song_key(title), [])
                 if any(_same_name(want, held)
                        for want in listed_names(artist) for held in _item_names(item))
                 and (not guests(title) or not guests(item.get("Name") or "")
                      or same_credit(guests(title), guests(item.get("Name") or "")))]
        if not found:
            return None
        if album:
            same = song_key(album)
            for item in found:
                if song_key(item.get("Album") or "") == same:
                    return item["Id"]
        return found[0]["Id"]


def _item_names(item: dict) -> set[str]:
    return library_names((item.get("Artists") or []) + [item.get("AlbumArtist") or ""])
