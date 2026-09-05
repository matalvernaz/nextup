"""Slowing down repeated failures on the one endpoint that takes a password.

`POST /signin` forwards a username and password to Jellyfin. It is
unauthenticated by necessity -- it is how somebody becomes authenticated -- and
in the shipped deployment it is a published port with nothing in front of it.
Without a limit it is two things at once: somewhere to test stolen credentials
against a Jellyfin that will answer honestly, and a way to run a real account
into Jellyfin's own lockout policy from outside.

Counted per address *and* per username, and either alone is enough to refuse.
Per address on its own misses a slow attempt against one account from many
places; per username on its own misses one address spraying many accounts. An
address behind a proxy is only as trustworthy as the proxy -- which is why the
username limit exists as well, and why exceeding it costs a wait rather than a
lockout that an attacker could inflict on somebody deliberately.

Successes clear both counters, so an ordinary person who mistypes a password
four times and then gets it right is not carrying anything afterwards.
"""
import time
from threading import Lock

from . import logs

log = logs.get("throttle")

#: Failures allowed in a window before the next attempt is refused.
MAX_ATTEMPTS = 10

#: How long failures are remembered, and how long a refusal lasts.
WINDOW_SECONDS = 300

_failures: dict[str, list[float]] = {}
_guard = Lock()


def _recent(key: str, now: float) -> list[float]:
    kept = [at for at in _failures.get(key, ()) if now - at < WINDOW_SECONDS]
    if kept:
        _failures[key] = kept
    else:
        _failures.pop(key, None)
    return kept


def retry_after(*keys: str) -> int:
    """Seconds to wait before any of these keys may try again; 0 when free."""
    now = time.monotonic()
    wait = 0
    with _guard:
        for key in keys:
            recent = _recent(key, now)
            if len(recent) >= MAX_ATTEMPTS:
                wait = max(wait, int(WINDOW_SECONDS - (now - recent[0])) + 1)
    return wait


def record_failure(*keys: str) -> None:
    now = time.monotonic()
    with _guard:
        for key in keys:
            _failures.setdefault(key, []).append(now)
            log.info("failed attempt %d/%d for %s",
                     len(_failures[key]), MAX_ATTEMPTS, key)


def clear(*keys: str) -> None:
    with _guard:
        for key in keys:
            _failures.pop(key, None)


def forget_everything() -> None:
    """Only for tests, which must not inherit each other's counters."""
    with _guard:
        _failures.clear()


def caller_address(request) -> str:
    """The address to count against, as well as it can be known here.

    The leftmost `X-Forwarded-For` entry where a proxy set one, because
    otherwise every request behind a proxy is the proxy and one person's
    mistyping would lock out the household. A caller can put anything in that
    header when no proxy overwrites it, which is exactly why the username limit
    is not optional.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return f"address:{first}"
    client = getattr(request, "client", None)
    return f"address:{getattr(client, 'host', '') or 'unknown'}"
