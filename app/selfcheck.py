"""Whether the routes clients actually look for are really there.

Standing this service up on its own hostname is only half a deployment. The
other half is a proxy rule at the Jellyfin origin, and leaving that out has no
symptom worth the name: a client probes the Jellyfin origin, gets Jellyfin's
own 404, concludes the service is not installed and says nothing, because most
servers do not run one. The feature is simply missing, with nothing in any log.

Warn-only, and deliberately not part of the health check. A proxy drops an
unhealthy container from its load balancer, so failing health over a missing
same-origin route would take down the address that still works and turn half a
misconfiguration into all of one.
"""
import time
from threading import Thread

import httpx

from . import compat_nextread, config, logs

log = logs.get("selfcheck")

# Traefik and its like take twenty-five to thirty seconds to register a
# recreated container, so a route probed sooner reads as missing when it is
# only late.
FIRST_DELAY_SECONDS = 60
INTERVAL_SECONDS = 3600
TIMEOUT_SECONDS = 10.0

# One probe at sixty seconds was not enough of a wait. Measured 2026-09-10: a
# deploy logged both routes as 404 at exactly the first delay, and both
# answered when asked again minutes later -- so every slow registration wrote
# the one error line in this service that means "a feature is silently
# missing". An alarm that fires on healthy deploys is one nobody reads on the
# day it is right, which is the whole failure this module exists to prevent.
#
# Only the first pass retries. A later one is about a rule that has since
# vanished, and there is nothing starting up to wait for.
FIRST_PASS_ATTEMPTS = 3
RETRY_GAP_SECONDS = 20


def expected_service(base_url: str) -> str:
    """Which service name `/info` should answer with under this base URL.

    One process answers on several prefixes and `/info` names the service the
    caller asked under, so the old audiobook prefix answers `nextread` and is
    correct in doing so. Checking it against this service's own name instead
    would report a working route as somebody else's.
    """
    tail = base_url.rstrip("/").rsplit("/", 1)[-1].casefold()
    return compat_nextread.LEGACY_SERVICE if (
        tail == compat_nextread.LEGACY_SERVICE) else config.SERVICE_NAME


def check(base_url: str) -> str | None:
    """None when the route answers, otherwise a sentence naming what is wrong."""
    url = f"{base_url.rstrip('/')}/api/v1/info"
    wanted = expected_service(base_url)
    try:
        resp = httpx.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.HTTPError as exc:
        return f"{url} could not be reached ({exc.__class__.__name__})"
    if resp.status_code == 404:
        return (f"{url} answers 404. The proxy rule serving this service at the "
                "Jellyfin origin is missing, and no client will ever find it.")
    if resp.status_code != 200:
        return f"{url} answered {resp.status_code} rather than 200."
    try:
        named = resp.json().get("service")
    except ValueError:
        return f"{url} answered 200 but not with JSON."
    if named != wanted:
        return f"{url} answered 200 but belongs to {named!r}, not {wanted!r}."
    return None


def settled(base_url: str, attempts: int = 1,
            gap: float = RETRY_GAP_SECONDS) -> tuple[str | None, int]:
    """The problem still there after `attempts` probes, and how many it took.

    Returns `(None, n)` once the route answers, so a caller can tell a route
    that was simply late from one that was right the first time.
    """
    problem = check(base_url)
    for taken in range(2, attempts + 1):
        if problem is None:
            return None, taken - 1
        time.sleep(gap)
        problem = check(base_url)
        if problem is None:
            return None, taken
    return problem, attempts


def _loop(base_urls: list[str]) -> None:
    time.sleep(FIRST_DELAY_SECONDS)
    attempts = FIRST_PASS_ATTEMPTS
    while True:
        for base_url in base_urls:
            problem, taken = settled(base_url, attempts)
            if problem:
                log.error("same-origin route: %s", problem)
            elif taken > 1:
                log.info("same-origin route: %s answers, after %d attempt(s) "
                         "— the proxy was still registering this container",
                         base_url, taken)
            else:
                log.info("same-origin route: %s answers", base_url)
        attempts = 1
        time.sleep(INTERVAL_SECONDS)


def watch() -> None:
    """Start the check, if this deployment said where to look.

    Unset is the ordinary case for somebody running only the browser pages, and
    it costs them nothing: no thread, no requests, no log line.
    """
    if not config.PUBLIC_URLS:
        return
    Thread(target=_loop, args=(list(config.PUBLIC_URLS),),
           name="same-origin-check", daemon=True).start()
