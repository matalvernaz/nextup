"""How much of a podcast to fetch when it is asked for.

Three answers, because they are the three things somebody means: everything
it has ever published, only what it publishes from now on, or the newest few
to start with. Nothing at all is not one of them. A podcast asked for with
nothing to fetch never arrives, so the request would read "on its way" for
good.

The same shape as `seasons`: a choice travels in a request body and in
`/capabilities` as a JSON object, and in the ledger and an account's settings
as one short string (`all`, `new`, `latest:5`). Nobody is given a default by
the code. Whoever has not chosen is asked before their first podcast, the
answers are counted on the accounts page, and `PODCAST_EPISODES_DEFAULT` is
how the most common one becomes the default later.
"""
from typing import NamedTuple

ALL = "all"
NEW = "new"
LATEST = "latest"
CHOICES = (ALL, LATEST, NEW)

#: The per-account setting that holds somebody's usual choice.
SETTING = "PODCAST_EPISODES"

#: How many "the newest few" is when the choice does not say. Five is a week
#: of a daily show and a season's worth of most others.
DEFAULT_LATEST_COUNT = 5

#: A bound keeps a typo from reading as a request for a thousand episodes.
HIGHEST_LATEST_COUNT = 100


class Episodes(NamedTuple):
    """One choice. `count` is how many for the newest few, and 0 otherwise."""

    choice: str
    count: int = 0

    def encode(self) -> str:
        if self.choice == LATEST:
            return f"{LATEST}:{self.count or DEFAULT_LATEST_COUNT}"
        return self.choice

    def as_json(self) -> dict:
        out: dict = {"choice": self.choice}
        if self.choice == LATEST:
            out["count"] = self.count or DEFAULT_LATEST_COUNT
        return out

    def phrase(self) -> str:
        """What was asked for, to finish a sentence: "Asked for ___"."""
        if self.choice == ALL:
            return "every episode"
        if self.choice == NEW:
            return "new episodes as they come out"
        count = self.count or DEFAULT_LATEST_COUNT
        return f"the newest {count} episodes, then new ones as they come out"


def decode(text: str | None) -> Episodes | None:
    """The short string form back into a choice, or None for nothing usable."""
    text = (text or "").strip()
    if not text:
        return None
    name, _, detail = text.partition(":")
    if name == LATEST:
        if not detail:
            return Episodes(LATEST, DEFAULT_LATEST_COUNT)
        try:
            return _checked(int(detail))
        except ValueError:
            return None
    return Episodes(name) if name in (ALL, NEW) else None


def from_body(body) -> Episodes:
    """A choice from a request body. Raises ValueError with a sentence to show.

    Strict, because a choice this service misread would fetch a whole back
    catalogue somebody only wanted the newest few of, or nothing at all.
    """
    if not isinstance(body, dict):
        raise ValueError("How much of the podcast to fetch was not understood.")
    name = body.get("choice")
    if name not in CHOICES:
        raise ValueError("That is not a choice of episodes this server knows.")
    if name != LATEST:
        return Episodes(name)
    count = body.get("count", DEFAULT_LATEST_COUNT)
    if type(count) is not int:
        raise ValueError("The newest few needs a whole number of episodes.")
    return _checked(count)


def _checked(count: int) -> Episodes:
    if not 1 <= count <= HIGHEST_LATEST_COUNT:
        raise ValueError(
            f"The newest few has to be between 1 and {HIGHEST_LATEST_COUNT} episodes.")
    return Episodes(LATEST, count)
