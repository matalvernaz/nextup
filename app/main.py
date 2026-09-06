"""The web application: browser pages, and the JSON API mounted beneath them.

The pages are server-rendered HTML with real forms and no JavaScript in any
path that does something. That is not minimalism for its own sake -- a screen
reader is the primary interface here, and a page that has already finished
rendering when it arrives is one a reader can simply read.

Identity for these pages comes from three places, in this order: a
forward-auth proxy's header where one is in front, then this app's own signed
session cookie, then the configured single-user fallback. The middle one is
what makes the pages usable on an installation with no proxy at all -- see
`app/sessions.py`. Identity for the JSON API comes from none of them and never
may: see `api.caller`.
"""
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (api, backends, compat_nextread, config, jellyfin, logs, media,
               recommendations, selfcheck, sessions, settings, setup, store,
               throttle, wants)
from .books import shelves as book_shelves
from .books import store as book_store
from .books import upkeep
from .books import wants as book_wants

log = logs.get("main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _rekey_ledger_once()
    selfcheck.watch()
    upkeep.watch()
    yield


def _rekey_ledger_once() -> None:
    """Move the ledger off display names and onto Jellyfin account ids.

    Deliberately fatal when Jellyfin cannot be asked. Serving id-keyed lookups
    over a name-keyed ledger is exactly the failure this migration exists to
    prevent: every list reads empty and every daily allowance reads unspent,
    which invites a second request for something already on its way.
    """
    if store.user_key_scheme() == "id":
        return
    if store.nothing_to_rekey():
        # Nothing to move, so nothing to ask Jellyfin about. Without this a
        # fresh install whose Jellyfin is not up yet -- the ordinary ordering
        # on a one-box bring-up -- dies in lifespan and restart-loops, on a
        # migration of zero rows.
        store.set_user_key_scheme("id")
        return
    try:
        names = jellyfin.all_users()
    except Exception as exc:
        raise RuntimeError(
            "cannot rekey the request ledger: Jellyfin did not answer") from exc
    store.rekey_users(names)


app = FastAPI(title="nextup", lifespan=lifespan)

# One API, answered on three prefixes, because a client's address is derived
# rather than typed. EchoFin resolves it as
# `override ?? companion + "/nextup" ?? jellyfinOrigin + "/nextup"` and then
# appends `/api/v1/...`, so a direct address arrives at the first of these and
# every derived one at the second.
app.include_router(api.router)
app.include_router(api.router, prefix="/nextup")

# The audiobook service's own protocol, at the prefix its clients derive. Not
# a translation layer over this API: mostly its original handlers, mounted
# here and calling the engine from its new home. See the module.
app.include_router(compat_nextread.router)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

#: Additional hostnames whose pages may post to this one. Rarely needed: the
#: default is "the host this request arrived at", which covers every ordinary
#: deployment.
ALLOWED_ORIGIN_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("ALLOWED_ORIGIN_HOSTS", "").split(",") if h.strip()}


@app.middleware("http")
async def block_cross_origin_writes(request: Request, call_next):
    """Refuse a write whose page came from somewhere else.

    The browser pages ride a forward-auth cookie, and that cookie is scoped to
    the parent domain -- so a page on any sibling subdomain can post to this
    one and spend a signed-in person's allowance. SameSite does not stop a
    *same-site* cross-origin post and CORS does not apply to form submissions,
    so checking the origin of unsafe methods is the whole mitigation.

    A request carrying neither Origin nor Referer is allowed. Some privacy
    setups strip both, and native clients send neither -- which is also why
    this does not disturb the JSON API, whose callers authenticate on a token
    rather than on a cookie and so have nothing to be ridden.
    """
    if request.method in SAFE_METHODS:
        return await call_next(request)
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    host = urlsplit(raw).hostname if raw else None
    arrived_at = (request.headers.get("x-forwarded-host")
                  or request.url.hostname or "").split(",")[0].strip().lower()
    expected = ({arrived_at} if arrived_at else set()) | ALLOWED_ORIGIN_HOSTS
    if host and expected and host.lower() not in expected:
        log.warning("refused a write from %s (expected one of %s)",
                    host, sorted(expected))
        return PlainTextResponse(
            f"Refused: this looks like a cross-site request (from {host}).",
            status_code=403)
    return await call_next(request)
