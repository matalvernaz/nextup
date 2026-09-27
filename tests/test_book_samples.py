"""Hearing a book before asking for it.

Audible publishes a five-minute preview of nearly every book it sells, and the
address arrives on the same product lookup a summary already pays for. Pinned
here: the address reaches the phone on the summary, behind a capability block;
a product cached before the `sample` group was asked for is fetched again
rather than answering "no sample" for the rest of its month; and the browser
pages link each book to a redirect instead of drawing a player per row.
"""
import types

import harness

harness.setup(LISTENARR_URL="", JELLYFIN_USER="")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import jellyfin, main, media, sessions, store, wants  # noqa: E402
from app.books import audible, search  # noqa: E402

check = harness.Check("book samples")
store.init()

SAMPLE = "https://samples.audible.com/bk/acx0/470414/bk_acx0_470414_sample.mp3"

# --- the product lookup ------------------------------------------------------

catalogue: dict[str, object] = {}
asked: list[tuple[str, str]] = []


class FakeResponse:
    def __init__(self, product): self._product = product
    def raise_for_status(self): return self
    def json(self): return {"product": self._product}


class FakeClient:
    """Answers by host, the way each marketplace is its own catalogue.

    A host with nothing configured answers the way a real store that does not
    sell the book does: 200 with an empty product, not a 404.
    """
    def __init__(self, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def get(self, url, params=None):
        asked.append((url, (params or {}).get("response_groups", "")))
        answer = catalogue.get(httpx.URL(url).host)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer or {"asin": url.rsplit("/", 1)[-1]})


# The module's own name, not httpx's: replacing `httpx.Client` itself would
# reach the TestClient below, which is built on it.
audible.httpx = types.SimpleNamespace(
    Client=FakeClient, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout)

catalogue["api.audible.ca"] = {"asin": "B0FQ65NC2F", "title": "Castor",
                               "sample_url": SAMPLE, "is_preview_enabled": True}
got = audible.product("B0FQ65NC2F") or {}
check.that("sample" in asked[-1][1].split(","),
           "the product lookup asks for the sample group")
check.equal(got.get("sample_url"), SAMPLE, "and keeps the address it answers with")

asked.clear()
audible.product("B0FQ65NC2F")
check.equal(asked, [], "a row stored under the current groups is served from cache")

# Stored the way every row was before this change: no record of its groups.
store.put_product("B0OLDROW01", {"asin": "B0OLDROW01", "title": "Cached Before",
                                 "_region": "ca"})
catalogue["api.audible.ca"] = {"asin": "B0OLDROW01", "title": "Cached Before",
                               "sample_url": SAMPLE}
asked.clear()
got = audible.product("B0OLDROW01") or {}
check.equal(len(asked), 1,
            "a row cached before samples were asked for is fetched again")
check.equal(got.get("sample_url"), SAMPLE, "and the fresh one carries the sample")
check.equal((store.get_product("B0OLDROW01") or {}).get("_groups"),
            audible._PRODUCT_RESPONSE_GROUPS, "and replaces the stored row")

store.put_product("B0OLDROW02", {"asin": "B0OLDROW02", "title": "Still Useful",
                                 "_region": "ca"})
catalogue["api.audible.ca"] = httpx.ConnectError("unreachable")
catalogue["api.audible.com"] = httpx.ConnectError("unreachable")
check.equal((audible.product("B0OLDROW02") or {}).get("title"), "Still Useful",
            "an unreachable catalogue falls back to the older row, not to nothing")
check.equal((store.get_product("B0OLDROW02") or {}).get("_groups"), None,
            "and that row is not stamped as current, which would hide its "
            "sample for the rest of the month")
catalogue.clear()

asked.clear()
check.equal(audible.product("../B0FQ65NC2F"), None,
            "something that is not an ASIN is not a product")
check.equal(asked, [], "and is never put into a catalogue address")

# --- the summary ---------------------------------------------------------------

search.store_backed_product = lambda asin: {"title": "Castor", "sample_url": SAMPLE}
check.equal(search.summary("B0FQ65NC2F")["sampleUrl"], SAMPLE,
            "the summary carries the sample address")
search.store_backed_product = lambda asin: {"title": "Not Out Yet"}
check.equal(search.summary("B0PLACEHLD")["sampleUrl"], None,
            "a book with no sample says so with null")
