"""Importing a list in a browser: the form, the review, and the report.

What is pinned here is the two-step shape. An upload must not acquire
anything, the review must be readable as a list -- every tick box carrying
both what the file said and what it was matched to -- and only a posted
confirmation may spend anybody's allowance.
"""
import time

import harness

harness.setup(
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    MUSIC_DAILY_CAP="3", MUSIC_ALBUM_COST="1", MUSIC_ARTIST_COST="3",
    MUSIC_TRACK_COST="1", IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="",
)

from urllib.parse import unquote  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import (arr, buskarr, imports, jellyfin, main, media,  # noqa: E402
                 sessions, store)

check = harness.Check("import page")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
KID = jellyfin.User(id="user-kid", name="kid", is_admin=False)
jellyfin.credential_rejected = lambda force=True: False

#: Whoever the cookie below currently stands for. Both accounts sign in the
#: same way, and the page must tell them apart by more than the address they
#: typed.
signed_in = MATT
jellyfin.user_from_token = lambda token: signed_in

media._registry = {
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3,
                              ("lib-music",)),
}
media._registry_built_at = time.monotonic()
media._registry_settled = True

CATALOGUE = [
    {"title": "Kind of Blue", "artist": "Miles Davis", "ref": "a1"},
    {"title": "Rumours", "artist": "Fleetwood Mac", "ref": "a2"},
]

buskarr.search = lambda q, unit, limit: [
    buskarr._result(dict(row, source="deezer"), unit) for row in CATALOGUE
    if any(word in f"{row['artist']} {row['title']}".casefold()
           for word in q.casefold().split() if len(word) > 3)
] if unit == "album" else []

asked: list[str] = []
buskarr.add = lambda unit, hit, by: (
    asked.append(hit.get("title", ""))
    or arr.AddResult(True, "Sent to buskarr.", "job:1", hit.get("title", "")))

client = TestClient(main.app, raise_server_exceptions=False,
                    follow_redirects=False)
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", MATT.id))

LIST = ("Artist,Album\n"
        "Miles Davis,Kind of Blue\n"
        "Fleetwood Mac,Rumours\n"
        "Nobody At All,A Record Nobody Pressed\n")


def settle(url: str, until: str, seconds: float = 10.0):
    """Reload a page until it says the thing, the way a person would."""
    deadline = time.monotonic() + seconds
    page = client.get(url)
    while until not in page.text and time.monotonic() < deadline:
        time.sleep(0.05)
        page = client.get(url)
    return page


# --- the form ---------------------------------------------------------------
form = client.get("/import")
check.equal(form.status_code, 200, "the import form answers")
check.that('enctype="multipart/form-data"' in form.text,
           "and can carry a file")
check.that('<a href="/import">Import a list</a>' in form.text,
           "with a way in from the nav on every page, because a page nobody "
           "can find is a page that does not exist")
check.that("Nothing is asked for yet" in form.text,
           "saying before anything is uploaded that uploading acquires nothing")
check.that('name="unit" value="album"' in form.text,
           "and offering music's three units, which no other medium has")

# --- uploading --------------------------------------------------------------
posted = client.post("/import", data={"medium": "music", "unit": "album"},
                     files={"listing": ("collection.csv", LIST.encode(),
                                        "text/csv")})
check.equal(posted.status_code, 303, "an uploaded list redirects to itself")
where = posted.headers["location"]
check.that(where.startswith("/import/"), "at an address of its own, so a "
                                         "reload does not re-upload the file")
check.equal(asked, [], "and nothing at all has been asked for yet")

review = settle(where, "What matched")
check.equal(review.status_code, 200, "the review page answers")
check.that("Kind of Blue" in review.text and "Rumours" in review.text,
           "listing what matched")
check.that("A Record Nobody Pressed" in review.text,
           "and what did not, rather than dropping it")
check.that("collection.csv" in review.text,
           "under the name of the file it came from")

# The label is the assertion. A column of forty tick boxes each labelled only
# with a title is unreadable; what has to be in the one string is what the
# file said and what it was matched to.
check.that("Ask for Kind of Blue by Miles Davis" in review.text
           and "line 2 of the file says Kind of Blue by Miles Davis"
           in review.text,
           "every tick box says both what was matched and which line of the "
           "file it came from")
check.that(review.text.count('name="line"') == 2,
           "only the rows that matched something are offered")
check.that(review.text.count("checked") == 2,
           "and both exact matches are ticked ready")

# --- somebody else's list ---------------------------------------------------
signed_in = KID
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("kid-token", KID.id))
theirs = client.get(where)
check.equal(theirs.status_code, 303,
            "another account asking for that list is sent away")
check.equal(theirs.headers["location"].split("?")[0], "/import",
            "to the form, with no hint that the list exists")
stolen = client.post(where + "/confirm", data={"line": "2"})
check.equal(stolen.headers["location"].split("?")[0], "/import",
            "and cannot confirm it either")
check.equal(asked, [], "so nothing was asked for on its owner's allowance")

signed_in = MATT
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", MATT.id))

# --- confirming -------------------------------------------------------------
nothing = client.post(where + "/confirm", data={})
check.equal(nothing.status_code, 303,
            "a form with every row unticked posts no field at all, and is a "
            "redirect rather than a missing-parameter error")
check.that("Nothing was ticked" in unquote(nothing.headers["location"]),
           "and says so")
check.equal(asked, [], "having asked for nothing")

done = client.post(where + "/confirm", data={"line": ["2", "3"]})
check.equal(done.status_code, 303, "confirming redirects back to the list")
report = settle(where, "What was asked for")
check.that(sorted(asked) == ["Kind of Blue", "Rumours"],
           f"and the two ticked rows were asked for, not {asked}")
check.that("2 asked for" in report.text, "the report counts them")
check.that("See everything you have asked for" in report.text,
           "and points at the list they are now on")

# --- a file this cannot read ------------------------------------------------
refused = client.post("/import", data={"medium": "music", "unit": "album"},
                      files={"listing": ("odd.csv", b"Column A,Column B\nx,y\n",
                                         "text/csv")})
check.equal(refused.status_code, 303, "a file with no title column comes back")
check.that("heading" in unquote(refused.headers["location"]),
           "naming the headings it would have understood")

empty = client.post("/import", data={"medium": "music", "unit": "album"})
check.that("Choose a file" in unquote(empty.headers["location"]),
           "and posting the form with neither a file nor a paste says so")

# --- pasting instead of uploading -------------------------------------------
pasted = client.post("/import", data={"medium": "music", "unit": "album",
                                      "pasted": "Miles Davis - Kind of Blue"})
check.equal(pasted.status_code, 303, "a pasted list is accepted too")
page = settle(pasted.headers["location"], "What matched")
check.that("A pasted list" in page.text,
           "and says where it came from, having no filename to show")
check.that("Kind of Blue by Miles Davis — already asked for" in page.text,
           "with 'Artist - Title' read apart -- which is how a list in a "
           "message is written -- and the row recognised as one already on "
           "this account's list rather than offered to be asked for twice")
check.that('name="line"' not in page.text,
           "so there is no tick box on it at all")

harness.cleanup()
raise SystemExit(check.report())
