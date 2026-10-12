"""The first search can choose a book series without an intermediate page."""
import re
import time
from html import unescape

import harness

harness.setup(RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
              RADARR_QUALITY_PROFILE_ID="1",
              SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k",
              SONARR_QUALITY_PROFILE_ID="1",
              BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              LISTENARR_URL="http://listenarr.invalid", JELLYFIN_USER="")

from fastapi.testclient import TestClient  # noqa: E402

from app import (backends, buskarr, imports, jellyfin, main, media,  # noqa: E402
                 podfetch, podcasts, radarr, sessions, sonarr, store, wants)
from app.books import adapter, series  # noqa: E402

check = harness.Check("web search")
USER = jellyfin.User(id="listener", name="listener", is_admin=True)
jellyfin.user_from_token = lambda token: USER
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: ["lib-" + medium]
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
podfetch._presence = (True, time.monotonic())
media.owned = lambda *args, **kwargs: jellyfin.Owned()
wants.states = lambda user, **kwargs: []
media.forget()

searches = []
radarr.search = lambda query, *args: searches.append(("movie", "movie", query)) or []
sonarr.search = lambda query, *args: searches.append(("series", "series", query)) or []
buskarr.search = lambda query, unit, *args: searches.append(("music", unit, query)) or []
podcasts.search = lambda query, user: searches.append(("podcast", "podcast", query)) or []
adapter.book_search.search = lambda user, query: (
    searches.append(("book", "book", query)) or [])


def search_series(user, query):
    searches.append(("book", "series", query))
    return [{"series": "The Archive", "catalogueName": "The Archive",
             "have": [{}], "onOrder": [], "missing": [{}, {}]}]


series.plans_for = search_series
asked = []
series.want_series = lambda user, name, anchor, **kwargs: (
    asked.append((user.key, name)) or
    {"requested": 2, "message": "Asked for 2 books from The Archive."})

client = TestClient(main.app, follow_redirects=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("listener-token", USER.id))


def search_form(page):
    return re.search(r'<form method="get" action="/"[^>]*>(.*?)</form>',
                     page.text, re.S).group(1)


def selected(page):
    return re.findall(r'<option value="([^"]+)"\s+selected>', search_form(page))


first = client.get("/")
check.equal(first.status_code, 200, "the initial page renders")
form = search_form(first)
options = {unescape(label): value for value, label in
           re.findall(r'<option value="([^"]+)"[^>]*>([^<]+)</option>', form)}
check.equal(set(options), {"Films", "TV shows", "Songs", "Albums", "Artists",
                           "Books", "Book series", "Podcasts"},
            "every search type is offered before any search")
check.that('<label for="search_type">' in form
           and '<select id="search_type" name="search_type">' in form,
           "the picker has a native control and an associated label")
check.equal(len(selected(first)), 1, "there is one selected search type")

# Submit values taken from the actual HTML, as a browser without JavaScript
# does. The catalogue stubs sit below wants.search, so the route must choose
# the right medium and unit rather than merely echoing the picker value.
for label, medium, unit in [
        ("Book series", "book", "series"), ("Books", "book", "book"),
        ("TV shows", "series", "series"), ("Songs", "music", "track"),
        ("Albums", "music", "album"), ("Artists", "music", "artist"),
        ("Films", "movie", "movie"), ("Podcasts", "podcast", "podcast")]:
    searches.clear()
    page = client.get("/", params={"search_type": options[label], "q": "archive"})
    check.equal(page.status_code, 200, f"{label} search renders")
    check.equal(searches, [(medium, unit, "archive")],
                f"{label} reaches its catalogue on the first submit")
    check.equal(selected(page), [options[label]], f"{label} stays selected")
    if label == "Book series":
        series_page = page

check.that("1 of 3 in your library, 2 to ask for." in series_page.text,
           "the book series result explains which books are missing")
result_form = re.search(r'<form method="post" action="/want">(.*?)</form>',
                        series_page.text, re.S).group(1)
fields = {unescape(name): unescape(value) for name, value in
          re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', result_form)}
check.equal((fields["medium"], fields["unit"], fields["item_key"]),
            ("book", "series", "The Archive"),
            "the result form requests the named book series")
requested = client.post("/want", data=fields)
check.equal(requested.status_code, 303, "asking returns to the website")
check.equal(asked, [(USER.key, "The Archive")],
            "the website request reaches the book series engine")

for medium, unit in [("book", "series"), ("book", "book"),
                     ("series", "series"), ("music", "album")]:
    searches.clear()
    old_link = client.get("/", params={"medium": medium, "unit": unit, "q": "archive"})
    check.equal(searches, [(medium, unit, "archive")], "existing search URLs still work")
    check.equal(selected(old_link), [f"{medium}:{unit}"],
                "existing URLs select their corresponding search type")

searches.clear()
client.get("/", params={"search_type": options["Books"], "medium": "music",
                        "unit": "series", "q": "archive"})
check.equal(searches, [("book", "book", "archive")],
            "the picker wins over stale medium and unit parameters")

# Import replacements keep the list's type even if a URL also names another
# search type. Their form still carries the original list and source row.
store.put_import("correcting", USER.key, "music", imports.READY, 1,
                 {"medium": "music", "unit": "track", "filename": "songs.csv"})
store.put_import_row("correcting", {"line": 2, "title": "Archive", "artist": "",
                                   "label": "Archive", "state": imports.MISSING})
searches.clear()
correction = client.get("/", params={"import_id": "correcting", "import_line": 2,
                                     "search_type": options["Book series"], "q": "archive"})
check.equal(correction.status_code, 200, "import replacement search renders")
check.equal(searches, [("music", "track", "archive")],
            "import replacement searches keep the list's type")
check.that('name="import_id" value="correcting"' in search_form(correction)
           and 'name="unit" value="track"' in search_form(correction),
           "the replacement form preserves its list and scope")

# The picker follows what the deployment offers, including partial installs.
media.available = lambda: {media.SERIES: media.Medium(
    media.SERIES, "TV shows", ("series",), 3, ("tv",))}
tv_only = client.get("/")
check.that("Book series</option>" not in search_form(tv_only)
           and "TV shows</option>" in search_form(tv_only),
           "a TV-only install offers no book search")
media.available = lambda: {}
check.that("Nothing to ask for" in client.get("/").text,
           "an unconfigured install still renders")

harness.cleanup()
raise SystemExit(check.report())
