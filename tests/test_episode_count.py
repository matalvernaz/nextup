"""What "how many episodes of this are here" counts, and what it leaves out.

This number exists to be compared with Sonarr's aired total, and Sonarr's
aired total counts no specials at all. Counting them here made a whole-series
request close while episodes somebody had asked for were still missing: six
series on the deployment this was found on hold specials, one of them
thirty-three, and each of those is a free episode towards a total that never
included them.
"""
import httpx

import harness

harness.setup()

from app import jellyfin, sonarr  # noqa: E402

check = harness.Check("episode count")


class Response:
    def __init__(self, body, error=None):
        self.body = body
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error
        return self

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Client:
    """Answers the two counting endpoints and records what it was asked."""

    def __init__(self, total, specials):
        self.total = total
        self.specials = specials
        self.asked = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def get(self, path, params=None):
        self.asked.append((path, (params or {}).get("season")))
        answer = self.specials if path.startswith("/Shows/") else self.total
        if isinstance(answer, Response):
            return answer
        return Response({"TotalRecordCount": answer})


def use(total, specials):
    client = Client(total, specials)
    jellyfin._client = lambda: client
    return client


# --- specials do not count towards a total that excludes them ---------------
client = use(total=154, specials=33)
check.equal(jellyfin.episode_count("series-1"), 121,
            "the count is what is here minus what Sonarr never counted")
check.equal(client.asked,
            [("/Items", None), ("/Shows/series-1/Episodes", 0)],
            "asked as two totals rather than one listing of the series: the "
            "listing is 109 kB against 100 bytes for the pair")

# --- and a series with none of them is unchanged ----------------------------
use(total=136, specials=0)
check.equal(jellyfin.episode_count("series-2"), 136,
            "a series with no specials counts exactly what it holds")

# --- the failure this exists to stop ----------------------------------------
#
# Fairy Tail on the live server: 33 specials in Jellyfin, and Sonarr's aired
# total counts none of them. With specials included the request closes 33
# episodes early; without them it stays open, which is the truth.
AIRED_TOTAL = 175
use(total=175, specials=33)
count = jellyfin.episode_count("series-3")
check.that(count is not None and count < AIRED_TOTAL,
           "a series still missing episodes does not reach Sonarr's total "
           "just because specials made up the difference")
progress = sonarr.AcquisitionProgress(AIRED_TOTAL, 0)
check.that(not (count is not None and progress.episodes_total is not None
                and count >= progress.episodes_total),
           "so the whole-series request stays open")

# --- unknown stays unknown, on either half ----------------------------------
#
# Zero is a real answer and unknown is not. Reporting the undiscounted total
# when the discount cannot be had would be the very over-count above.
use(total=Response(None, error=httpx.ConnectError("no route")), specials=0)
check.equal(jellyfin.episode_count("series-4"), None,
            "a total that cannot be had is unknown, not zero")

use(total=154, specials=Response(None, error=httpx.ConnectError("no route")))
check.equal(jellyfin.episode_count("series-5"), None,
            "and so is a total whose specials cannot be counted")

use(total=154, specials=Response({"TotalRecordCount": "many"}))
check.equal(jellyfin.episode_count("series-6"), None,
            "a count that is not a number is not a count")

use(total=10, specials=99)
check.equal(jellyfin.episode_count("series-7"), 0,
            "and nothing ever counts below zero, however the two disagree")

check.equal(jellyfin.episode_count(""), None,
            "a series Jellyfin has no item for is not asked about at all")

harness.cleanup()
raise SystemExit(check.report())