search.store_backed_product = lambda asin: {
    "title": "Odd", "sample_url": "http://samples.audible.com/odd.mp3"}
check.equal(search.summary("B0PLAINHTP")["sampleUrl"], None,
            "only an https address is passed on")

# --- the native route ------------------------------------------------------------

READER = jellyfin.User(id="reader", name="Reader")
jellyfin.user_from_token = lambda token: READER
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: ["books"] if medium == "book" else []
media.forget()

client = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
headers = {"X-Emby-Token": "reader-token"}

caps = client.get("/nextread/api/v1/capabilities", headers=headers).json()
check.equal(caps.get("sample"), {"supported": True},
            "capabilities say the summary carries a sample, with no Listenarr "
            "configured, because it needs none")

search.store_backed_product = lambda asin: {"title": "Castor", "sample_url": SAMPLE}
summary = client.get("/nextread/api/v1/summary", params={"asin": "B0FQ65NC2F"},
                     headers=headers).json()
check.equal(summary.get("sampleUrl"), SAMPLE, "the summary route carries it to the phone")

# --- the browser pages -------------------------------------------------------------

PRODUCTS = {"B0FQ65NC2F": {"title": "Castor", "sample_url": SAMPLE},
            "B0PLACEHLD": {"title": "Not Out Yet"}}
looked_up: list[str] = []
audible.product = lambda asin: looked_up.append(asin) or PRODUCTS.get(asin)

signed_out = client.get("/sample", params={"asin": "B0FQ65NC2F"})
check.equal(signed_out.status_code, 401, "signed out, no redirect is handed out")

client.cookies.set(sessions.COOKIE_NAME, sessions.issue("reader-token", READER.id))

sent = client.get("/sample", params={"asin": "B0FQ65NC2F"})
check.equal(sent.status_code, 302, "a book with a sample redirects")
check.equal(sent.headers.get("location"), SAMPLE, "to Audible's own file")
check.equal(client.get("/sample", params={"asin": "b0fq65nc2f"}).headers.get("location"),
            SAMPLE, "a lower-case ASIN is the same book")

none = client.get("/sample", params={"asin": "B0PLACEHLD"})
check.equal(none.status_code, 404, "a book with no sample is not a redirect")
check.that("Audible has no sample of Not Out Yet" in none.text,
           "and the page names the book it has no sample of")

unknown = client.get("/sample", params={"asin": "B0NOTSOLD1"})
check.equal(unknown.status_code, 404, "a book Audible did not answer for is not a redirect")
check.that("could not find that book on Audible" in unknown.text
           and "Audible has no sample" not in unknown.text,
           "and says it could not tell, rather than that there is no sample")

looked_up.clear()
odd = client.get("/sample", params={"asin": "../B0FQ65NC2F?x"})
check.equal(odd.status_code, 404, "something that is not an ASIN has no sample")
check.equal(looked_up, [], "and never reaches the catalogue")

BOOKS = media.Medium(key="book", label="Books", units=("book", "series"),
                     daily_cap=3, library_ids=("books",))
FILMS = media.Medium(key="movie", label="Films", units=("movie",),
                     daily_cap=3, library_ids=("films",))
media.available = lambda: {"book": BOOKS, "movie": FILMS}
HITS = {
    ("book", "book"): [{"medium": "book", "unit": "book", "itemKey": "B0FQ65NC2F",
                        "title": "Castor", "owned": False, "requested": False}],
    ("book", "series"): [{"medium": "book", "unit": "series", "itemKey": "Cor Heart",
                          "title": "The Cor Heart Saga", "owned": False}],
    ("movie", "movie"): [{"medium": "movie", "unit": "movie", "itemKey": "603",
                          "title": "The Matrix", "owned": False}],
}
wants.search = lambda q, medium, unit, user: HITS[(medium, unit)]

books = client.get("/", params={"q": "castor", "medium": "book", "unit": "book"}).text
check.that('href="/sample?asin=B0FQ65NC2F"' in books,
           "a book in the search results links to its sample")
check.that("Hear a sample of Castor" in books,
           "and the link names the book, as the buttons do")
check.that("/sample?" not in client.get(
    "/", params={"q": "cor", "medium": "book", "unit": "series"}).text,
           "a whole series has no one sample to link")
check.that("/sample?" not in client.get(
    "/", params={"q": "matrix", "medium": "movie"}).text,
           "and a film has none at all")

harness.cleanup()
raise SystemExit(check.report())
