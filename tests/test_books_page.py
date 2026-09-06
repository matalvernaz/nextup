"""The book shelves on Discover, and the upkeep that no longer needs a visitor.

Three things are being pinned. The shelves themselves -- which had no page at
all once, and whose absence cost the Jellyfin playlist a day of being written
-- the address they used to live at, which still has to lead somewhere, and
the scheduled pass that means the playlist's freshness never depends on a
person visiting again.
"""
import harness

harness.setup(LISTENARR_URL="http://listenarr.invalid:4545",
              PLAYLIST_OWNER="matt", JELLYFIN_USER="")

from fastapi.testclient import TestClient  # noqa: E402

from app import backends, jellyfin, main, media, sessions, store  # noqa: E402
from app.books import engine, shelves, upkeep  # noqa: E402
from app.books import store as book_store  # noqa: E402

check = harness.Check("books page")
store.init()

MATT = jellyfin.User(id="user-matt", name="matt", is_admin=True)
jellyfin.user_from_token = lambda token: MATT
jellyfin.credential_rejected = lambda force=True: False
backends.status = lambda medium, force=False: backends.Status(
    medium, medium, configured=True, reachable=True)
media.jellyfin.library_ids = lambda medium: (
    ["lib-books"] if medium == "book" else [])
media.forget()

written: list[tuple[str, list[str]]] = []
jellyfin.set_playlist = lambda uid, name, ids: (
    written.append((name, list(ids))) or "playlist-1")

SHELF = {
    "own": [{"id": "i1", "title": "A Book You Have",
             "authors": ["Someone"], "series": "A Series",
             "why": ["because of another one you finished"]}],
    "discover": [
        {"asin": "B0NEW", "title": "One You Do Not",
         "authors": ["Another"], "narrators": ["A Reader"],
         "series": "A Series", "series_position": "2", "runtime_min": 600,
         "description": "<p>Tags that must not reach the page raw.</p>",
         "why": ["Audible lists it alongside one you own"]},
        {"asin": "B0ASKED", "title": "Already On The Way",
         "authors": ["Third"], "narrators": [], "why": []},
    ],
    "owned_index": ({"B0OWNED"}, {"a book you have": {"someone"}}),
    "playlist_name": "Next Read",
    "playlist_id": None,
    "seeds": 1, "library": 2, "ratings": 0,
}
def fake_run(user, update_playlist=True):
    """Stands in for the real build, which is twelve seconds of Jellyfin.

    Writes the playlist through the module attribute, as the real one does, so
    that patching `jellyfin.set_playlist` actually intercepts it.
    """
    if update_playlist:
        jellyfin.set_playlist(user.id, "Next Read", ["i1"])
    return dict(SHELF)


engine.run = fake_run

# One book already asked for, so the page must offer no button for it.
book_store.record_request("user-matt", "B0ASKED", "Already On The Way",
                          ["Third"])

client = TestClient(main.app, raise_server_exceptions=False,
                    follow_redirects=False)
# A signed session, which is how an installation with no sign-in proxy resolves
# anybody. JELLYFIN_USER is empty here, as a household deployment leaves it.
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("a-token", MATT.id))

moved = client.get("/books")
check.equal(moved.status_code, 303,
            "the address the shelves used to live at still leads somewhere")
check.equal(moved.headers["location"], "/discover?medium=book",
            "and it leads to the same shelves under the one Discover noun")

page = client.get("/discover?medium=book")
check.equal(page.status_code, 200, "the page renders")
body = page.text

check.that("A Book You Have" in body and "One You Do Not" in body,
           "both shelves are on it")
check.that("because of another one you finished" in body,
           "with the reason a book is being recommended, which is the whole "
           "of what makes a recommendation answerable")
check.that("<p>Tags that must not reach the page raw.</p>" not in body,
           "and a description full of markup is not injected into the page")

check.that("Ask for One You Do Not" in body,
           "the request button names the book, because a reader moving by "
           "button hears the label and nothing around it")
check.that("Not One You Do Not" in body, "and so does the dismiss button")
check.that("Ask for Already On The Way" not in body,
           "a book already on the way has no button")
check.that("Already asked for" in body, "and says why instead")

