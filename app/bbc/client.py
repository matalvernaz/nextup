"""The HTTP client nextup uses to talk to bbc-ui.

Thin on purpose. It does the shared-secret handshake, the timeouts, and the one
job that matters here: turning every network and protocol failure into one of
two exceptions the callers understand.

* `BbcUnavailable` -- bbc-ui could not be reached, or refused or could not be
  read. A read may be retried freely; a write that raises this did NOT take
  effect (nothing was sent, or bbc-ui answered 4xx), so the caller may leave
  things as they were.
* `BbcAmbiguous` (a subclass) -- a write may have reached bbc-ui but its outcome
  was lost: a read timeout, a dropped connection mid-flight, a 5xx, or a 2xx
  whose body would not parse. The caller must NOT refund or resend blindly; it
  reconciles against status instead. Being a subclass means the safe default
  (treat as an outage, change nothing) holds wherever only `BbcUnavailable` is
  caught.

Parsing is `contract`'s job; this module never reads a field itself.
"""
import httpx

from .. import config, logs
from . import contract

log = logs.get("bbc")

#: One timeout for every call, so the client and the backends probe cannot
#: drift. Short: a browse or a status read happens while somebody waits, and a
#: bbc-ui that takes longer than this to answer is not usable anyway.
TIMEOUT = httpx.Timeout(10.0, connect=3.0)

#: The header the shared secret travels in. A header, never the URL or a query
#: string, so it cannot land in an access log.
TOKEN_HEADER = "X-Bbc-Token"


class BbcUnavailable(Exception):
    """bbc-ui could not be reached, or answered something unusable.

    For a write, raising this means the request did not take effect, so nothing
    needs undoing.
    """


class BbcAmbiguous(BbcUnavailable):
    """A write may have reached bbc-ui, but its outcome was lost.

    The caller reconciles against status rather than refunding or resending. A
    subclass of `BbcUnavailable` so code that only wants "is it an outage" is
    right without having to know the difference.
    """


def _base() -> str:
    url = (config.BBC_UI_URL or "").rstrip("/")
    if not url:
        # Not configured at all. configured() gates the medium, so a caller that
        # reaches here is a bug or a race; an outage is the safe reading.
        raise BbcUnavailable("bbc-ui is not configured (no BBC_UI_URL).")
    return url


def _auth_headers() -> dict:
    """Headers for every endpoint but health, fail-closed on a missing secret.

    An unset token means refuse to call rather than call bbc-ui without the
    shared secret: the docker.sock mount makes an unauthenticated bbc-ui a way to
    drive get_iplayer, so a half-configured install must not reach it at all.
    Raised as an outage so a caller holds its request rather than crashing.
    """
    token = config.BBC_UI_TOKEN
    if not token:
        raise BbcUnavailable(
            "bbc-ui is configured without its shared secret (BBC_UI_TOKEN); "
            "refusing to call it unauthenticated.")
    return {TOKEN_HEADER: token, "Accept": "application/json"}


# --- reads: any failure is a plain outage, safe to retry --------------------

