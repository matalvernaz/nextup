"""How much of a podcast to fetch: the choice, its spellings, and its refusals."""
import harness

harness.setup()

from app import episodes  # noqa: E402

check = harness.Check("episodes")

# --- the three answers round-trip through their short form -------------------
for text, expected in (("all", episodes.Episodes("all")),
                       ("new", episodes.Episodes("new")),
                       ("latest:5", episodes.Episodes("latest", 5)),
                       ("latest:12", episodes.Episodes("latest", 12)),
                       ("latest", episodes.Episodes("latest", episodes.DEFAULT_LATEST_COUNT))):
    check.equal(episodes.decode(text), expected, f"{text!r} decodes")
check.equal(episodes.Episodes("latest", 7).encode(), "latest:7", "the newest few carries its count")
check.equal(episodes.Episodes("latest").encode(), f"latest:{episodes.DEFAULT_LATEST_COUNT}",
            "the newest few without a count encodes the default, so the ledger never holds an open question")
check.equal(episodes.Episodes("all").as_json(), {"choice": "all"}, "every episode as JSON")
check.equal(episodes.Episodes("latest", 3).as_json(), {"choice": "latest", "count": 3},
            "the newest few as JSON carries its count")

# --- nothing usable is None, never a guess ------------------------------------
for text in ("", None, "some", "latest:many", "latest:0", "latest:1000", "range:1-3"):
    check.equal(episodes.decode(text), None, f"{text!r} is not a choice")

# --- a request body is read strictly --------------------------------------------
check.equal(episodes.from_body({"choice": "new"}), episodes.Episodes("new"), "new from a body")
check.equal(episodes.from_body({"choice": "latest"}),
            episodes.Episodes("latest", episodes.DEFAULT_LATEST_COUNT),
            "the newest few without a count takes the default")
check.equal(episodes.from_body({"choice": "latest", "count": 10}),
            episodes.Episodes("latest", 10), "and with one, that count")
check.raises(ValueError, lambda: episodes.from_body({"choice": "everything"}),
             "an unknown choice is refused")
check.raises(ValueError, lambda: episodes.from_body({"choice": "latest", "count": "5"}),
             "a count that is not a whole number is refused")
check.raises(ValueError, lambda: episodes.from_body({"choice": "latest", "count": 0}),
             "zero of the newest is refused")
check.raises(ValueError, lambda: episodes.from_body("all"), "a bare string is refused")

# --- the phrase a list of requests uses ------------------------------------------
check.equal(episodes.Episodes("all").phrase(), "every episode", "phrase: all")
check.equal(episodes.Episodes("new").phrase(), "new episodes as they come out", "phrase: new")
check.that("newest 5" in episodes.Episodes("latest", 5).phrase(), "phrase: latest names the count")

harness.cleanup()
raise SystemExit(check.report())
