"""Finding a song from somebody's list in the Jellyfin music library.

Two things use it. An imported list of songs checks the library before asking for anything, so a
song already here is not asked for again; and a playlist made from a list needs the library item
each of its songs is, to put it in.

The title is matched more strictly than the importer's catalogue key, which drops every
parenthetical. Here only the asides that name the same recording differently go: a featured
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
_LIST_SPLIT = re.compile(r"\s*(?:,|;|&|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b)\s*",
                         re.IGNORECASE)
#: What separates a lead from the guests in a library credit.
_GUEST_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with)\s+", re.IGNORECASE)


def _plain(text: str) -> str:
    """Case, accents and curly apostrophes away, and "&" read as "and"."""
    text = unicodedata.normalize("NFKD", text or "").replace("’", "'").replace("‘", "'")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _key(text: str) -> str:
    return " ".join(_LETTERS.sub(" ", text.replace("&", " and ")).split())


def song_key(title: str) -> str:
    """A song title reduced to what two spellings of one recording share."""
    text = _plain(title).strip()
    dash = _DASH_ASIDE.search(text)
    if dash:
        text = text[:dash.start()] + f" ({dash.group(1)})"
    kept = []

    def aside(match: re.Match) -> str:
        words = match.group(1).strip()
        if words and not _CREDIT_ASIDE.search(words):
            rest = _key(_REMASTER.sub(" ", words))
            if rest:
                kept.append(rest)
        return " "

    text = _ASIDE.sub(aside, text)
    return " ".join([_key(text)] + kept).strip()


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
        wanted = listed_names(artist)
        found = [item for item in self._by_title.get(song_key(title), [])
                 if wanted & _item_names(item)]
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