def _read(path: str, params: dict) -> dict:
    """GET an authenticated endpoint and return its JSON, or raise BbcUnavailable."""
    headers = _auth_headers()
    try:
        resp = httpx.get(_base() + path, params=params, headers=headers,
                         timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise BbcUnavailable(
            f"bbc-ui could not be reached: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        log.error("bbc-ui refused the shared secret on %s (%d)",
                  path, resp.status_code)
        raise BbcUnavailable(
            f"bbc-ui refused the shared secret ({resp.status_code}).")
    if resp.status_code != 200:
        raise BbcUnavailable(f"bbc-ui answered {resp.status_code} on {path}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise BbcUnavailable(
            "bbc-ui answered a body that is not JSON.") from exc


def health() -> contract.Health:
    """bbc-ui's liveness. The one call that needs no shared secret.

    A 200 with a schema this nextup cannot read is unhealthy, not healthy, which
    is why it parses through the contract rather than trusting the status code.
    """
    try:
        resp = httpx.get(_base() + "/api/health",
                         headers={"Accept": "application/json"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise BbcUnavailable(
            f"bbc-ui health could not be reached: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise BbcUnavailable(f"bbc-ui health answered {resp.status_code}.")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise BbcUnavailable("bbc-ui health is not JSON.") from exc
    try:
        return contract.Health.from_json(payload)
    except contract.ContractError as exc:
        raise BbcUnavailable(
            f"bbc-ui health is a schema this nextup cannot read: {exc}") from exc


def _catalogue(params: dict, key: str, row_from_json):
    """Parse a catalogue reply, skipping and logging a bad row.

    A single bad row must not lose a whole shelf, but a reply whose rows are ALL
    unreadable is an outage, not an empty shelf -- otherwise a bbc-ui bug would
    read as "nothing new". A genuinely empty reply (no rows) stays empty.
    """
    reply = _read("/api/catalogue", params)
    try:
        body = contract.check_version(reply)
    except contract.ContractError as exc:
        raise BbcUnavailable(
            f"bbc-ui catalogue is a schema this nextup cannot read: {exc}") from exc
    rows = body.get(key)
    if not isinstance(rows, list):
        raise BbcUnavailable(f"bbc-ui catalogue {key!r} is not a list.")
    parsed = []
    skipped = 0
    for row in rows:
        try:
            parsed.append(row_from_json(row))
        except contract.ContractError as exc:
            skipped += 1
            log.warning("skipping a bad bbc-ui %s row: %s", key, exc)
    if rows and not parsed:
        raise BbcUnavailable(
            f"every {key} row from bbc-ui was unreadable ({skipped}).")
    if skipped:
        log.warning("bbc-ui catalogue: skipped %d of %d %s rows",
                    skipped, len(rows), key)
    return parsed


def programmes(view: str, query: str = "",
               category: str = "") -> list[contract.Programme]:
    """Browse rows: `view` is new / coming / category / search.

    `query` is the search term, `category` the category to browse; each is sent
    only when it has a value, so bbc-ui never has to tell "" from absent.
    """
    params = {"view": view}
    if query:
        params["q"] = query
    if category:
        params["category"] = category
    return _catalogue(params, "programmes", contract.Programme.from_json)


def episodes(programme_pid: str) -> list[contract.Episode]:
    """The episodes of one programme, newest usable first (bbc-ui's order)."""
    return _catalogue({"view": "episodes", "programme": programme_pid},
                      "episodes", contract.Episode.from_json)


#: How many pids one status GET asks about. Reconciliation can name every
#: pending pid at once and a query string has a length limit, so a long list is
#: chunked rather than risking a truncated URL.
_STATUS_BATCH = 50


def status(pids) -> contract.StatusPartition:
    """Where each requested pid's download has got to, partitioned strictly.

    Deduplicated, and asked in bounded batches so a long reconcile list cannot
    overflow the query string. An empty ask makes no call at all.
    """
    unique = list(dict.fromkeys(pids))
    if not unique:
        return contract.StatusPartition({}, frozenset(), frozenset())
    statuses: dict[str, contract.JobStatus] = {}
    not_found: set[str] = set()
    unanswered: set[str] = set()
    for start in range(0, len(unique), _STATUS_BATCH):
        chunk = unique[start:start + _STATUS_BATCH]
        reply = _read("/api/status", {"pids": ",".join(chunk)})
        try:
            part = contract.parse_statuses(reply, chunk)
        except contract.ContractError as exc:
            raise BbcUnavailable(
                f"bbc-ui status is a schema this nextup cannot read: {exc}") from exc
        statuses.update(part.statuses)
        not_found |= part.not_found
        unanswered |= part.unanswered
    return contract.StatusPartition(statuses, frozenset(not_found),
                                    frozenset(unanswered))


# --- writes: a failure may mean "may have landed", which is BbcAmbiguous -----

def _write(path: str, body: dict) -> dict:
    """POST to bbc-ui, classifying a failure by whether the POST could have landed.

    Pre-send failures (could not connect) are plain outages. Everything after the
    bytes may have left -- a read timeout, a protocol error, a 5xx, a 2xx that
    will not parse -- is ambiguous. A 4xx (including 401/403) is a clean refusal:
    bbc-ui said no and queued nothing.
    """
    headers = _auth_headers()
    base = _base()
    try:
        resp = httpx.post(base + path, json=body, headers=headers, timeout=TIMEOUT)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise BbcUnavailable(
            f"bbc-ui could not be reached: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        raise BbcAmbiguous(
            f"bbc-ui did not answer a POST cleanly, so it may have landed: "
            f"{type(exc).__name__}") from exc
    code = resp.status_code
    if code // 100 == 2:
        try:
            return resp.json()
        except ValueError as exc:
            raise BbcAmbiguous(
                "bbc-ui answered a POST with a body that is not JSON; it may "
                "have landed.") from exc
    if code in (401, 403):
        log.error("bbc-ui refused the shared secret on %s (%d)", path, code)
        raise BbcUnavailable(f"bbc-ui refused the shared secret ({code}).")
    if code // 100 == 4:
        raise BbcUnavailable(f"bbc-ui rejected the request ({code}).")
    # Any other status (a 5xx, or an unexpected 3xx the client did not follow):
    # the proxy or bbc-ui did something unexpected and the POST may already have
    # been acted on, so treat it as ambiguous rather than a clean refusal.
    raise BbcAmbiguous(f"bbc-ui answered {code} to a POST, which may have landed.")


def request(items) -> dict[str, contract.RequestResult]:
    """Ask bbc-ui for episodes. `items` is (pid, attempt_key, reason) per pid.

    Returns the outcome by pid. A reply this nextup cannot read, or one that does
    not cover exactly the pids sent, is BbcAmbiguous rather than a plain outage:
    bbc-ui answered a POST, so it may well have queued the download, and an intent
    with no clean outcome must reconcile against status, not read as done.
    """
    items = list(items)
    reply = _write("/api/request", contract.request_body(items))
    try:
        return contract.parse_results(reply, [pid for pid, _key, _reason in items])
    except contract.ContractError as exc:
        raise BbcAmbiguous(
            f"bbc-ui replied to a request this nextup cannot read cleanly, "
            f"so it may have landed: {exc}") from exc


def cancel(pids) -> frozenset[str]:
    """Drop this household's request claim on each pid; return which were dropped.

    bbc-ui stops the underlying get_iplayer job only when no claim (a request
    like this, or a PVR schedule) is left, so cancelling a request never kills a
    schedule for the same pid. An acknowledgement this nextup cannot read is
    BbcAmbiguous, not success: the wants path must not drop a claim on a reply it
    could not verify.
    """
    pids = list(pids)
    reply = _write("/api/cancel", {"schema_version": contract.SCHEMA_VERSION,
                                   "pids": pids})
    try:
        return contract.parse_cancelled(reply, pids)
    except contract.ContractError as exc:
        raise BbcAmbiguous(
            f"bbc-ui acknowledged a cancel in a way this nextup cannot read: "
            f"{exc}") from exc
