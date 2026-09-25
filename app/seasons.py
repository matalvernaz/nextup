"""Which seasons of a series to ask for.

Four answers, because they are the four things somebody means: every season,
the latest one, a stretch of them, or none of what has aired yet, only what
airs from now on. Nothing at all is not one of them. A series asked for with
nothing to fetch never arrives, so the request would read "on its way" for
good, and there is no second step here that could pick seasons for it later.

A choice travels three ways and is spelled the same in all of them: in a
request body and in `/capabilities` as a JSON object, and in the ledger and an
account's settings as one short string (`all`, `latest`, `new`, `range:2-4`).
The latest season is only known once Sonarr has the episode list, so a ledger
row can also carry the season it turned out to be (`latest:37`).
"""
from typing import NamedTuple

ALL = "all"
LATEST = "latest"
RANGE = "range"
NEW = "new"
CHOICES = (ALL, LATEST, RANGE, NEW)

#: The per-account setting that holds somebody's usual choice.
SETTING = "SERIES_SEASONS"

#: No real series runs past this, and a bound keeps a typo from reading as a
#: request for seasons nobody could have meant.
HIGHEST_SEASON = 200


class Seasons(NamedTuple):
    """One choice. `first` and `last` are the stretch for a range; for the
    latest season, `last` is the season it resolved to once that is known."""

    choice: str
    first: int = 0
    last: int = 0

    def encode(self) -> str:
        if self.choice == RANGE:
            return f"{RANGE}:{self.first}-{self.last}"
        if self.choice == LATEST and self.last:
            return f"{LATEST}:{self.last}"
        return self.choice

    def as_json(self) -> dict:
        out: dict = {"choice": self.choice}
        if self.choice == RANGE:
            out["from"] = self.first
            out["to"] = self.last
        elif self.choice == LATEST and self.last:
            out["season"] = self.last
        return out

    def phrase(self) -> str:
        """What was asked for, to finish a sentence: "Asked for ___"."""
        if self.choice == ALL:
            return "every season"
        if self.choice == NEW:
            return "new episodes as they air"
        if self.choice == LATEST:
            return f"season {self.last}, the latest" if self.last else "the latest season"
        if self.first == self.last:
            return f"season {self.first}"
        return f"seasons {self.first} to {self.last}"


def decode(text: str | None) -> Seasons | None:
    """The short string form back into a choice, or None for nothing usable."""
    text = (text or "").strip()
    if not text:
        return None
    name, _, detail = text.partition(":")
    try:
        if name == RANGE:
            first, _, last = detail.partition("-")
            return _checked(RANGE, int(first), int(last))
        if name == LATEST:
            return Seasons(LATEST, 0, int(detail)) if detail else Seasons(LATEST)
    except ValueError:
        return None
    return Seasons(name) if name in (ALL, NEW) else None


def from_body(body) -> Seasons:
    """A choice from a request body. Raises ValueError with a sentence to show.

    Strict, because a choice this service misread would acquire something
    other than what was asked for, and the person would only find out when it
    arrived.
    """
    if not isinstance(body, dict):
        raise ValueError("Which seasons to ask for was not understood.")
    name = body.get("choice")
    if name not in CHOICES:
        raise ValueError("That is not a choice of seasons this server knows.")
    if name != RANGE:
        return Seasons(name)
    first, last = body.get("from"), body.get("to")
    if type(first) is not int or type(last) is not int:
        raise ValueError("A stretch of seasons needs a first and a last season.")
    return _checked(RANGE, first, last)


def _checked(name: str, first: int, last: int) -> Seasons:
    if not 1 <= first <= last <= HIGHEST_SEASON:
        raise ValueError(
            "The first season has to be 1 or more, and no later than the last.")
    return Seasons(name, first, last)