templates = Jinja2Templates(directory="app/templates")

# A global rather than a context key on five routes: the banner it draws is
# about the installation, not about the page, and one route forgetting to pass
# it is one page that says everything is fine while nothing is.
templates.env.globals["credential_rejected"] = (
    lambda: jellyfin.credential_rejected(force=False))

# Whether to draw the link to Discover at all. An installation with no
# rankable library should not be offered a page that has nothing on it.
templates.env.globals["discover_media"] = lambda: discover_media()

app.mount("/static", StaticFiles(directory="app/static"), name="static")

store.init()


def viewer(request: Request) -> jellyfin.User:
    """Who is looking at this page.

    A forward-auth proxy's header first, because where one is deployed it is
    the authority and this app has no business second-guessing it. Then this
    app's own session cookie, which is how an installation with no proxy signs
    anybody in at all. Then `JELLYFIN_USER`, for a single-person deployment
    that wants neither.

    Raises `LookupError` when none of the three resolves, which the pages turn
    into the sign-in form. That fallback chain is why this resolver is not
    shared with the JSON API: there it would hand any caller the owner's list.
    """
    name = (request.headers.get(config.AUTH_USER_HEADER) or "").strip()
    if name:
        return jellyfin.user(name)
    token = sessions.read(request.cookies.get(sessions.COOKIE_NAME))
    if token:
        try:
            return jellyfin.user_from_token(token)
        except jellyfin.TokenRejected:
            # The cookie verified but Jellyfin has since revoked the token
            # inside it. Signing out is the honest answer; carrying on to the
            # configured fallback would silently show one account's list to
            # whoever's session had just expired.
            log.info("a session's Jellyfin token is no longer accepted")
            raise LookupError("Your session has expired. Please sign in again.")
    return jellyfin.user(config.JELLYFIN_USER or None)


def _signin_page(request: Request, detail: str = "", status: int = 200,
                 headers: dict[str, str] | None = None):
    """The sign-in form, and why it is being shown."""
    proxied = bool(request.headers.get(config.AUTH_USER_HEADER))
    return templates.TemplateResponse(
        request=request, name="signin.html", status_code=status,
        headers=headers,
        context={
            "detail": detail,
            "proxied": proxied,
            "encrypted": sessions.is_secure(
                request.headers.get("x-forwarded-proto"), request.url.scheme),
            "configured_user": bool(config.JELLYFIN_USER),
        })


@app.get("/signin", response_class=HTMLResponse)
def get_signin(request: Request, msg: str = ""):
    return _signin_page(request, detail=msg)


@app.post("/signin")
def post_signin(request: Request, username: str = Form(""),
                password: str = Form("")):
    """Sign in against Jellyfin and keep the token it hands back.

    No password is stored, logged, or sent anywhere but Jellyfin. What is kept
    is the access token, in a cookie this app signs.
    """
    name = username.strip()
    keys = (throttle.caller_address(request), f"user:{name.casefold()}")
    wait = throttle.retry_after(*keys)
    if wait:
        log.warning("refusing a sign-in attempt for %r: too many failures",
                    name)
        minutes = max(1, round(wait / 60))
        return _signin_page(
            request, status=429,
            detail=f"Too many failed sign-in attempts. Try again in about "
                   f"{minutes} minute{'' if minutes == 1 else 's'}.",
            headers={"Retry-After": str(wait)})
    try:
        cookie, user = sessions.sign_in(name, password)
    except jellyfin.TokenRejected as exc:
        # Counted here and not on an unreachable Jellyfin: a server that is
        # down says nothing about whether the password was right, and counting
        # it would lock the household out of a service that is about to work
        # again on its own.
        throttle.record_failure(*keys)
        return _signin_page(request, detail=str(exc), status=401)
    except jellyfin.JellyfinUnavailable as exc:
        return _signin_page(
            request,
            detail=f"Jellyfin could not be reached, so nobody can sign in "
                   f"yet. ({exc})",
            status=503)
    throttle.clear(*keys)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        sessions.COOKIE_NAME, cookie, max_age=sessions.SESSION_SECONDS,
        httponly=True, samesite="lax",
        # Only where the request really arrived over HTTPS. Setting it on a
        # plain-HTTP install would stop the cookie coming back at all, and a
        # first installation on a home network is exactly that install.
        secure=sessions.is_secure(
            request.headers.get("x-forwarded-proto"), request.url.scheme))
    log.info("session started for %s", user.key)
    return response


