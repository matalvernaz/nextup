"""Getting connected, from a page rather than from a text file.

The old first run was: create an API key in Jellyfin's dashboard, find two
quality-profile ids in two other applications' URLs, write five variables into
a file, and only then find out whether any of it was right. Every one of those
steps is a place to stop, and the feedback for all of them arrived at once, at
the end.

So the same things are asked here, one at a time, with the answer checked
before it is saved:

* **Jellyfin** is signed into, not configured. Somebody with an administrator
  account types the address and their own password; this asks Jellyfin for a
  credential and keeps that. Nobody has to visit a dashboard.
* **Each backend** is a URL and a key with a Test button, and once it answers
  its quality profiles are listed from the backend itself. Nobody has to find
  a number.

The environment still wins over everything here. A deployment that sets
`RADARR_URL` in a compose file is making a statement, and this page says so and
leaves the field alone rather than writing a value that will not be read.
"""
from dataclasses import dataclass

from . import backends, config, jellyfin, logs, media, settings

log = logs.get("setup")


@dataclass(frozen=True, slots=True)
class Field:
    """One thing to fill in, and whether this deployment lets a page fill it."""
    name: str
    label: str
    value: str
    #: True when the environment is deciding this one.
    locked: bool
    secret: bool = False
    help: str = ""


@dataclass(frozen=True, slots=True)
class BackendForm:
    """One backend as the page shows it."""
    key: str
    name: str
    label: str
    medium: str
    fields: tuple[Field, ...]
    status: backends.Status | None
    #: That backend's own quality profiles, once it answers. Empty until then,
    #: which is why the profile is only asked for after a successful test.
    profiles: tuple[backends.Choice, ...] = ()
    profile_field: str = ""
    profile_value: str = ""
    roots: tuple[backends.Choice, ...] = ()
    root_field: str = ""
    root_value: str = ""
    note: str = ""


def _field(name: str, label: str, secret: bool = False,
           help_text: str = "") -> Field:
    stored = getattr(config, name)
    value = "" if secret else str(stored or "")
    return Field(name=name, label=label, value=value,
                 locked=settings.held_in_environment(name),
                 secret=secret, help=help_text)


def _arr_form(key: str, label: str, medium: str, port: int) -> BackendForm:
    upper = key.upper()
    url = getattr(config, f"{upper}_URL")
    api_key = getattr(config, f"{upper}_API_KEY")
    status = backends.status(medium)
    profiles: tuple[backends.Choice, ...] = ()
    roots: tuple[backends.Choice, ...] = ()
    if url and api_key and status and status.reachable:
        profiles = tuple(backends.quality_profiles(url, api_key))
        found = backends.root_folders(url, api_key)
        # Only asked about when there is a choice to make. One root folder is
        # not a decision, and a form control for it is a step for nothing.
        roots = tuple(found) if len(found) > 1 else ()
    return BackendForm(
        key=key, name=key, label=label, medium=medium, status=status,
        fields=(
            _field(f"{upper}_URL", f"{label} address",
                   help_text=f"For example http://{key}:{port} if it is on the "
                             f"same Docker network. Not localhost: inside a "
                             f"container that is this container."),
            _field(f"{upper}_API_KEY", f"{label} API key", secret=True,
                   help_text=f"{label}: Settings, then General, then API Key."),
        ),
        profiles=profiles,
        profile_field=f"{upper}_QUALITY_PROFILE_ID",
        profile_value=str(getattr(config, f"{upper}_QUALITY_PROFILE_ID") or ""),
        roots=roots,
        root_field=f"{upper}_ROOT_FOLDER",
        root_value=str(getattr(config, f"{upper}_ROOT_FOLDER") or ""),
    )


def _listenarr_form() -> BackendForm:
    url = config.LISTENARR_URL
    status = backends.status("book")
    profiles = tuple(backends.listenarr_profiles(url)) if (
        url and status and status.reachable) else ()
    note = ""
    if url and status and status.reachable:
        serves = jellyfin.serves_audiobooks()
        if serves is False:
            note = ("This Jellyfin does not file audiobooks as whole books, so "
                    "books will not be offered even with Listenarr connected. "
                    "That needs the audiobook fork of Jellyfin; on a stock "
                    "server a Books library holds ebooks and nothing asked for "
                    "here would ever read as arrived.")
    return BackendForm(
        key="listenarr", name="listenarr", label="Listenarr", medium="book",
        status=status, note=note,
        fields=(_field("LISTENARR_URL", "Listenarr address",
                       help_text="For example http://listenarr:4545."),),
        profiles=profiles,
        profile_field="LISTENARR_QUALITY_PROFILE_ID",
        profile_value=str(config.LISTENARR_QUALITY_PROFILE_ID or ""),
    )


def _buskarr_form() -> BackendForm:
    return BackendForm(
        key="buskarr", name="buskarr", label="buskarr", medium="music",
        status=backends.status("music"),
        fields=(
            _field("BUSKARR_URL", "buskarr address",
                   help_text="For example http://buskarr:8000."),
            _field("BUSKARR_API_KEY", "buskarr API key", secret=True,
                   help_text="The same key buskarr itself was started with."),
        ),
    )


def forms() -> tuple[BackendForm, ...]:
    """Every backend, in the order somebody is most likely to want them."""
    return (
        _arr_form("radarr", "Radarr", "movie", 7878),
        _arr_form("sonarr", "Sonarr", "series", 8989),
        _listenarr_form(),
        _buskarr_form(),
    )