check.that("reading list" in body and "playlist" not in body.lower(),
           "the list is named for what it holds: Jellyfin stores it as a "
           "playlist item, but a list of books is not a playlist to the "
           "person reading it")
check.that('<ol class="shelf"' in body,
           "the shelves are ordered lists, because the order IS the "
           "recommendation")
check.that("Discover" in body,
           "and the nav names the page, so it can be found from the other one")
check.that('name="medium" value="book"' in body,
           "the refresh control says which shelf it refreshes, because one "
           "page now carries three")

# --- the nav is not drawn where there is nothing behind it -------------------
#
# "Not served here" means no Listenarr, not an empty library: a books library
# that does not exist yet is the ordinary case for somebody setting this up
# before they own anything, and the medium stays offered on purpose so the
# controls are there when the library appears.
# Through the environment, not by patching `listenarr.configured`: the
# registry captured that function object at import, so replacing the module
# attribute changes nothing the registry ever calls.
import os  # noqa: E402

os.environ["LISTENARR_URL"] = ""
media.forget()
not_served = client.get("/discover?medium=book")
check.equal(not_served.status_code, 404,
            "the page says it has nothing to recommend, rather than "
            "rendering empty shelves")
check.that("Discover</a>" not in not_served.text, "and no nav offers it")

# The sign-in page must never draw it either, and not because of what it says:
# `discover_media()` asks the registry, which probes backends on a cache miss,
# so a signed-out visitor could otherwise make this container reach out to
# Listenarr and learn from the delay whether it is configured.
check.that("Discover</a>" not in client.get("/signin").text,
           "and a signed-out visitor is told nothing about the backends")
os.environ["LISTENARR_URL"] = "http://listenarr.invalid:4545"
media.forget()
check.equal(client.get("/discover?medium=book").status_code, 200,
            "an empty books library is still served, controls and all")

# --- dismissing offers an undo, in the one moment it is wanted ---------------
#
# The dismissed titles are not stored -- the table holds an ASIN and a time --
# so a standing restore list could only name marketplace identifiers. The
# title rides the redirect instead.
hidden = client.post("/books/dismiss",
                     data={"asin": "B0NEW", "title": "One You Do Not"})
check.equal(hidden.status_code, 303, "hiding a suggestion redirects")
check.that("undo_asin=B0NEW" in hidden.headers["location"],
           "carrying what to put back")
check.that("One%20You%20Do%20Not" in hidden.headers["location"],
           "and its title, so the undo control can be read aloud")

with_undo = client.get(
    "/discover?medium=book&undo_asin=B0NEW&undo_title=One+You+Do+Not")
check.that("Undo hiding One You Do Not" in with_undo.text,
           "and the page offers it by name")

restored = client.post("/books/restore", data={"asin": "B0NEW"})
check.equal(restored.status_code, 303, "and putting it back works")

# --- the scheduled pass, which is why the playlist stays current -------------
written.clear()
jellyfin.all_users = lambda: {"matt": "user-matt"}
jellyfin.user = lambda name=None: MATT
shelves.invalidate("user-matt")
book_store.put_shelf("user-matt", dict(SHELF))

check.equal(upkeep.once(), 1, "upkeep refreshes the account that has a shelf")
check.that(written and all(name == "Next Read" and ids == ["i1"]
                           for name, ids in written),
           "and writes its playlist without anybody having visited anything. "
           "More than one write is expected and correct: a shelf restored "
           "from disk is served stale and settled, then rebuilt behind the "
           "answer and settled again")

# Nobody else. Building a shelf for a household member who has never opened
# the feature costs a twelve-second Jellyfin listing to produce something
# nobody asked for.
jellyfin.all_users = lambda: {"matt": "user-matt", "kadija": "user-kadija"}
written.clear()
check.equal(upkeep.once(), 1,
            "an account with no shelf is left alone, not given one")

# A key Jellyfin no longer knows must be skipped, not raise: an account can be
# deleted while its shelf is still on disk.
book_store.put_shelf("user-ghost", dict(SHELF))
check.equal(upkeep.once(), 1, "a shelf for a deleted account is skipped")

harness.cleanup()
raise SystemExit(check.report())
