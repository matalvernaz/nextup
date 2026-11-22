"""What each account has said about its suggestions: more like this, less like this, already had it.

A mark is one person's own statement about one suggestion, so it is kept until
they take it back, unlike "Hide for now", which deliberately runs out after a
month. It is never written anywhere else: not as a Jellyfin favourite, which
would put things in somebody's Favourites they never chose (Matt, 2026-11-22),
and not as a Jellyfin rating, which cannot be cleared again and would show as
their score for something they have never seen.

Every recommender reads the same marks and the same weights from here, so "more
like this" pulls the same way on a film as on a book. Each one also has to find
a mark again after the thing it was made on has changed places -- a book asked
for from the catalogue that is now in the library, a podcast added in EchoFin --
and that matching lives with each medium's own engine, because only the engine
knows how its medium is identified.
"""
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from . import logs, store

log = logs.get("taste")

#: The media a mark can be made in, and the surfaces it can be made on. A film
#: or a show is only ever suggested from the library, so only podcasts and books
#: have a catalogue surface.
MEDIA = ("movie", "series", "podcast", "book")
OWNED = "owned"
CATALOGUE = "catalogue"
SURFACES = (OWNED, CATALOGUE)
CATALOGUE_MEDIA = ("podcast", "book")

MORE = "more"
LESS = "less"
NO_OPINION = ""
OPINIONS = (MORE, LESS, NO_OPINION)

# How hard a mark pulls, on the scale every engine already uses for a seed.
#
# "Less like this" is an explicit dislike, so it outranks everything else known
# about the thing, the way a rating under five already does; -0.75 is where a
# film rated about two sits, and close to the book engine's "disliked" (-0.7).
# "More like this" counts like having watched it through (1.0), and "already
# had it, liked it" like a favourite (1.25). "Already had it" with no opinion is
# the book engine's weight for a book finished and never rated (0.35): it was
# chosen, which says a little, and nothing about whether it was any good.
LESS_WEIGHT = -0.75
MORE_WEIGHT = 1.0
LIKED_WEIGHT = 1.25
HAD_WEIGHT = 0.35

#: Longest key accepted. A Jellyfin id is 32 characters, an ASIN 10, a podcast
#: feed key 21; anything near this is not one of them.
MAX_KEY_LENGTH = 200
MAX_TITLE_LENGTH = 500


@dataclass(frozen=True, slots=True)
class Mark:
    """One account's standing mark on one thing."""
    medium: str
    item_key: str
    surface: str
    opinion: str
    had: bool
    title: str = ""
    details: dict = field(default_factory=dict)
    revision: int = 0

    @property
    def active(self) -> bool:
        """Whether it says anything, rather than being a mark taken back.

        A mark taken back still counts as the newest thing said about its
        item, so it overrides an older mark that reaches the same item another
        way: "more like this" said of a podcast on the catalogue list, taken
        back from its row once the library holds it. It pulls no weight and
        hides nothing itself.
        """
        return bool(self.opinion) or self.had

    @property
    def hides(self) -> bool:
        """Whether it keeps the thing off every list it would be offered on."""
        return self.had or self.opinion == LESS

    @property
    def asked_only(self) -> bool:
        """"More like this" about something not had: evidence nobody has heard."""
        return self.opinion == MORE and not self.had

    def state(self) -> dict:
        return {"opinion": self.opinion, "had": self.had}


def _from_row(row) -> Mark:
    try:
        details = json.loads(row["details"] or "{}")
    except ValueError:
        details = {}
    return Mark(
        medium=row["medium"],
        item_key=row["item_key"],
        surface=row["surface"],
        opinion=row["opinion"],
        had=bool(row["had"]),
        title=row["title"] or "",
        details=details if isinstance(details, dict) else {},
        revision=int(row["revision"]))


def marks(user_key: str, medium: str) -> list[Mark]:
    """Every mark one account has made in one medium, oldest first.

    Including those taken back, which only ever override older ones: see
    `Mark.active`.
    """
    return [_from_row(row) for row in store.taste_marks(user_key, medium)]


def revision(user_key: str, medium: str) -> int:
    """The newest change to one account's marks in one medium, or 0 for none.

    A shelf keeps the revision it was built from. One built from an older
    revision is out of date however young it is.
    """
    return store.taste_revision(user_key, medium)


def weight(base: float, mark: Mark | None) -> float:
    """A seed's weight once its mark is taken into account."""
    if mark is None:
        return base
    if mark.opinion == LESS:
        return LESS_WEIGHT
    if mark.opinion == MORE:
        return max(base, LIKED_WEIGHT if mark.had else MORE_WEIGHT)
    if mark.had:
        return max(base, HAD_WEIGHT)
    return base


