"""The wire shape bbc-ui and nextup agree on, written once.

Three programs depend on this shape: bbc-ui produces it, nextup consumes it, and
EchoFin renders what nextup passes on. A field renamed in one and not the others
is the break that passes every unit test and fails only against the running
pair -- so the shape lives in one file, carries a version, and nextup's contract
test checks a recorded bbc-ui reply against it. EchoFin mirrors the enum values
and the version below; it does not import this module, so the string values are
the contract, not the Python names.

Keys are snake_case because this is Python talking to Python over the Docker
network. nextup maps to camelCase at its own API edge, where EchoFin reads it;
that mapping is api.py's job, not this file's.
"""
import enum
from dataclasses import dataclass

#: Bumped on any change to the shapes below. bbc-ui stamps every reply with it;
#: nextup refuses a reply whose version it does not know rather than mis-reading
#: a half-deployed peer, and the contract test fails when a fixture predates a
#: bump. One integer, not semver: there is one producer and one consumer, and a
#: shape either matches or it does not.
SCHEMA_VERSION = 1


class ContractError(ValueError):
    """A bbc-ui reply could not be read as this contract.

    Its own type so the client can tell "bbc-ui answered something I cannot
    read" -- a deploy skew, an outage to log loudly and, on a read path, to
    treat as "state unknown" rather than failure -- from an ordinary per-item
    outcome ("this episode is unavailable") carried inside a perfectly valid
    reply. The read paths in wants.py lean on that distinction: a ContractError
    must never refund a request or trigger a re-download.
    """


class Described(str, enum.Enum):
    """Whether a BBC-made audio-described version exists.

    Three states, never a boolean, because "not looked up" must not render as
    "not described": that would tell a blind listener a described programme is
    not one. AVAILABLE is the BBC's own human description, always preferred; an
    episode that is NONE is where describarr's AI description is the fallback,
    never the first choice.
    """
    AVAILABLE = "available"   #: a BBC audio-described version exists to fetch
    NONE = "none"             #: no described version on iPlayer
    UNKNOWN = "unknown"       #: not determined (the episode detail was not read)


class Unavailable(str, enum.Enum):
    """Why one episode cannot be fetched, carried so the reason can be shown.

    nextup turns one of these into a single plain sentence instead of a retry
    that never succeeds. DRM and GEO are walls; EXPIRED and NOT_YET are time,
    not failure -- an expired episode can return, and a not-yet one has a date,
    so neither is hidden from the browse surface, only from the request action.
    """
    DRM = "drm"               #: encrypted, the relay cannot play it (most films)
    EXPIRED = "expired"       #: the availability window has closed
    NOT_YET = "not_yet"       #: announced, not yet available to download
    GEO = "geo"               #: not offered to the tunnel's region
    NOT_FOUND = "not_found"   #: no such programme id in the catalogue
    UPSTREAM = "upstream"     #: iPlayer or get_iplayer errored


class Outcome(str, enum.Enum):
    """What became of asking bbc-ui for one episode."""
    ACCEPTED = "accepted"                #: a new download was queued
    ALREADY_OWNED = "already_owned"      #: already in the shared library
    ALREADY_PENDING = "already_pending"  #: a download is already queued/running
    UNAVAILABLE = "unavailable"          #: cannot be fetched; see `unavailable`


class JobState(str, enum.Enum):
    """Where a download has got to, as bbc-ui reports it.

    QUEUED through IMPORTED are live or finishing; a request stays "pending" to
    a listener across all of them, including IMPORTED, because the file is on
    disk but Jellyfin may not have rescanned yet and ownership is read from
    Jellyfin, not from here. FAILED and CANCELLED are terminal. CANCELLED is its
    own state so a stopped job does not read as "no record", which the reconcile
    path would otherwise take as a cue to ask for it all over again.
    """
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"      #: downloaded, handed to the library
    IMPORTED = "imported"        #: in the library tree; may await a Jellyfin scan
    FAILED = "failed"            #: terminal; `unavailable` says why
    CANCELLED = "cancelled"      #: terminal; stopped on purpose


class Reason(str, enum.Enum):
    """Why nextup is asking for a pid, which gates the history-clear path.

    RECONCILE is nextup catching up after a lost response and must never cause a
    second download: for a pid bbc-ui still has history or a file for, a
    reconcile ask is answered already_pending/imported, not re-fetched.
    USER_REQUEST is a person asking on purpose and is the only reason allowed to
    clear a spent history entry to re-download something deleted from disk.
    """
    USER_REQUEST = "user_request"
    RECONCILE = "reconcile"


def _as_object(d: object, what: str) -> dict:
    if not isinstance(d, dict):
        raise ContractError(f"{what} is {type(d).__name__}, not an object")
    return d


