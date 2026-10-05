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

The credit matches when any one name on either side is the same: the library stores a
collaboration as one credit ("Psychostick & Retroware Sound Team") and an export may list only the
first name, or all of them separated by commas.
"""
import re
import unicodedata

from . import jellyfin, logs

log = logs.get("songs")

#: An aside that names the same recording another way.
_SAME_RECORDING = re.compile(
    r"^(feat\.?|ft\.?|featuring|with|prod\.?|produced by)(\s|$)|\bremaster(ed)?\b",
    re.IGNORECASE)
_ASIDE = re.compile(r"[(\[]([^)\]]*)[)\]]")
#: Spotify's way of adding an aside: "Song - Remastered 2011", "Song - Live at Wembley".
_DASH_ASIDE = re.compile(r"\s+-\s+(.+)$")
_LETTERS = re.compile(r"[^a-z0-9]+")
#: What separates the names in one credit.
_CREDIT_SPLIT = re.compile(
    r"\s*(?:,|;|/|&|\+|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\band\b)\s*",
    re.IGNORECASE)


def _plain(text: str) -> str:
    """Case, accents and curly apostrophes away."""
    text = unicodedata.normalize("NFKD", text or "").replace("’", "'").replace("‘", "'")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def song_key(title: str) -> str:
    """A song title reduced to what two spellings of one recording share."""
    text = _plain(title).strip()
    dash = _DASH_ASIDE.search(text)
    if dash:
        text = text[:dash.start()] + f" ({dash.group(1)})"
    kept = []

    def aside(match: re.Match) -> str:
        words = match.group(1).strip()
        if words and not _SAME_RECORDING.search(words):
            kept.append(words)
        return " "

    text = _ASIDE.sub(aside, text)
    whole = " ".join([text] + [f"({words})" for words in kept])
    return " ".join(_LETTERS.sub(" ", whole).split())


def credit_names(credit: str) -> set[str]:
    """Every name in a credit, as keys, and the whole credit as one more."""
    plain = _plain(credit)
    names = {" ".join(_LETTERS.sub(" ", part).split())
             for part in _CREDIT_SPLIT.split(plain)}
    names.add(" ".join(_LETTERS.sub(" ", plain).split()))
    names.discard("")
    return names


class LibraryIndex:
    """The music library's songs, keyed for finding a song from a list."""

    def __init__(self, items: list[dict]):
        self._by_title: dict[str, list[dict]] = {}
        for item in items:
            key = song_key(item.get("Name") or "")
            if key:
                self._by_title.setdefault(key, []).append(item)
        self.size = len(items)

    @classmethod
    def load(cls) -> "LibraryIndex":
        return cls(jellyfin.audio_items())

    def find(self, title: str, artist: str, album: str = "") -> str | None:
        """The library item id for this song, or None.

        No credit, no match: a title alone reaches the wrong "Home". Where the
        library holds the song more than once, the copy on the named album wins,
        and otherwise the first.
        """
        if not artist:
            return None
        wanted = credit_names(artist)
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
    names: set[str] = set()
    for credit in (item.get("Artists") or []) + [item.get("AlbumArtist") or ""]:
        names |= credit_names(credit)
    return names
