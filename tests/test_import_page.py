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

buskarr.search = lambda q, unit, limit, sources=(): [
    buskarr._result(dict(row, source="deezer"), unit) for row in CATALOGUE
    if any(word in f"{row['artist']} {row['title']}".casefold()
           for word in q.casefold().split() if len(word) > 3)
] if unit == "album" else []

asked: list[str] = []
buskarr.add = lambda unit, hit, by, bulk=False: (
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
check.that("exact matches are asked for straight away" in " ".join(form.text.split()),
           "saying before anything is uploaded what uploading does")
check.that('name="unit" value="album"' in form.text,
           "and offering music's three units, which no other medium has")

# --- uploading --------------------------------------------------------------
LIST_WITH_NEAR_MISS = LIST + "Bill Evans,Kind of Blue\n"
posted = client.post("/import", data={"medium": "music", "unit": "album"},
                     files={"listing": ("collection.csv",
                                        LIST_WITH_NEAR_MISS.encode(),
                                        "text/csv")})
check.equal(posted.status_code, 303, "an uploaded list redirects to itself")
where = posted.headers["location"]
check.that(where.startswith("/import/"), "at an address of its own, so a "
                                         "reload does not re-upload the file")

review = settle(where, "All looked up")
check.equal(review.status_code, 200, "the list's page answers")
check.equal(sorted(asked), ["Kind of Blue", "Rumours"],
            "the two exact matches were asked for as the list was looked up, "
            "with nobody pressing anything")
flat = " ".join(review.text.split())
check.that("<li>2 asked for</li>" in flat, "the page counts what was asked for")
check.that("Kind of Blue by Miles Davis" in flat and "Rumours by Fleetwood Mac" in flat,
           "and names it")
check.that("Search for A Record Nobody Pressed by Nobody At All" in flat,
           "a row nothing matched is listed with a way to look for it, rather "
           "than dropped")
check.that("collection.csv" in review.text,
           "under the name of the file it came from")
check.that("First line read as column headings: Artist, Album." in review.text,
           "saying which line was taken for headings, so a title lost to a "
           "wrong guess can be noticed")

# The label is the assertion. A column of forty tick boxes each labelled only
# with a title is unreadable; what has to be in the one string is what the
# file said and what it was matched to.
check.that("Kind of Blue by Miles Davis. Line 5 of your file: "
           "Kind of Blue by Bill Evans</label>" in review.text,
           "the near miss is offered with both what was matched and which "
           "line of the file it came from")
check.equal(review.text.count('name="line"'), 1,
            "only the near miss waits for a tick")
check.that('name="line"' in review.text and " checked" not in review.text,
           "and it is not ticked: nothing is asked for on a guess")

# --- somebody else's list ---------------------------------------------------
signed_in = KID
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("kid-token", KID.id))
theirs = client.get(where)
check.equal(theirs.status_code, 303,
            "another account asking for that list is sent away")
check.equal(theirs.headers["location"].split("?")[0], "/import",
            "to the form, with no hint that the list exists")
asked.clear()
stolen = client.post(where + "/confirm", data={"line": "5"})
check.equal(stolen.headers["location"].split("?")[0], "/import",
            "and cannot ask for anything on it either")
check.equal(asked, [], "so nothing was asked for on its owner's allowance")

signed_in = MATT
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", MATT.id))
recent = client.get("/import")
check.that(f'<a href="{where}">collection.csv</a>' in recent.text,
           "the import page lists the account's recent lists, so one can be "
           "found again after leaving it")

# --- deciding about the near misses -----------------------------------------
nothing = client.post(where + "/confirm", data={})
check.equal(nothing.status_code, 303,
            "a form with every row unticked posts no field at all, and is a "
            "redirect rather than a missing-parameter error")
check.that("Nothing was ticked" in unquote(nothing.headers["location"]),
           "and says so")

left = client.post(where + "/leave")
check.equal(left.status_code, 303, "leaving the rest redirects back")
done = settle(where, "Close matches left out")
check.that("Search for Kind of Blue by Bill Evans" in " ".join(done.text.split()),
           "a near miss left out is still listed, with a way to look for it")
check.that('name="line"' not in done.text, "and nothing is offered any more")
check.that("See everything you have asked for" in done.text,
           "and the page points at the list they are now on")

