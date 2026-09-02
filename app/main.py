"""The web application: browser pages, and the JSON API mounted beneath them.

The pages are server-rendered HTML with real forms and no JavaScript in any
path that does something. That is not minimalism for its own sake -- a screen
reader is the primary interface here, and a page that has already finished
rendering when it arrives is one a reader can simply read.

Identity for these pages comes from the sign-in proxy's forwarded header.
Identity for the JSON API does not, and never may: see `api.caller`.
"""
import os
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, config, jellyfin, logs, media, selfcheck, store, wants

log = logs.get("main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _rekey_ledger_once()
    selfcheck.watch()
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
    try:
        names = jellyfin.all_users()
    except Exception as exc:
        raise RuntimeError(
            "cannot rekey the request ledger: Jellyfin did not answer") from exc
    store.rekey_users(names)


app = FastAPI(title="nextup", lifespan=lifespan)
app.include_router(api.router)

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
app.mount("/static", StaticFiles(directory="app/static"), name="static")

store.init()


def viewer(request: Request) -> jellyfin.User:
    """Who is looking at this page, according to the sign-in proxy.

    Falls back to `JELLYFIN_USER` so a local or direct deployment still works.
    That fallback is why this resolver is not shared with the JSON API, which
    is not behind the proxy: there it would hand any caller the owner's list.
    """
    name = (request.headers.get(config.AUTH_USER_HEADER) or "").strip()
    return jellyfin.user(name or None)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz(response: Response) -> str:
    """Liveness, plus the one upstream fault that needs a person.

    A check that fails whenever a downstream is unreachable turns one outage
    into a restart loop, so this does not probe for reachability: an arr being
    down, or Jellyfin being slow, is not this container's problem to report. A
    credential Jellyfin has stopped accepting is different. Every route here
    resolves a caller through Jellyfin, none of them can work, and it will not
    come right on its own -- and without this the container reports healthy for
    the whole time it is useless. The share gateway this service borrowed its
    shape from ran that way for days.
    """
    if jellyfin.credential_rejected():
        log.warning("unhealthy: Jellyfin is refusing this service's API key")
        response.status_code = 503
        return "Jellyfin is refusing this service's API key."
    return "ok"


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", medium: str = "", unit: str = "",
          msg: str = ""):
    """Search, and everything this account is currently waiting for."""
    try:
        user = viewer(request)
    except LookupError as exc:
        return templates.TemplateResponse(
            request=request, name="signin.html", status_code=403,
            context={"detail": str(exc)})

    offered = media.available()
    medium = medium if medium in offered else (next(iter(offered), ""))
    found = offered.get(medium)
    unit = unit if found and unit in found.units else (
        found.units[0] if found else "")

    results = wants.search(q.strip(), medium, unit) if q.strip() and found else []
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


def _back(medium: str, message: str) -> RedirectResponse:
    """Back to the index, carrying one sentence about what just happened."""
    return RedirectResponse(
        f"/?medium={quote(medium)}&msg={quote(message)}", status_code=303)
