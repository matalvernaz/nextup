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
check.equal(len(C.parse_results(reply(results=[RESULT]),
                                requested=["m002xv0m"])), 1,
            "one result out of a request reply")
check.equal(len(C.parse_statuses(reply(statuses=[STATUS], not_found=[]),
                                 requested=["m002xv0m"]).statuses), 1,
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

# --- ContractError is not a ValueError (so broad excepts don't eat it) ------

check.that(not issubclass(C.ContractError, ValueError),
           "ContractError does not subclass ValueError")
check.that(issubclass(C.ContractError, Exception),
           "ContractError is an Exception")

# --- Health (the probe's one read) ------------------------------------------

h = C.Health.from_json(reply(ok=True, tunnel_ok=False))
check.that(h.ok and not h.tunnel_ok, "health parses ok and tunnel_ok")
check.raises(C.ContractError,
             lambda: C.Health.from_json({"ok": True, "tunnel_ok": True}),
             "health with no schema version is a skew, not healthy")
check.raises(C.ContractError,
             lambda: C.Health.from_json(reply(ok="yes", tunnel_ok=True)),
             "health ok must be a bool")

# --- request_body: one entry per pid, each with its own key -----------------

body = C.request_body([("m1", "m1:k1", C.Reason.USER_REQUEST),
                       ("m2", "m2:k2", C.Reason.RECONCILE)])
check.equal(body["schema_version"], C.SCHEMA_VERSION,
            "request body carries the version")
check.equal([r["pid"] for r in body["requests"]], ["m1", "m2"],
            "one entry per pid")
check.equal(body["requests"][0]["attempt_key"], "m1:k1",
            "each pid keeps its own key")
check.equal(body["requests"][1]["reason"], "reconcile",
            "each pid keeps its own reason")
check.raises(C.ContractError, lambda: C.request_body([]),
             "an empty request batch is refused")
check.raises(C.ContractError,
             lambda: C.request_body([("", "k", C.Reason.USER_REQUEST)]),
             "a blank pid in a request is refused")
check.raises(C.ContractError,
             lambda: C.request_body([("m1", "", C.Reason.USER_REQUEST)]),
             "a blank attempt key in a request is refused")
check.raises(C.ContractError,
             lambda: C.request_body([("m1", "k1", C.Reason.USER_REQUEST),
                                     ("m1", "k2", C.Reason.USER_REQUEST)]),
             "the same pid twice in one request is refused")
check.raises(C.ContractError,
             lambda: C.request_body([("m1", "k1", "user_request")]),
             "a reason that is not a Reason is refused")

# --- parse_statuses: a strict partition against the asked-about pids ---------

part = C.parse_statuses(reply(statuses=[STATUS], not_found=["m2"]),
                        requested=["m002xv0m", "m2", "m3"])
check.equal(set(part.statuses), {"m002xv0m"}, "known statuses by pid")
check.equal(part.not_found, frozenset({"m2"}), "explicit not_found")
check.equal(part.unanswered, frozenset({"m3"}),
            "a pid in neither list is unanswered")
check.that(part.unanswered != part.not_found,
           "unanswered and not_found are distinct")

check.raises(C.ContractError,
             lambda: C.parse_statuses(
                 reply(statuses=[STATUS, STATUS], not_found=[]),
                 requested=["m002xv0m"]),
             "a duplicate status is refused")
check.raises(C.ContractError,
             lambda: C.parse_statuses(
                 reply(statuses=[STATUS], not_found=["m002xv0m"]),
                 requested=["m002xv0m"]),
             "a pid in both statuses and not_found is refused")
check.raises(C.ContractError,
             lambda: C.parse_statuses(reply(statuses=[STATUS], not_found=[]),
                                      requested=["other"]),
             "a status for a pid not asked about is refused")
check.raises(C.ContractError,
             lambda: C.parse_statuses(reply(statuses=[], not_found=["m2", "m2"]),
                                      requested=["m2"]),
             "a duplicate not_found is refused")
check.raises(C.ContractError,
             lambda: C.parse_statuses(reply(statuses=[], not_found=[""]),
                                      requested=["m2"]),
             "a blank not_found pid is refused")
check.raises(C.ContractError,
             lambda: C.parse_statuses(reply(statuses=[], not_found="m2"),
                                      requested=["m2"]),
             "not_found that is not a list is refused")

# --- parse_results: a request reply must cover exactly the batch -------------

results = C.parse_results(reply(results=[RESULT]), requested=["m002xv0m"])
check.equal(set(results), {"m002xv0m"}, "results keyed by pid")
check.raises(C.ContractError,
             lambda: C.parse_results(reply(results=[]), requested=["m002xv0m"]),
             "a request reply that drops a requested pid is refused")
check.raises(C.ContractError,
             lambda: C.parse_results(reply(results=[RESULT, RESULT]),
                                     requested=["m002xv0m"]),
             "a duplicate result is refused")
check.raises(C.ContractError,
             lambda: C.parse_results(reply(results=[RESULT]), requested=["other"]),
             "a result for a pid not requested is refused")
check.raises(C.ContractError,
             lambda: C.parse_results(reply(results="x"), requested=["m002xv0m"]),
             "results that are not a list are refused")

# --- parse_cancelled: a subset of what was asked, nothing unsolicited --------

cancelled = C.parse_cancelled(reply(cancelled=["m1", "m2"]),
                              requested=["m1", "m2", "m3"])
check.equal(cancelled, frozenset({"m1", "m2"}), "cancelled is the dropped subset")
check.raises(C.ContractError,
             lambda: C.parse_cancelled(reply(cancelled=["x"]), requested=["m1"]),
             "cancelling a pid not asked about is refused")
check.raises(C.ContractError,
             lambda: C.parse_cancelled(reply(cancelled=["m1", "m1"]),
                                       requested=["m1"]),
             "a duplicate cancelled is refused")
check.raises(C.ContractError,
             lambda: C.parse_cancelled(reply(cancelled=[""]), requested=["m1"]),
             "a blank cancelled pid is refused")
check.raises(C.ContractError,
             lambda: C.parse_cancelled(reply(cancelled="m1"), requested=["m1"]),
             "cancelled that is not a list is refused")

raise SystemExit(check.report())
