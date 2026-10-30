"""The books route says a waiting book goes through by itself, as the other lists do.

Found by the EchoFin side of request notices (2026-10-30): the films, shows and
music list said `releasesOnItsOwn` on a waiting ask, but the books screen's
route built its held rows from a list of keys of its own, so a waiting book
never said it. Both now take their held rows from one place.
"""
import harness

harness.setup(BOOK_DAILY_CAP="1", APPROVALS="on", JELLYFIN_USER="",
              LISTENARR_URL="http://listenarr.invalid", LISTENARR_API_KEY="k")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import compat_nextread, held, jellyfin, listenarr, media, store  # noqa: E402
from app.books import shelves  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

check = harness.Check("books list says a waiting book goes through")
store.init()

READER = jellyfin.User(id="u-reader", name="Reader", is_admin=False)
STRICT = jellyfin.User(id="u-strict", name="Strict", is_admin=False)
TOKENS = {"reader-token": READER, "strict-token": STRICT}
jellyfin.user_from_token = lambda token: TOKENS[token]
jellyfin.credential_rejected = lambda force=True: False
jellyfin.accounts = lambda: list(TOKENS.values())
BOOKS = {media.BOOK: media.Medium(media.BOOK, "Books", ("book", "series"), 1, ("lib-books",))}
media.available = lambda: dict(BOOKS)
media.get = lambda medium: BOOKS.get(medium)
held._post = lambda url, body, headers: None
shelves.owned_index = lambda user: (set(), {})
shelves.forget_asin = lambda asin, user_key=None: None
shelves.result = lambda user, force=False, update_playlist=False: {
    "own": [], "discover": [], "run_id": None, "ranker_version": None}
shelves.unowned_suggestions = lambda data, owned: data
listenarr.add = lambda asin, monitored=True, metadata=None: listenarr.AddResult(
    True, "Sent to Listenarr", 41, title=f"Book {asin}", authors=("An Author",))
listenarr.enqueue_search = lambda audiobook_id: True

app = FastAPI()
app.include_router(compat_nextread.router)
client = TestClient(app, raise_server_exceptions=False)


def waiting_rows(token):
    shelf = client.get("/nextread/api/v1/shelves?held=true", headers={"X-Emby-Token": token})
    check.equal(shelf.status_code, 200, "the books route answers")
    return {row["asin"]: row for row in shelf.json()["requests"]
            if row["state"] == held.WAITING_FOR_APPROVAL}


book_wants.want(READER, "B01", "First Book", over_limit=True)
book_wants.want(READER, "B02", "Second Book", over_limit=True)
rows = waiting_rows("reader-token")
check.equal(list(rows), ["B02"], "the book past the day's limit waits")
check.equal(rows["B02"].get("releasesOnItsOwn"), True,
            "and the books route says it goes through by itself, as the other lists do")

store.set_cap(STRICT.key, media.BOOK, 0)
book_wants.want(STRICT, "B03", "Third Book", over_limit=True)
rows = waiting_rows("strict-token")
check.equal(rows["B03"].get("releasesOnItsOwn"), False,
            "a limit of 0 says it waits for a keyholder whatever happens")

harness.cleanup()
raise SystemExit(check.report())
