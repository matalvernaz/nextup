"""Whether a configured backend is actually there.

Configuration is a shape: a URL, a key, and for the two *arrs a quality
profile. Reachability is a fact, and until this module existed the two were
conflated -- `configured` returning true was enough to offer a medium, so a
Radarr at the wrong port produced a working search box that found nothing, and
the only trace was a warning line per search that nobody watching a first
install ever reads.

Each backend is asked on an endpoint it already reads from in normal use, so
nothing here needs a health route the other end may not have. Every probe is
read-only.

The answers are cached for a short while and never fatal. An acquisition tool
being down is not this service's problem to report as its own ill health --
that would take the container out of a proxy's rotation over somebody else's
outage -- but it is very much worth saying out loud.
"""
import threading
import time
from dataclasses import dataclass

import httpx

from . import buskarr, config, listenarr, logs, radarr, sonarr

log = logs.get("backends")

#: Deliberately short. A probe is one small request, and the answer people want
#: after fixing a URL is "is it right now", not "is it right in an hour".
CACHE_SECONDS = 60

#: Shorter than the ordinary transport timeout. This question is asked while
#: somebody waits for a page or a capabilities response, and a backend that
#: takes eight seconds to say hello is not usable anyway.
PROBE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


@dataclass(frozen=True, slots=True)
class Status:
    """What is known about one backend right now."""
    #: The medium this backend serves.
    medium: str
    #: Its display name, for a log line or a page.
    name: str
    #: Whether it has the settings it needs. False means nothing was tried.
    configured: bool
    #: Whether it answered. None when it was not asked, because it is not
    #: configured -- which is not the same as "did not answer", and a page
    #: that showed them the same way would send somebody looking for a network
    #: fault that is really an empty variable.
    reachable: bool | None
    #: One sentence naming what is wrong, or "" when nothing is.
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.configured and self.reachable is True