def newest(*found: "Mark | None") -> "Mark | None":
    """The most recently made of several marks that all name one thing.

    A podcast marked from the catalogue and marked again once it was in the
    library has two; the later one is what the person thinks now.
    """
    present = [mark for mark in found if mark is not None]
    return max(present, key=lambda mark: mark.revision) if present else None


class Invalid(ValueError):
    """A mark that cannot be stored, with the sentence that says why."""


class Changed(Exception):
    """An Undo for a mark that has been changed since, somewhere else."""

    SENTENCE = ("That was changed again since, so it has been left as it is "
                "now.")


def check(medium: str, surface: str, item_key: str, opinion: str) -> None:
    """Refuse a mark this server could never match against anything."""
    if medium not in MEDIA:
        raise Invalid(f"Suggestions cannot be marked for {medium!r}.")
    if surface not in SURFACES:
        raise Invalid(f"There is no {surface!r} list to mark a suggestion on.")
    if surface == CATALOGUE and medium not in CATALOGUE_MEDIA:
        raise Invalid(f"There is no catalogue list of {medium} suggestions.")
    if opinion not in OPINIONS:
        raise Invalid("Say more, less, or nothing about a suggestion.")
    key = item_key.strip()
    if not key or len(key) > MAX_KEY_LENGTH or not key.isprintable():
        raise Invalid("That suggestion has no usable id.")


def record(user_key: str, medium: str, item_key: str, surface: str,
           opinion: str, had: bool, title: str = "",
           details: dict | None = None,
           if_revision: int | None = None) -> tuple[Mark | None, Mark]:
    """Store one account's mark and answer what it replaced and what it is now.

    The answer before is what the app puts back when somebody presses Undo, so
    it is the whole of the earlier state, read in the same transaction as the
    write, not a guess at it.

    `if_revision` is what Undo sends: the revision its change was given. A
    mark changed since is left alone and `Changed` is raised, so taking back
    one change can never undo a later one made somewhere else.
    """
    check(medium, surface, item_key, opinion)
    key = item_key.strip()
    try:
        previous_row, current_row = store.put_taste_mark(
            user_key, medium, key, surface, opinion, had,
            (title or "").strip()[:MAX_TITLE_LENGTH],
            json.dumps(details or {}, sort_keys=True),
            if_revision=if_revision)
    except store.StaleMark as exc:
        raise Changed(Changed.SENTENCE) from exc
    previous = _from_row(previous_row) if previous_row is not None else None
    if previous is not None and not previous.opinion and not previous.had:
        previous = None
    current = _from_row(current_row)
    log.info("mark user=%s medium=%s surface=%s key=%s opinion=%s had=%s rev=%d",
             user_key, medium, surface, key, opinion or "-", had, current.revision)
    return previous, current


# --- rebuilding where nobody is waiting ---------------------------------------


def _start(target: Callable[[], None], name: str) -> None:
    """Run work off the request. A seam, so a test can run it in place."""
    threading.Thread(target=target, name=name, daemon=True).start()


class BehindRebuilds:
    """Shelf rebuilds started by a mark, one at a time per shelf, never one too few.

    A mark changes what a shelf should hold, and the person who made it is not
    going to wait twelve seconds to find out, so the rebuild runs behind the
    answer. Two rules make that safe. Only one rebuild runs per shelf at a
    time. And a request that arrives while one is running is never dropped:
    that rebuild read the marks before this one was made, so it is followed by
    one more. The flag is checked and cleared under the same lock the worker
    leaves under, so a request can never land in the gap between a worker's
    last look and its exit.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._guard = threading.Lock()
        self._running: set = set()
        self._wanted: set = set()

    def request(self, key, build: Callable[[], None]) -> None:
        with self._guard:
            self._wanted.add(key)
            if key in self._running:
                return
            self._running.add(key)

        def run() -> None:
            while True:
                with self._guard:
                    if key not in self._wanted:
                        self._running.discard(key)
                        return
                    self._wanted.discard(key)
                try:
                    build()
                except Exception as exc:  # noqa: BLE001 - nobody is waiting on
                    # this, and the shelf already being served is still there.
                    log.warning("%s rebuild after a mark failed key=%s: %s",
                                self.name, key, exc)

        _start(run, f"{self.name}-after-mark")

    def running(self, key) -> bool:
        with self._guard:
            return key in self._running