@app.post("/signout")
def post_signout(request: Request):
    """Forget this browser's session. Jellyfin's own token is left alone.

    Deliberately not a Jellyfin sign-out: the same account may be signed in on
    a phone with the same token, and closing a browser tab is no reason to
    stop somebody's audiobook.
    """
    response = RedirectResponse(url="/signin", status_code=303)
    response.delete_cookie(sessions.COOKIE_NAME)
    return response


@app.exception_handler(jellyfin.JellyfinUnavailable)
def jellyfin_unavailable(request: Request, exc: Exception):
    """A readable page when Jellyfin cannot be asked who somebody is.

    Every page resolves its viewer through Jellyfin, and the five routes that
    do caught only the "nobody resolved" case -- so a Jellyfin that was merely
    down raised through them and became a 500 with a traceback. The container
    stays healthy through an outage on purpose; the pages should read like an
    outage too, not like a bug in this service.

    Registered on the application rather than repeated per route so a route
    added later cannot forget it. The JSON API resolves its caller through its
    own dependency, which answers 503 in JSON before this is reached.
    """
    log.warning("Jellyfin could not be reached for %s: %s", request.url.path, exc)
    return templates.TemplateResponse(
        request=request, name="unavailable.html", status_code=503,
        context={"detail": str(exc)})


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    """Liveness, and only that: whether this process is still answering.

    Deliberately says nothing about Jellyfin, an arr, or this service's own
    credential. A check that fails on a downstream fault turns one outage into
    a restart loop -- and, worse here, a proxy takes an unhealthy container out
    of its pool. Verified against Traefik's Docker provider: the same container
    with the same labels answers 200 through the proxy while healthy and 404
    while unhealthy, with no log line either way.

    So a refused API key must not fail this. `/setup` is the page that issues a
    new one, and a check that can delete `/setup`'s router is a check that
    makes the fault unrepairable from outside. `/readyz` reports that state
    instead, and nothing routes on it.
    """
    return "ok"


@app.get("/readyz")
def readyz(response: Response) -> dict:
    """Whether this service can actually do its work, for a person to read.

    Split from `/healthz` on purpose, and nothing in the shipped compose points
    a healthcheck at it: a proxy that drops a container on this answer would
    take away the page that fixes it.

    Only the credential is reported. Everything else -- a refused connection, a
    timeout, an arr that is down -- is somebody else's outage, passes on its
    own, and would make this flap.
    """
    if jellyfin.credential_rejected():
        log.warning("not ready: Jellyfin is refusing this service's API key")
        response.status_code = 503
        return {
            "ready": False,
            "reason": "jellyfin_credential_rejected",
            "detail": "Jellyfin is refusing this service's API key. Sign in "
                      "again at /setup to issue a new one.",
            "repair": "/setup",
        }
    return {"ready": True}