def save(values: dict[str, str]) -> list[str]:
    """Store what a page submitted. Returns what was refused, and why.

    Anything the environment holds is skipped rather than written: writing it
    would store a value nothing ever reads, and the next page load would show
    the environment's value back, which looks like the save failed.
    """
    refused: list[str] = []
    keep: dict[str, str] = {}
    for name, raw in values.items():
        if name not in settings.WRITABLE:
            continue
        if settings.held_in_environment(name):
            refused.append(
                f"{name} is set in this deployment's environment, so it is "
                "not changed here.")
            continue
        value = (raw or "").strip()
        if name.endswith("_QUALITY_PROFILE_ID") and value:
            # Validated at save, which is what lets the reader trust it. A
            # number that will not parse must not become a page nobody can
            # load in order to fix it.
            try:
                int(value)
            except ValueError:
                refused.append(f"{name} must be a whole number, not {value!r}.")
                continue
        if name in settings.SECRET and not value:
            # An empty secret field means "leave it alone", not "delete it":
            # the form never renders a key back, so a blank box is the normal
            # state of one that is already set.
            continue
        keep[name] = value
    settings.put_all(keep)
    if keep:
        # Everything derived from a connection setting is now stale: which
        # media are offered, whether each backend answers, and the library
        # index behind arrival. All three are dropped together.
        media.forget()
        log.info("settings saved: %s", ", ".join(sorted(keep)))
    return refused


def connect_jellyfin(url: str, username: str, password: str) -> str:
    """Sign in as a Jellyfin administrator and keep a credential for this app.

    Prefers an API key of its own, which survives that person signing out
    everywhere. Falls back to the access token the sign-in produced, which
    works identically but is tied to their session -- so if it is used, the
    page says so, because revoking it would stop this service reading the
    library at all.

    Returns a sentence about what happened, for the page to read out.
    """
    url = url.strip().rstrip("/")
    if not url:
        raise jellyfin.TokenRejected("A Jellyfin address is needed.")
    # Written before authenticating: `jellyfin.authenticate` reads
    # `config.JELLYFIN_URL`, and the address being tried is the one just typed.
    if not settings.held_in_environment("JELLYFIN_URL"):
        settings.put("JELLYFIN_URL", url)
    token, user = jellyfin.authenticate(username.strip(), password,
                                        _device())
    if not user.is_admin:
        raise jellyfin.TokenRejected(
            f"{user.name} is not a Jellyfin administrator. Setting this up "
            "needs an administrator account, because it reads every library.")
    settings.put("JELLYFIN_TOKEN", token)
    media.forget()
    made_key = _make_api_key(token)
    if made_key:
        settings.put("JELLYFIN_TOKEN", made_key)
        media.forget()
        return (f"Connected to Jellyfin as {user.name}, with an API key of "
                "this service's own.")
    return (f"Connected to Jellyfin as {user.name}, using that sign-in's own "
            "access token. Jellyfin would not issue a separate API key, so "
            "signing out of every device on that account would disconnect "
            "this service and you would set it up again here.")


def _device() -> str:
    from . import sessions
    return sessions.device_id()


def _make_api_key(token: str) -> str | None:
    """Ask Jellyfin for an API key of this service's own, or give up quietly.

    Deliberately best-effort. The endpoint has moved between Jellyfin versions
    and forks, and this has a working credential either way -- so a failure
    here is worth a log line and nothing more.
    """
    import httpx
    headers = {"Authorization": f'MediaBrowser Token="{token}"',
               "Accept": "application/json"}
    base = config.JELLYFIN_URL
    try:
        with httpx.Client(base_url=base, headers=headers,
                          timeout=backends.PROBE_TIMEOUT) as c:
            # Read first, so the new key can be told from any this service
            # made on a previous setup. Jellyfin's POST answers 204 with no
            # body, so the only way to learn the key is to list them -- and
            # "the last one" is a guess about ordering rather than a promise.
            # Setting this up twice is a documented path, so that guess would
            # eventually hand back a stale key and call it new.
            before = _key_tokens(c)
            created = c.post("/Auth/Keys", params={"app": config.SERVICE_NAME})
            if created.status_code >= 400:
                log.info("Jellyfin would not issue an API key (%d); using the "
                         "sign-in token instead", created.status_code)
                return None
            after = _key_tokens(c)
    except (httpx.HTTPError, ValueError) as exc:
        log.info("could not ask Jellyfin for an API key (%s); using the "
                 "sign-in token instead", exc)
        return None
    fresh = [token for token in after if token not in before]
    if len(fresh) != 1:
        # Nothing appeared, or more than one did and there is no telling
        # which is this one. The sign-in token works, so guessing here would
        # trade a credential that works for one that might not.
        log.info("could not identify the API key just created (%d new); "
                 "using the sign-in token instead", len(fresh))
        return None
    log.info("Jellyfin issued an API key for this service")
    return fresh[0]


def _key_tokens(client) -> set[str]:
    """Every API key Jellyfin currently holds for this service, by token."""
    listed = client.get("/Auth/Keys").raise_for_status().json()
    rows = listed.get("Items") if isinstance(listed, dict) else listed
    return {str(row["AccessToken"]) for row in rows or []
            if isinstance(row, dict) and row.get("AccessToken")
            and row.get("AppName") == config.SERVICE_NAME}


def needs_setup() -> bool:
    """Whether there is any Jellyfin credential at all.

    The one thing nothing works without, and the only thing that makes a first
    run stop at a page rather than at the ordinary one.
    """
    return not config.JELLYFIN_TOKEN
