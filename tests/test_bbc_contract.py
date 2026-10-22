"""The bbc-ui <-> nextup wire contract parses, and rejects a skewed reply.

Pure data: no database and no network, so this imports harness only for the
repo path and the Check tally, and does not call setup(). The point of the file
is that a reply nextup cannot read is a loud ContractError at the edge, told
apart from a valid reply that merely carries an unavailable or failed outcome --
the distinction the request path leans on so a skew never refunds or re-downloads.
"""
import harness  # noqa: F401  (adds the repo root to sys.path)

from app.bbc import contract as C

check = harness.Check("bbc contract")

EP = {
    "pid": "m002xv0m", "programme_pid": "b006m8z1",
    "programme_title": "Casualty", "title": "Episode 7",
    "synopsis": "A busy shift.", "image_url": "https://ichef/p.jpg",
    "category": "Drama", "series_number": 49, "episode_number": 7,
    "described": "available", "available": True, "unavailable": None,
    "release_at": "2026-06-13T00:00:00Z",
    "first_broadcast_at": "2026-06-13T19:35:00Z",
}
PROG = {
    "pid": "b006m8z1", "title": "Casualty", "synopsis": "Medical drama.",
    "image_url": "https://ichef/c.jpg", "category": "Drama",
    "episode_count": 39, "described": "available",
}
RESULT = {"pid": "m002xv0m", "outcome": "accepted",
          "attempt_key": "m002xv0m:abc", "unavailable": None, "message": ""}
STATUS = {"pid": "m002xv0m", "attempt_key": "m002xv0m:abc",
          "state": "downloading", "unavailable": None, "message": ""}


def reply(**rows) -> dict:
    return {"schema_version": C.SCHEMA_VERSION, **rows}


# --- the happy path ----------------------------------------------------------

ep = C.Episode.from_json(EP)
check.equal(ep.pid, "m002xv0m", "episode pid")
check.equal(ep.described, C.Described.AVAILABLE, "episode described is the enum")
check.equal(ep.series_number, 49, "episode series number")
check.that(ep.available is True, "episode available is a real bool")
check.that(ep.unavailable is None, "available episode has no reason")

prog = C.Programme.from_json(PROG)
check.equal(prog.episode_count, 39, "programme episode count")
check.equal(prog.described, C.Described.AVAILABLE, "programme aggregate described")

res = C.RequestResult.from_json(RESULT)
check.equal(res.outcome, C.Outcome.ACCEPTED, "result outcome")
check.equal(res.attempt_key, "m002xv0m:abc", "result carries the attempt key")

pending = C.RequestResult.from_json(
    {**RESULT, "outcome": "already_pending", "attempt_key": "m002xv0m:owner"})
check.equal(pending.attempt_key, "m002xv0m:owner",
            "already_pending returns the canonical (owner's) key to adopt")

st = C.JobStatus.from_json(STATUS)
check.equal(st.state, C.JobState.DOWNLOADING, "status state")
check.that(not st.terminal, "downloading is not terminal")
check.that(C.JobStatus.from_json({**STATUS, "state": "failed",
                                  "unavailable": "upstream"}).terminal,
           "failed is terminal")
check.that(C.JobStatus.from_json({**STATUS, "state": "cancelled"}).terminal,
           "cancelled is terminal")

# Optional strings default, nullable numbers survive, reasons normalise.
lean = {k: EP[k] for k in ("pid", "programme_pid", "title", "described",
                           "available")}
lean = {**lean, "unavailable": None}
thin = C.Episode.from_json(lean)
check.equal(thin.synopsis, "", "missing optional string becomes ''")
check.that(thin.series_number is None, "absent series number is None")
for blank in (None, ""):
    check.that(
        C.Episode.from_json({**EP, "unavailable": blank}).unavailable is None,
        f"unavailable {blank!r} reads as no reason")
coming = C.Episode.from_json(
    {**EP, "available": False, "unavailable": "not_yet"})
check.equal(coming.unavailable, C.Unavailable.NOT_YET, "coming-soon reason")

# --- reply wrappers are version-checked ------------------------------------

check.equal(len(C.episodes_from_reply(reply(episodes=[EP, EP]))), 2,
            "two episodes out of a catalogue reply")
check.equal(len(C.programmes_from_reply(reply(programmes=[PROG]))), 1,
            "one programme out of a catalogue reply")
check.equal(len(C.results_from_reply(reply(results=[RESULT]))), 1,
            "one result out of a request reply")
check.equal(len(C.statuses_from_reply(reply(statuses=[STATUS]))), 1,
            "one status out of a status reply")

# --- a skewed or malformed reply is a ContractError, not a KeyError --------

check.raises(C.ContractError,
             lambda: C.episodes_from_reply({"schema_version": 999,
                                            "episodes": [EP]}),
             "a future schema version is refused")
