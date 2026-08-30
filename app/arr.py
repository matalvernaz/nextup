"""What Radarr and Sonarr have in common: the v3 handshake and its guards.

These are one API with two vocabularies, so the transport lives here once and
each medium's module says only what is different about it -- what a lookup
returns, what an add body holds, and what "in the library" means for it.

Every guard Nextread learned the hard way against Listenarr is kept:

* the by-id check runs before every add, because an add endpoint is not
  assumed to dedupe;
* the quality profile is always stated, never left to a backend default;
* a test add is made unmonitored, so a verification run cannot start a real
  download.
"""
from dataclasses import dataclass
from typing import NamedTuple

import httpx

from . import logs

log = logs.get("arr")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Radarr and Sonarr answer a lookup for a term nothing matches with an empty
#: list, and a lookup for a malformed one with a 400. Neither is an error worth
#: showing: both mean "no results".
_EMPTY = []


class AddResult(NamedTuple):
    """Outcome of handing one thing to an acquisition tool.

    `ok` means it is now in that tool's library and will be acquired --
    including when it was already there, because the caller's question is "is
    this coming?" and the answer is yes either way.
    """
    ok: bool
    message: str
    backend_id: str = ""
    title: str = ""
    year: str = ""


@dataclass(frozen=True, slots=True)
class Arr:
    """One configured *arr, and the few things that differ between them."""
    name: str
    url: str
    api_key: str
    quality_profile_id: int
    root_folder: str
    #: Collection endpoint: `movie` for Radarr, `series` for Sonarr.
    resource: str
    #: The provider id this tool is keyed on: `tmdbId` or `tvdbId`.
    id_field: str

    @property
    def configured(self) -> bool:
        """Whether this backend may be offered at all.

        A missing quality profile disqualifies it as firmly as a missing key.
        Radarr ships seven profiles and Sonarr the same; letting the backend
        pick would make a real choice about disk and bandwidth silently.
        """
        return bool(self.url and self.api_key and self.quality_profile_id > 0)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.url.rstrip("/") + "/api/v3",
            headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
            timeout=_TIMEOUT)

    def lookup(self, term: str, limit: int) -> list[dict]:
        """Catalogue search, which these tools proxy from TMDB and TVDB.

        Worth noting because it is what makes this service cheap: no metadata
        account, no second API key, and the same records the acquisition tool
        will itself use once the thing is asked for.
        """
        term = term.strip()
        if not term:
            return _EMPTY
        try:
            with self.client() as c:
                resp = c.get(f"/{self.resource}/lookup", params={"term": term})
        except httpx.HTTPError as exc:
            log.warning("%s lookup unreachable term=%r (%s)", self.name, term, exc)
            return _EMPTY
        if resp.status_code >= 400:
            log.warning("%s lookup refused term=%r status=%d",
                        self.name, term, resp.status_code)
            return _EMPTY
        try:
            rows = resp.json()
        except ValueError:
            return _EMPTY
        return rows[:limit] if isinstance(rows, list) else _EMPTY

    def existing(self, provider_id: str) -> dict | None:
        """This tool's own row for a provider id, or None.

        Checked before every add. Verified live: the filtered collection query
        returns only matching rows rather than the whole library, so this is
        one small response rather than a download of everything.
        """
        try:
            with self.client() as c:
                resp = c.get(f"/{self.resource}",
                             params={self.id_field: provider_id})
        except httpx.HTTPError as exc:
            log.warning("%s by-id lookup failed id=%s (%s); treating as absent, "
                        "so the add may hit a duplicate",
                        self.name, provider_id, exc)
            return None
        if resp.status_code >= 400:
            return None
        try:
            rows = resp.json()
        except ValueError:
            return None
        return rows[0] if isinstance(rows, list) and rows else None

    def add(self, body: dict) -> AddResult:
        """POST one add body, with the tool's own answer carried back."""
        try:
            with self.client() as c:
                resp = c.post(f"/{self.resource}", json=body)
        except httpx.HTTPError as exc:
            log.error("%s add failed: unreachable (%s)", self.name, exc)
            return AddResult(False, f"{self.name} could not be reached.")
        if resp.status_code >= 400:
            detail = _detail(resp)
            # Both tools answer an already-present item with 400 and a message
            # naming the clash. Two people asking at the same moment both pass
            # the by-id check, so one of them lands here; it is coming either
            # way, which is what was asked.
            if _already_present(detail):
                log.info("%s add: already present", self.name)
                return AddResult(True, f"Already in {self.name}.")
            log.error("%s add rejected status=%d body=%s",
                      self.name, resp.status_code, detail[:180])
            return AddResult(False, f"{self.name} refused it: {detail[:180]}")
        try:
            row = resp.json()
        except ValueError:
            row = {}
        return AddResult(True, f"Sent to {self.name}.",
                         str(row.get("id") or ""), row.get("title") or "",
                         str(row.get("year") or ""))

    def delete(self, backend_id: str) -> bool:
        """Remove a row without touching files.

        `deleteFiles` is false and stays false. Cancelling a request means
        stopping a search, never removing something already downloaded -- the
        library belongs to the household, not to whoever asked last.
        """
        if not backend_id:
            return False
        try:
            with self.client() as c:
                resp = c.delete(f"/{self.resource}/{backend_id}",
                                params={"deleteFiles": "false",
                                        "addImportListExclusion": "false"})
        except httpx.HTTPError as exc:
            log.warning("%s delete failed id=%s (%s)", self.name, backend_id, exc)
            return False
        return resp.status_code < 400

    def root_folder_path(self) -> str:
        """Where to put it: configured, or discovered when there is one choice.

        Unlike the quality profile this is not a judgement call when a server
        has a single root folder -- there is nothing to choose between -- so
        discovery saves a setting. With several configured folders and none
        named, the add is refused rather than guessed at.
        """
        if self.root_folder:
            return self.root_folder
        try:
            with self.client() as c:
                rows = c.get("/rootfolder").raise_for_status().json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("%s root folder lookup failed (%s)", self.name, exc)
            return ""
        paths = [r.get("path") for r in rows if r.get("path")]
        if len(paths) == 1:
            return paths[0]
        log.error("%s has %d root folders and none is configured; set the "
                  "root folder explicitly", self.name, len(paths))
        return ""


def _detail(resp: httpx.Response) -> str:
    """The human-readable half of an *arr error, whichever shape it arrives in."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text
    if isinstance(payload, list):
        return "; ".join(
            str(p.get("errorMessage") or p.get("message") or p) for p in payload)
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("errorMessage") or payload)
    return str(payload)


def _already_present(detail: str) -> bool:
    """Whether an *arr's refusal means "I already have this"."""
    lowered = detail.casefold()
    return "already been added" in lowered or "already exists" in lowered