def _setup_is_open(request: Request):
    """None when this page may be used, or the response that refuses it.

    Open with no credential at all, because that is the state it exists for and
    there is nobody to authenticate against yet. Once connected it needs a
    Jellyfin administrator, exactly as `/backends` does: this page rewrites the
    address this service sends its own API key to, which is a larger prize than
    pointing the install at somebody's Radarr.

    Signing in still works while Jellyfin is refusing this service's key --
    `authenticate` and `user_from_token` both carry the caller's credentials
    rather than this service's -- so the repair path `/readyz` names stays open
    in the state it is named for. The one case this closes is an address that
    is wrong: nobody can sign in through it, and the way back is the
    environment, which wins over anything a page has stored.
    """
    if setup.needs_setup():
        return None
    try:
        user = viewer(request)
    except LookupError as exc:
        return _signin_page(request, detail=str(exc), status=401)
    if not user.is_admin:
        return templates.TemplateResponse(
            request=request, name="setup.html", status_code=403,
            context={
                "message": "Changing where this connects needs a Jellyfin "
                           "administrator account.",
                "jellyfin_url": "",
                "locked": True,
                "connected": True,
            })
    return None


@app.get("/setup", response_class=HTMLResponse)
def get_setup(request: Request, msg: str = ""):
    """Connect to Jellyfin. The only step a first run cannot skip."""
    if (refused := _setup_is_open(request)) is not None:
        return refused
    return templates.TemplateResponse(
        request=request, name="setup.html",
        context={
            "message": msg,
            "jellyfin_url": config.JELLYFIN_URL,
            "locked": settings.held_in_environment("JELLYFIN_TOKEN"),
            "connected": not setup.needs_setup(),
        })


@app.post("/setup")
def post_setup(request: Request, jellyfin_url: str = Form(""),
               username: str = Form(""), password: str = Form("")):
    if (refused := _setup_is_open(request)) is not None:
        return refused
    # Counted the same way `/signin` counts, and for the same reason: this
    # forwards a password to Jellyfin, and the shipped compose publishes a port.
    keys = (throttle.caller_address(request), f"setup:{username.strip().casefold()}")
    if (wait := throttle.retry_after(*keys)):
        minutes = max(1, round(wait / 60))
        return RedirectResponse(
            url="/setup?msg=" + quote(
                f"Too many failed attempts. Try again in about {minutes} "
                f"minute{'' if minutes == 1 else 's'}."),
            status_code=303)
    try:
        message = setup.connect_jellyfin(jellyfin_url, username, password)
    except jellyfin.TokenRejected as exc:
        throttle.record_failure(*keys)
        return RedirectResponse(url=f"/setup?msg={quote(str(exc))}",
                                status_code=303)
    except jellyfin.JellyfinUnavailable as exc:
        return RedirectResponse(
            url=f"/setup?msg={quote(f'That Jellyfin could not be reached. {exc}')}",
            status_code=303)
    throttle.clear(*keys)
    return RedirectResponse(url=f"/backends?msg={quote(message)}",
                            status_code=303)


@app.get("/backends", response_class=HTMLResponse)
def get_backends(request: Request, msg: str = ""):
    """Which acquisition tools this household runs, and whether they answer."""
    if setup.needs_setup():
        return RedirectResponse(url="/setup", status_code=303)
    try:
        user = viewer(request)
    except LookupError as exc:
        return _signin_page(request, detail=str(exc), status=401)
    if not user.is_admin:
        # A member pointing the household's install at their own Radarr is not
        # a threat model worth leaving open for the sake of one fewer check.
        return templates.TemplateResponse(
            request=request, name="backends.html", status_code=403,
            context={"user": user, "forms": (), "message":
                     "Changing where this connects needs a Jellyfin "
                     "administrator account.", "readonly": True})
    return templates.TemplateResponse(
        request=request, name="backends.html",
        context={"user": user, "forms": setup.forms(), "message": msg,
                 "readonly": False})