check.raises(C.ContractError,
             lambda: C.episodes_from_reply({"episodes": [EP]}),
             "a reply with no schema version is refused")
check.raises(C.ContractError,
             lambda: C.episodes_from_reply(reply(episodes="not a list")),
             "episodes that are not a list is refused")
check.raises(C.ContractError, lambda: C.check_version("not an object"),
             "a non-object reply is refused")

# --- per-field guards -------------------------------------------------------

check.raises(C.ContractError, lambda: C.Episode.from_json({**EP, "described": "yes"}),
             "an unknown described value is refused")
check.raises(C.ContractError, lambda: C.Episode.from_json({**EP, "unavailable": "banned"}),
             "an unknown unavailable reason is refused")
check.raises(C.ContractError, lambda: C.Episode.from_json({k: v for k, v in EP.items() if k != "pid"}),
             "a missing required field is refused")
check.raises(C.ContractError, lambda: C.Episode.from_json({**EP, "available": "true"}),
             "available as a string is refused")
check.raises(C.ContractError, lambda: C.Episode.from_json({**EP, "series_number": True}),
             "a boolean series number is refused")
check.raises(C.ContractError, lambda: C.Programme.from_json({**PROG, "episode_count": True}),
             "a boolean episode count is refused")
check.raises(C.ContractError, lambda: C.Programme.from_json({**PROG, "episode_count": -1}),
             "a negative episode count is refused")
check.raises(C.ContractError, lambda: C.Episode.from_json("not an object"),
             "an episode that is not an object is refused")

# outcome and reason must agree.
check.raises(C.ContractError,
             lambda: C.RequestResult.from_json({**RESULT, "outcome": "unavailable"}),
             "an unavailable outcome with no reason is refused")
check.raises(C.ContractError,
             lambda: C.RequestResult.from_json(
                 {**RESULT, "unavailable": "drm"}),
             "an accepted outcome carrying a reason is refused")
check.raises(C.ContractError,
             lambda: C.JobStatus.from_json({**STATUS, "state": "failed"}),
             "a failed status with no reason is refused")
check.raises(C.ContractError,
             lambda: C.RequestResult.from_json({**RESULT, "outcome": "lost"}),
             "an unknown outcome is refused")
check.raises(C.ContractError,
             lambda: C.JobStatus.from_json({**STATUS, "state": "paused"}),
             "an unknown job state is refused")

# --- blank required strings (GPT round 1) ----------------------------------

check.raises(C.ContractError, lambda: C.Episode.from_json({**EP, "pid": ""}),
             "a blank pid is refused")
check.raises(C.ContractError, lambda: C.Episode.from_json({**EP, "pid": "   "}),
             "a whitespace pid is refused")
check.raises(C.ContractError,
             lambda: C.RequestResult.from_json({**RESULT, "attempt_key": ""}),
             "an accepted result with a blank attempt key is refused")
check.raises(C.ContractError,
             lambda: C.RequestResult.from_json({**RESULT, "attempt_key": "  "}),
             "an accepted result with a whitespace attempt key is refused")

# A result with no job behind it sends no key, and that is allowed.
owned = C.RequestResult.from_json(
    {"pid": "m002xv0m", "outcome": "already_owned", "attempt_key": "",
     "unavailable": None, "message": "Already here."})
check.equal(owned.attempt_key, "", "already_owned carries no attempt key")
gone = C.RequestResult.from_json(
    {"pid": "m002xv0m", "outcome": "unavailable", "attempt_key": "",
     "unavailable": "not_found", "message": ""})
check.equal(gone.unavailable, C.Unavailable.NOT_FOUND,
            "an unavailable result needs no job key")

# --- the version is an int, not True or 1.0 (Sonnet round 1) ----------------

check.raises(C.ContractError,
             lambda: C.episodes_from_reply({"schema_version": True,
                                            "episodes": [EP]}),
             "schema_version True is not version 1")
check.raises(C.ContractError,
             lambda: C.episodes_from_reply({"schema_version": 1.0,
                                            "episodes": [EP]}),
             "schema_version 1.0 is not version 1")

# --- an episode's availability and its reason must agree (Sonnet round 1) ---

check.raises(C.ContractError,
             lambda: C.Episode.from_json({**EP, "available": True,
                                          "unavailable": "drm"}),
             "an available episode carrying a reason is refused")
check.raises(C.ContractError,
             lambda: C.Episode.from_json({**EP, "available": False,
                                          "unavailable": None}),
             "an unavailable episode with no reason is refused")

# --- a status reason only with failed (Gemini + Sonnet round 1) -------------

check.raises(C.ContractError,
             lambda: C.JobStatus.from_json({**STATUS, "state": "downloading",
                                            "unavailable": "drm"}),
             "a downloading status carrying a reason is refused")

raise SystemExit(check.report())