# --- a file this cannot read ------------------------------------------------
refused = client.post("/import", data={"medium": "music", "unit": "album"},
                      files={"listing": ("odd.csv", b"Year,Artist\n2020,Someone\n",
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
page = settle(pasted.headers["location"], "All looked up")
check.that("Pasted list" in page.text,
           "and says where it came from, having no filename to show")
flat = " ".join(page.text.split())
check.that("<li>1 already here or already asked for</li>" in flat
           and "Kind of Blue by Miles Davis" in flat,
           "with 'Artist - Title' read apart -- which is how a list in a "
           "message is written -- and the row recognised as one already on "
           "this account's list rather than asked for twice")
check.that('name="line"' not in page.text,
           "so there is no tick box on it at all")

# --- the two states nobody can hold still long enough to look at -----------
# `asking` lasts as long as the requests take and `failed` needs a container
# to be restarted mid-pass, so both are written straight into the store. They
# are pages a person will see on a bad day, and a template that raises on one
# of them would never be found any other way.
store.put_import("half-way", MATT.id, "music", imports.ASKING, 9,
                 {"medium": "music", "unit": "album",
                  "filename": "long.csv", "rows": [], "chosen": 7})
store.touch_import("half-way", 4)
page = client.get("/import/half-way")
check.equal(page.status_code, 200, "a list part way through asking renders")
check.that("4 of 7 sent so far" in page.text,
           "saying how far it has got in things, not in a percentage")

store.put_import("stopped", MATT.id, "music", imports.FAILED, 9,
                 {"medium": "music", "unit": "album", "filename": "long.csv",
                  "rows": [], "error": "The service was restarted."})
page = client.get("/import/stopped")
check.equal(page.status_code, 200, "so does one that stopped")
check.that("The service was restarted." in page.text
           and 'role="alert"' in page.text,
           "with the reason announced rather than left on the page to be "
           "found")

# --- files the csv module used to refuse with a 500 --------------------------
old_mac = client.post("/import", data={"medium": "music", "unit": "album"},
                      files={"listing": ("old.csv",
                                         b"Artist,Album\rMiles Davis,Kind of Blue\r",
                                         "text/csv")})
check.equal(old_mac.status_code, 303,
            "a file whose lines end in a bare carriage return is accepted")
check.that(old_mac.headers.get("location", "").startswith("/import/"),
           "and goes on to be looked up")

unclosed = 'Artist,Album\n"Miles Davis,Kind of Blue\n' + "x,y\n" * 40000
broken = client.post("/import", data={"medium": "music", "unit": "album"},
                     files={"listing": ("broken.csv", unclosed.encode(),
                                        "text/csv")})
check.equal(broken.status_code, 303,
            "a file with a quote that never closes comes back to the form")
check.that("quotation mark" in unquote(broken.headers.get("location", "")),
           "saying what to look for")

# --- what the review says about rows that found nothing --------------------
def row(line, title, state, detail, hit=None):
    return {"line": line, "title": title, "artist": "", "year": "",
            "label": title, "state": state, "detail": detail, "hit": hit,
            "others": 0}


store.put_import("looked-up", MATT.id, "series", imports.READY, 3,
                 {"medium": "series", "unit": "series", "filename": "shows.csv",
                  "rows": [
                      row(2, "Lost", imports.MATCHED, "",
                          {"itemKey": "tvdb:1", "title": "Lost",
                           "year": 2004, "unit": "series"}),
                      row(3, "Nothing Like It", imports.MISSING,
                          imports.NO_MATCH),
                      row(4, "Unlucky", imports.MISSING,
                          "The search for this one failed. Try it again "
                          "later."),
                  ]})
page = client.get("/import/looked-up")
check.equal(page.status_code, 200, "a list of series renders")
check.that("3 series." in page.text and "seriess" not in page.text,
           "and counts them as series, not seriess")
check.that("Search for Unlucky</a>. The search for this one failed"
           in page.text,
           "a row whose search failed says so, rather than reading as a "
           "title the catalogue does not have")
check.that(imports.NO_MATCH not in page.text,
           "while a row with no match is not told so again under the No match "
           "heading")

harness.cleanup()
raise SystemExit(check.report())