@app.post("/backends")
async def post_backends(request: Request):
    """Save one backend's settings, then show what it says for itself."""
    if setup.needs_setup():
        return RedirectResponse(url="/setup", status_code=303)
    try:
        user = viewer(request)
    except LookupError as exc:
        return _signin_page(request, detail=str(exc), status=401)
    if not user.is_admin:
        return RedirectResponse(
            url="/backends?msg=" + quote(
                "Changing where this connects needs a Jellyfin administrator "
                "account."), status_code=303)
    form = await request.form()
    submitted = {key: str(value) for key, value in form.items()}
    refused = setup.save(submitted)
    # Probed straight away rather than on the next page load. "Saved" is not
    # the news anybody wants; "and it answers" is.
    backends.forget()
    # Only about the backend whose fields were on this form. Every form on the
    # page posts here, and reporting all four would put three unrelated
    # "could not be reached" sentences into a live region after somebody saved
    # one -- burying the answer to the question they actually asked.
    touched = {name.split("_", 1)[0].lower() for name in submitted
               if "_" in name and name in settings.WRITABLE}
    said = []
    for status in backends.statuses(force=True):
        if status.name not in touched or not status.configured:
            continue
        said.append(f"{status.name}: "
                    + ("answered." if status.reachable
                       else status.detail or "did not answer."))
    message = " ".join(refused + said) or "Saved."
    return RedirectResponse(url=f"/backends?msg={quote(message)}",
                            status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", medium: str = "", unit: str = "",
          msg: str = ""):
    """Search, and everything this account is currently waiting for."""
    if setup.needs_setup():
        # Nothing can be asked of a Jellyfin this service has no credential
        # for -- including who somebody is -- so a first run goes to the one
        # page that does not need one.
        return RedirectResponse(url="/setup", status_code=303)
    try:
        user = viewer(request)
    except LookupError as exc:
        return _signin_page(request, detail=str(exc), status=401)

    offered = media.available()
    medium = medium if medium in offered else (next(iter(offered), ""))
    found = offered.get(medium)
    unit = unit if found and unit in found.units else (
        found.units[0] if found else "")

    results = (wants.search(q.strip(), medium, unit, user)
               if q.strip() and found else [])
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "user": user,
            "media": list(offered.values()),
            "medium": medium,
            "unit": unit,
            "query": q,
            "results": results,
            "requests": wants.states(user),
            "message": msg,
            "allowance": {key: wants.allowance(user, key) for key in offered},
        })


@app.post("/want")
def post_want(request: Request, medium: str = Form(...),
              item_key: str = Form(...), unit: str = Form(""),
              title: str = Form(""), year: str = Form(""),
              artist: str = Form(""), source: str = Form(""),
              ref: str = Form("")):
    """Ask for one thing, then send the browser back to the list.

    A redirect rather than a rendered response so that a reload does not
    re-submit, and so the answer arrives as a whole page a reader can read
    from the top.
    """
    try:
        user = viewer(request)
    except LookupError as exc:
        return _back(medium, f"Nextup could not work out who you are. {exc}")
    hit = {"title": title, "year": year, "artist": artist,
           "source": source, "ref": ref}
    try:
        _, message = wants.want(user, medium, item_key, unit, hit)
    except wants.Denied as denied:
        message = str(denied)
    return _back(medium, message)


@app.post("/cancel")
def post_cancel(request: Request, medium: str = Form(...),
                item_key: str = Form(...)):
    """Stop looking for one thing."""
    try:
        user = viewer(request)
    except LookupError as exc:
        return _back(medium, f"Nextup could not work out who you are. {exc}")
    _, message = wants.cancel(user, medium, item_key)
    return _back(medium, message)


BOOK = "book"

#: The shelves this page can draw, in the order it draws them, and what to
#: call each. Books last: they are the medium with two shelves and much the
#: longest page, so putting them first buries the other two.
#:
#: Named here rather than read from the media registry because that registry
#: only carries a medium whose acquisition tool is configured, and a film
#: shelf needs no Radarr.
DISCOVER_LABELS = {media.MOVIE: "Films", media.SERIES: "Series", BOOK: "Books"}


