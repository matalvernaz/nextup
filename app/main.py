"""The web application: browser pages, and the JSON API mounted beneath them.

The pages are server-rendered HTML with real forms and no JavaScript in any
path that does something. That is not minimalism for its own sake -- a screen
reader is the primary interface here, and a page that has already finished
rendering when it arrives is one a reader can simply read.

Identity for these pages comes from the sign-in proxy's forwarded header.
Identity for the JSON API does not, and never may: see `api.caller`.
"""
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, config, jellyfin, logs, media, store, wants

log = logs.get("main")

app = FastAPI(title="nextup")
app.include_router(api.router)
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
def healthz() -> str:
    """Liveness only. Deliberately does not touch Jellyfin or any backend --
    a health check that fails when a downstream is down turns one outage into
    a restart loop."""
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
    user = viewer(request)
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
    user = viewer(request)
    _, message = wants.cancel(user, medium, item_key)
    return _back(medium, message)


def _back(medium: str, message: str) -> RedirectResponse:
    """Back to the index, carrying one sentence about what just happened."""
    return RedirectResponse(
        f"/?medium={quote(medium)}&msg={quote(message)}", status_code=303)