def _probe(name: str, url: str, path: str, headers: dict,
           params: dict | None = None) -> tuple[bool, str]:
    """One read-only GET. True when the far end answered like itself."""
    target = url.rstrip("/") + path
    try:
        resp = httpx.get(target, headers=headers, params=params or {},
                         timeout=PROBE_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        return False, (f"{target} could not be reached "
                       f"({exc.__class__.__name__}). Inside Docker, "
                       "'localhost' is this container rather than the host.")
    if resp.status_code in (401, 403):
        return False, f"{target} refused the API key ({resp.status_code})."
    if resp.status_code == 404:
        return False, (f"{target} answered 404. The address is reaching "
                       "something, but not this backend's API.")
    if resp.status_code >= 400:
        return False, f"{target} answered {resp.status_code}."
    return True, ""


def _arr_status(medium: str, name: str, backend, url: str,
                api_key: str, profile_id: int) -> Status:
    """Radarr and Sonarr, which are one API with two vocabularies."""
    if not backend.configured():
        missing = []
        if not url:
            missing.append(f"{name.upper()}_URL")
        if not api_key:
            missing.append(f"{name.upper()}_API_KEY")
        if profile_id <= 0:
            missing.append(f"{name.upper()}_QUALITY_PROFILE_ID")
        return Status(medium, name, False, None,
                      "not configured: " + ", ".join(missing) + " unset")
    ok, detail = _probe(name, url, "/api/v3/system/status",
                        {"X-Api-Key": api_key, "Accept": "application/json"})
    return Status(medium, name, True, ok, detail)


def _radarr() -> Status:
    return _arr_status("movie", "radarr", radarr, config.RADARR_URL,
                       config.RADARR_API_KEY, config.RADARR_QUALITY_PROFILE_ID)


def _sonarr() -> Status:
    return _arr_status("series", "sonarr", sonarr, config.SONARR_URL,
                       config.SONARR_API_KEY, config.SONARR_QUALITY_PROFILE_ID)


def _buskarr() -> Status:
    if not buskarr.configured():
        missing = [n for n, v in (("BUSKARR_URL", config.BUSKARR_URL),
                                  ("BUSKARR_API_KEY", config.BUSKARR_API_KEY))
                   if not v]
        return Status("music", "buskarr", False, None,
                      "not configured: " + ", ".join(missing) + " unset")
    # buskarr's search route with an empty term: it is the read this service
    # already makes, and an empty query is answered rather than acted on.
    ok, detail = _probe("buskarr", config.BUSKARR_URL, "/api/v1/search",
                        {"X-Api-Key": config.BUSKARR_API_KEY,
                         "Accept": "application/json"},
                        params={"q": "", "unit": "track", "limit": 1})
    return Status("music", "buskarr", True, ok, detail)


def _listenarr() -> Status:
    if not listenarr.configured():
        return Status("book", "listenarr", False, None,
                      "not configured: LISTENARR_URL unset")
    # Its library listing, which is the read used to suppress a book already
    # on order. No key: Listenarr authenticates by session, and a 200 here is
    # only ever evidence that the address is right.
    ok, detail = _probe("listenarr", config.LISTENARR_URL, "/api/v1/library",
                        {"Accept": "application/json"})
    return Status("book", "listenarr", True, ok, detail)


_PROBES = (_radarr, _sonarr, _buskarr, _listenarr)

_cache: tuple[Status, ...] | None = None
_cached_at = 0.0
_guard = threading.Lock()


def statuses(force: bool = False) -> tuple[Status, ...]:
    """Every backend, whether it is configured, and whether it answered."""
    global _cache, _cached_at
    with _guard:
        if (_cache is not None and not force
                and time.monotonic() - _cached_at < CACHE_SECONDS):
            return _cache
    # Probed outside the lock: four small requests, and holding the lock
    # across them would make every concurrent caller wait for the slowest.
    built = tuple(probe() for probe in _PROBES)
    for status in built:
        if status.configured and status.reachable is False:
            log.error("%s is configured but not answering: %s",
                      status.name, status.detail)
    with _guard:
        _cache = built
        _cached_at = time.monotonic()
    return built


def status(medium: str, force: bool = False) -> Status | None:
    for found in statuses(force=force):
        if found.medium == medium:
            return found
    return None


def forget() -> None:
    global _cache
    with _guard:
        _cache = None


# --- What a backend can tell you about itself --------------------------------
#
# The Backends page asks these once a connection has tested good, so that a
# quality profile is chosen from that server's own list rather than typed in
# as a number found in another application's URL. That number was the single
# worst thing about setting this up: unset it disables a whole medium, and the
# only symptom is a container that starts, reports healthy and offers nothing.


@dataclass(frozen=True, slots=True)
class Choice:
    """One thing a backend offers, as a form control needs it."""
    value: str
    label: str


def _list(url: str, path: str, headers: dict) -> list[dict]:
    try:
        resp = httpx.get(url.rstrip("/") + path, headers=headers,
                         timeout=PROBE_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not list %s from %s (%s)", path, url, exc)
        return []
    return rows if isinstance(rows, list) else []


def quality_profiles(url: str, api_key: str) -> list[Choice]:
    """That Radarr's or Sonarr's own profiles, newest API first."""
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    rows = _list(url, "/api/v3/qualityprofile", headers)
    return [Choice(str(row["id"]), str(row.get("name") or row["id"]))
            for row in rows if isinstance(row, dict) and row.get("id")]


def root_folders(url: str, api_key: str) -> list[Choice]:
    """Where that backend puts things. Only worth asking about when several."""
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    rows = _list(url, "/api/v3/rootfolder", headers)
    return [Choice(str(row["path"]), str(row["path"]))
            for row in rows if isinstance(row, dict) and row.get("path")]


def listenarr_profiles(url: str) -> list[Choice]:
    """Listenarr's quality profiles. Its API is v1 and needs no key to read."""
    rows = _list(url, "/api/v1/qualityprofile", {"Accept": "application/json"})
    return [Choice(str(row["id"]), str(row.get("name") or row["id"]))
            for row in rows if isinstance(row, dict) and row.get("id")]