def discover_media() -> list[str]:
    """Which media have a shelf on this installation, in page order."""
    rankable = set(recommendations.offered())
    books = BOOK in media.available()
    return [key for key in DISCOVER_LABELS
            if key in rankable or (key == BOOK and books)]


def _back_to_discover(medium: str, message: str = "",
                      **extra: str) -> RedirectResponse:
    """Back to one shelf, carrying one sentence about what just happened."""
    query = {"medium": medium, "msg": message,
             **{k: v for k, v in extra.items() if v}}
    trail = "&".join(f"{k}={quote(v)}" for k, v in query.items() if v)
    return RedirectResponse(f"/discover{'?' + trail if trail else ''}",
                            status_code=303)


def _back_to_books(message: str = "", **extra: str) -> RedirectResponse:
    return _back_to_discover(BOOK, message, **extra)


@app.get("/books", response_class=HTMLResponse)
def get_books(request: Request, msg: str = "", undo_asin: str = "",
              undo_title: str = ""):
    """Where the book shelves used to live.

    Kept because it is a bookmarkable address that was published, and because
    the one thing worse than moving a page is moving it into a 404.
    """
    return _back_to_discover(BOOK, msg, undo_asin=undo_asin,
                             undo_title=undo_title)


@app.get("/discover", response_class=HTMLResponse)
def get_discover(request: Request, medium: str = "", msg: str = "",
                 undo_asin: str = "", undo_title: str = ""):
    """What to play next, in whichever medium this server can rank.

    Its own page rather than a section of the search page. The search page's
    shape is "ask for a thing"; this one is "here is what was chosen for you",
    and eighty ranked rows with their reasons underneath the search form would
    bury the form for everybody and bury it worst for somebody reading top to
    bottom.

    One page for all three, and one noun. Books arrived first and had a page
    of their own; films and television had a working ranker with no page at
    all. Two pages answering the same question in two vocabularies is what
    this replaces.
    """
    if setup.needs_setup():
        return RedirectResponse(url="/setup", status_code=303)
    try:
        user = viewer(request)
    except LookupError as exc:
        return _signin_page(request, detail=str(exc), status=401)

    shelves = discover_media()
    if not shelves:
        return templates.TemplateResponse(
            request=request, name="discover.html", status_code=404,
            context={"user": user, "shelves": [], "medium": "", "rows": [],
                     "pending": "", "message":
                     "This installation has nothing to recommend yet. "
                     "Recommendations come from a Jellyfin film, television "
                     "or books library, and this server has none of those."})
    # An unknown medium falls back to the first shelf rather than refusing, as
    # the search page does with its own picker: a stale link is a person in
    # the right place with the wrong query string.
    medium = medium if medium in shelves else shelves[0]
    context = {"user": user, "medium": medium, "message": msg,
               "shelves": [{"key": key, "label": DISCOVER_LABELS[key],
                            "current": key == medium} for key in shelves]}
    if medium == BOOK:
        context |= _book_shelves(user, undo_asin, undo_title)
    else:
        context |= _owned_shelf(user, medium)
    return templates.TemplateResponse(
        request=request, name="discover.html", context=context)


def _book_shelves(user: jellyfin.User, undo_asin: str,
                  undo_title: str) -> dict:
    """The two book shelves: what to read next, and what to add."""
    data = book_shelves.result(user)
    asked_for = {row["asin"] for row in book_store.requests_for(user.key)
                 if not row["fulfilled_at"]}
    return {
        "own": data.get("own") or [],
        "discover": data.get("discover") or [],
        "asked_for": asked_for,
        "allowance": wants.allowance(user, BOOK),
        "playlist_name": data.get("playlist_name") or config.PLAYLIST_NAME,
        "computed_at": _when(book_store.last_run(user.key)),
        "undo_asin": undo_asin,
        "undo_title": undo_title,
        "pending": "",
    }