def _str(d: dict, key: str, required: bool = True) -> str:
    if key not in d:
        if required:
            raise ContractError(f"missing {key!r}")
        return ""
    value = d[key]
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ContractError(f"{key!r} is {type(value).__name__}, not a string")
    if required and not value.strip():
        # A blank required string is worse than a missing one: a blank pid or
        # attempt key passes every later check while naming nothing, which is
        # exactly the confusion the attempt key exists to prevent.
        raise ContractError(f"{key!r} must not be blank")
    return value


def _bool(d: dict, key: str) -> bool:
    if key not in d:
        raise ContractError(f"missing {key!r}")
    value = d[key]
    if not isinstance(value, bool):
        raise ContractError(f"{key!r} is {value!r}, not true or false")
    return value


def _count(d: dict, key: str) -> int:
    value = d.get(key)
    # bool is an int subclass; a True count is a bug, not a one.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(f"{key!r} is {value!r}, not a count")
    return value


def _int_or_none(d: dict, key: str) -> int | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{key!r} is {value!r}, not a whole number")
    return value


def _member(kind, d: dict, key: str):
    raw = _str(d, key)
    try:
        return kind(raw)
    except ValueError:
        allowed = ", ".join(m.value for m in kind)
        raise ContractError(
            f"{key!r} is {raw!r}, not one of {allowed}") from None


def _reason_or_none(d: dict, key: str) -> "Unavailable | None":
    """An Unavailable reason that may be absent, '' or null -- all meaning none."""
    raw = d.get(key)
    if raw in (None, ""):
        return None
    try:
        return Unavailable(raw)
    except ValueError:
        allowed = ", ".join(m.value for m in Unavailable)
        raise ContractError(
            f"{key!r} is {raw!r}, not one of {allowed}") from None


@dataclass(frozen=True, slots=True)
class Episode:
    """One episode, keyed on its BBC programme id (the pid everything shares).

    `described` is authoritative at the episode; the aggregate on the programme
    is only a hint for a browse row. `available` is whether it can be fetched
    right now, and `unavailable` names why not when it cannot (None when it can).
    A coming-soon episode is `available=False`, `unavailable=NOT_YET`, with its
    date in `release_at` -- shown in browse, its request action disabled.
    """
    pid: str
    programme_pid: str
    programme_title: str
    title: str
    synopsis: str
    image_url: str
    category: str
    series_number: int | None
    episode_number: int | None
    described: Described
    available: bool
    unavailable: "Unavailable | None"
    #: ISO 8601, when it was or will be available; "" when iPlayer gave none.
    release_at: str
    first_broadcast_at: str

    @classmethod
    def from_json(cls, d: object) -> "Episode":
        d = _as_object(d, "episode")
        available = _bool(d, "available")
        unavailable = _reason_or_none(d, "unavailable")
        # The browse surface enables or disables the request action from exactly
        # these two fields, so they must agree: an available episode has no
        # reason, and an unavailable one always names why (a coming-soon episode
        # is NOT_YET, not a bare `available=False`).
        if available and unavailable is not None:
            raise ContractError(
                "an available episode carries an unavailable reason")
        if not available and unavailable is None:
            raise ContractError("an unavailable episode needs a reason")
        return cls(
            pid=_str(d, "pid"),
            programme_pid=_str(d, "programme_pid"),
            programme_title=_str(d, "programme_title", required=False),
            title=_str(d, "title"),
            synopsis=_str(d, "synopsis", required=False),
            image_url=_str(d, "image_url", required=False),
            category=_str(d, "category", required=False),
            series_number=_int_or_none(d, "series_number"),
            episode_number=_int_or_none(d, "episode_number"),
            described=_member(Described, d, "described"),
            available=available,
            unavailable=unavailable,
            release_at=_str(d, "release_at", required=False),
            first_broadcast_at=_str(d, "first_broadcast_at", required=False),
        )


@dataclass(frozen=True, slots=True)
class Programme:
    """A brand or series, the unit a browse row shows.

    `described` here is an AGGREGATE, documented loudly because it is easy to
    misread: AVAILABLE means at least one episode has a BBC described version,
    NOT that every one does. A client shows "some episodes audio described" for
    an AVAILABLE programme whose episodes are mixed, and the per-episode truth is
    on `Episode.described`. `episode_count` is the catalogue's count, which can
    exceed what is fetchable now.
    """
    pid: str
    title: str
    synopsis: str
    image_url: str
    category: str
    episode_count: int
    #: Aggregate only: AVAILABLE = has at least one described episode.
    described: Described

    @classmethod
    def from_json(cls, d: object) -> "Programme":
        d = _as_object(d, "programme")
        return cls(
            pid=_str(d, "pid"),
            title=_str(d, "title"),
            synopsis=_str(d, "synopsis", required=False),
            image_url=_str(d, "image_url", required=False),
            category=_str(d, "category", required=False),
            episode_count=_count(d, "episode_count"),
            described=_member(Described, d, "described"),
        )


@dataclass(frozen=True, slots=True)
class RequestResult:
    """What bbc-ui did with one requested pid.

    `attempt_key` is the CANONICAL key for the job behind this pid, which is not
    always the key the caller sent: an `already_pending` result returns the key
    of the job already running, and the caller adopts it so its own
    reconciliation tracks the shared job rather than waiting on a key no job
    carries. An `already_owned` or `unavailable` result has no job behind it and
    sends `attempt_key` as "". `unavailable` is set only with outcome UNAVAILABLE.
    """
    pid: str
    outcome: Outcome
    attempt_key: str
    unavailable: "Unavailable | None"
    message: str

    @classmethod
    def from_json(cls, d: object) -> "RequestResult":
        d = _as_object(d, "result")
        outcome = _member(Outcome, d, "outcome")
        unavailable = _reason_or_none(d, "unavailable")
        if outcome is Outcome.UNAVAILABLE and unavailable is None:
            raise ContractError("outcome 'unavailable' with no reason")
        if outcome is not Outcome.UNAVAILABLE and unavailable is not None:
            raise ContractError(
                f"outcome {outcome.value!r} carries an unavailable reason")
        # A key only where there is a job: accepted queued one, already_pending
        # names the job to join. already_owned and unavailable have none, and
        # requiring a non-blank key for them would fail a perfectly good reply.
        attempt_key = _str(d, "attempt_key", required=False)
        if (outcome in (Outcome.ACCEPTED, Outcome.ALREADY_PENDING)
                and not attempt_key.strip()):
            raise ContractError(f"outcome {outcome.value!r} needs an attempt_key")
        return cls(
            pid=_str(d, "pid"),
            outcome=outcome,
            attempt_key=attempt_key,
            unavailable=unavailable,
            message=_str(d, "message", required=False),
        )


@dataclass(frozen=True, slots=True)
class JobStatus:
    """Where one pid's download has got to, for reconciliation.

    `attempt_key` is the key of the job bbc-ui is reporting on. nextup compares
    it to the key on its own want row: a status for an OLDER attempt is stale
    and must not settle or refund a newer intent. `unavailable` is set only with
    state FAILED.
    """
    pid: str
    attempt_key: str
    state: JobState
    unavailable: "Unavailable | None"
    message: str

    @property
    def terminal(self) -> bool:
        """Whether no further change will come without a new request."""
        return self.state in (JobState.FAILED, JobState.CANCELLED)

    @classmethod
    def from_json(cls, d: object) -> "JobStatus":
        d = _as_object(d, "status")
        state = _member(JobState, d, "state")
        unavailable = _reason_or_none(d, "unavailable")
        if state is JobState.FAILED and unavailable is None:
            raise ContractError("state 'failed' with no reason")
        if state is not JobState.FAILED and unavailable is not None:
            raise ContractError(
                f"state {state.value!r} carries an unavailable reason")
        return cls(
            pid=_str(d, "pid"),
            attempt_key=_str(d, "attempt_key"),
            state=state,
            unavailable=unavailable,
            message=_str(d, "message", required=False),
        )


def check_version(d: object) -> dict:
    """Return a version-checked reply body, or raise ContractError.

    The first thing read of any bbc-ui reply, so a deploy skew is one clear
    error at the edge rather than a KeyError three layers in. Returns the body
    so the row helpers can parse from a dict already known to match.
    """
    d = _as_object(d, "reply")
    version = d.get("schema_version")
    # `type(...) is not int` rejects True (which equals 1) and 1.0 as versions,
    # the same bool/int trap `_count` guards against.
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ContractError(
            f"bbc-ui replied schema_version {version!r}; this nextup speaks "
            f"{SCHEMA_VERSION}. The two are deployed out of step.")
    return d


def _rows(d: object, key: str) -> list:
    body = check_version(d)
    rows = body.get(key)
    if not isinstance(rows, list):
        raise ContractError(f"{key!r} is not a list")
    return rows


def episodes_from_reply(d: object) -> list[Episode]:
    """Episodes out of a version-checked catalogue reply."""
    return [Episode.from_json(row) for row in _rows(d, "episodes")]


def programmes_from_reply(d: object) -> list[Programme]:
    """Programmes out of a version-checked catalogue reply."""
    return [Programme.from_json(row) for row in _rows(d, "programmes")]


def results_from_reply(d: object) -> list[RequestResult]:
    """Per-pid outcomes out of a version-checked request reply."""
    return [RequestResult.from_json(row) for row in _rows(d, "results")]


def statuses_from_reply(d: object) -> list[JobStatus]:
    """Per-pid job states out of a version-checked status reply."""
    return [JobStatus.from_json(row) for row in _rows(d, "statuses")]