def _owned_shelf(user: jellyfin.User, medium: str) -> dict:
    """One ranked shelf of what the library already holds.

    Never built in front of the person who asked. A cold film build is twelve
    seconds of Jellyfin on this library, and a page that hangs that long reads
    as broken rather than as busy -- so the build runs behind the answer and
    `pending` is what the page says meanwhile.
    """
    rows, pending = recommendations.shelf_or_start(user, medium=medium)
    return {"rows": (rows or {}).get("recommendations") or [],
            "pending": pending}


def _when(run) -> str:
    """A finished run as a plain sentence fragment, or nothing at all."""
    if not run or not run["finished_at"]:
        return ""
    return time.strftime("on %d %B at %H:%M", time.localtime(run["finished_at"]))


@app.post("/books/want")
def post_book_want(request: Request, asin: str = Form(...),
                   title: str = Form("")):
    try:
        user = viewer(request)
    except LookupError as exc:
        return _back_to_books(f"Nextup could not work out who you are. {exc}")
    try:
        _, message = wants.want(user, BOOK, asin, BOOK, {"title": title})
    except wants.Denied as denied:
        message = str(denied)
    book_shelves.forget_asin(asin)
    return _back_to_books(message)


@app.post("/books/dismiss")
def post_book_dismiss(request: Request, asin: str = Form(...),
                      title: str = Form("")):
    """Hide one suggestion, and offer to put it back."""
    try:
        user = viewer(request)
    except LookupError as exc:
        return _back_to_books(f"Nextup could not work out who you are. {exc}")
    book_wants.dismiss(user, asin)
    # Taken off this account's shelf now, and the shelf marked for a rebuild
    # behind the next read. Invalidating instead threw away the stored copy as
    # well, so the redirect that follows paid for a whole cold rebuild -- twelve
    # to twenty-two seconds, in front of somebody who had just pressed a button
    # labelled "Not this one".
    book_shelves.forget_asin(asin, user_key=user.key)
    book_shelves.expire(user.key)
    return _back_to_books(
        f"Hidden: {title or asin}.", undo_asin=asin, undo_title=title)


@app.post("/books/restore")
def post_book_restore(request: Request, asin: str = Form(...)):
    try:
        user = viewer(request)
    except LookupError as exc:
        return _back_to_books(f"Nextup could not work out who you are. {exc}")
    restored = book_wants.restore(user, asin)
    # A row cannot be put back into a cached shelf it was removed from, so this
    # one really does need the rebuild -- but behind the answer, not in front
    # of it. The stored shelf is served meanwhile, without the book, and the
    # book returns on the read after that.
    book_shelves.expire(user.key)
    return _back_to_books("Put back." if restored
                          else "That book was not hidden.")


@app.post("/discover/refresh")
def post_discover_refresh(request: Request, medium: str = Form(BOOK)):
    """Throw one shelf away so the next load rebuilds it.

    Not a rebuild in front of the person who asked: books are twelve seconds,
    nine of them one Jellyfin listing, and films are much the same. A page
    that hangs that long reads as broken. The next load serves the stored
    answer where there is one and rebuilds behind it, which is what `expire`
    leaves in place and `invalidate` did not.
    """
    try:
        user = viewer(request)
    except LookupError as exc:
        return _back_to_discover(
            medium, f"Nextup could not work out who you are. {exc}")
    if medium == BOOK:
        book_shelves.expire(user.key)
    elif medium in recommendations.SUPPORTED_MEDIA:
        recommendations.expire(user, medium=medium)
    else:
        return _back_to_discover(medium, "There is no such shelf.")
    return _back_to_discover(
        medium, "Working out new recommendations. They will appear here "
                "shortly.")


def _back(medium: str, message: str) -> RedirectResponse:
    """Back to the index, carrying one sentence about what just happened."""
    return RedirectResponse(
        f"/?medium={quote(medium)}&msg={quote(message)}", status_code=303)
