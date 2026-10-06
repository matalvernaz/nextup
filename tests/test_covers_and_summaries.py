"""Covers and summaries for things the library does not hold yet.

Every search hit, book suggestion and request carries its catalogue's picture
in two sizes where the catalogue has one, and the film and series hits carry
the facts a summary shows beside the blurb. Pinned here: which address is
handed on and at what size; that a cached similarity list from before covers
were kept is fetched again but still served when Audible is down; that a
request keeps its tool's own poster and blurb in preference to what the caller
sent; and that the browser pages draw the pictures as decoration and give a
book its own summary page.
"""
import types

import harness

harness.setup(
    RADARR_URL="http://radarr.invalid", RADARR_API_KEY="k",
    RADARR_QUALITY_PROFILE_ID="4",
    SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k",
    SONARR_QUALITY_PROFILE_ID="4",
    BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
    LISTENARR_URL="", JELLYFIN_USER="",
)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (arr, artwork, buskarr, compat_nextread, jellyfin,  # noqa: E402
                 main, media, radarr, sessions, sonarr, store, wants)
from app.books import audible, search  # noqa: E402
from app.books import shelves as book_shelves  # noqa: E402
from app.books import wants as book_wants  # noqa: E402

check = harness.Check("covers and summaries")
store.init()

TMDB = "https://image.tmdb.org/t/p/original/v1tRXZ4JtD2Iv6fjkPvT4GiwslV.jpg"
TVDB = "https://artworks.thetvdb.com/banners/posters/81189-10.jpg"
TVDB_V4 = "https://artworks.thetvdb.com/banners/v4/series/303603/posters/6144441f5298f.jpg"
AMAZON = "https://m.media-amazon.com/images/I/51HIZdnqASL._SL500_.jpg"
DEEZER = ("https://cdn-images.dzcdn.net/images/cover/"
          "5718f7c81c27e0b2417e2a4c45224f8a/400x400-000000-80-0-0.jpg")
ITUNES = ("https://is1-ssl.mzstatic.com/image/thumb/Music221/v4/fd/4a/77/"
          "fd4a77db-0ebc-d043-41a2-f32fa1bb0fb4/dj.qrikkdwj.jpg/100x100bb.jpg")

# --- which address, at which size ----------------------------------------------

check.equal(artwork.art(TMDB), {
    "imageUrl": "https://image.tmdb.org/t/p/w500/v1tRXZ4JtD2Iv6fjkPvT4GiwslV.jpg",
    "thumbnailUrl": "https://image.tmdb.org/t/p/w154/v1tRXZ4JtD2Iv6fjkPvT4GiwslV.jpg",
}, "a TMDb poster is sized for a summary and for a row, never sent at 2000x3000")
check.equal(artwork.art(TVDB), {
    "imageUrl": TVDB,
    "thumbnailUrl": "https://artworks.thetvdb.com/banners/posters/81189-10_t.jpg",
}, "a TheTVDB poster keeps its full copy and names the half-size one for rows")
check.equal(artwork.art(TVDB_V4)["thumbnailUrl"],
            TVDB_V4.replace(".jpg", "_t.jpg"),
            "the same for TheTVDB's newer addresses")
check.equal(artwork.art(TVDB.replace(".jpg", "_t.jpg")), {
    "imageUrl": TVDB,
    "thumbnailUrl": "https://artworks.thetvdb.com/banners/posters/81189-10_t.jpg",
}, "and a half-size address given is not halved again")
check.equal(artwork.art(AMAZON), {
    "imageUrl": AMAZON,
    "thumbnailUrl": "https://m.media-amazon.com/images/I/51HIZdnqASL._SL160_.jpg",
}, "an Amazon cover names its size in the address, so the row's is a rewrite")
check.equal(artwork.art(DEEZER), {
    "imageUrl": DEEZER.replace("400x400", "500x500"),
    "thumbnailUrl": DEEZER.replace("400x400", "250x250"),
}, "a Deezer cover is squared at both sizes")
check.equal(artwork.art(
    "https://e-cdns-images.dzcdn.net/images/artist//500x500-000000-80-0-0.jpg"), {},
    "Deezer's grey silhouette for an artist with no picture is not a picture")
check.equal(artwork.art(ITUNES), {
    "imageUrl": ITUNES.replace("100x100bb", "600x600bb"),
    "thumbnailUrl": ITUNES.replace("100x100bb", "160x160bb"),
}, "an Apple cover is asked for at both sizes rather than at 100 pixels")
check.equal(artwork.art("https://example.org/cover.png"), {
    "imageUrl": "https://example.org/cover.png",
    "thumbnailUrl": "https://example.org/cover.png",
}, "an address from anywhere else is passed on at the one size it has")
for bad in (None, "", "http://image.tmdb.org/t/p/w500/x.jpg", "https://",
            "https:///nohost.jpg", "https://a.example/with space.jpg",
            "https://a.example/" + "x" * artwork.MAX_URL_LENGTH, 42):
    check.equal(artwork.art(bad), {}, f"no picture from {str(bad)[:40]!r}")

check.equal(artwork.arr_poster({
    "images": [
        {"coverType": "fanart", "url": "/MediaCoverProxy/fan.jpg",
         "remoteUrl": "https://image.tmdb.org/t/p/original/fan.jpg"},
        {"coverType": "poster", "url": "/MediaCoverProxy/poster.jpg",
         "remoteUrl": TMDB},
    ],
    "remotePoster": "https://image.tmdb.org/t/p/original/other.jpg",
}), TMDB, "an arr row's poster is the image it calls the poster, from the catalogue")
check.equal(artwork.arr_poster({"images": [
    {"coverType": "poster", "url": "/MediaCoverProxy/poster.jpg"}]}), None,
    "and never the arr's own cache path, which needs its API key")
check.equal(artwork.arr_poster({"images": [], "remotePoster": TVDB}), TVDB,
            "`remotePoster` stands in where the images list has none")

check.equal(artwork.audible_cover({"product_images": {
    "252": "https://m.media-amazon.com/images/I/small._SL252_.jpg", "500": AMAZON}}),
    AMAZON, "an Audible product's largest cover is the one kept")
check.equal(artwork.audible_cover({"product_images": {"500": "http://x/y.jpg"}}), None,
            "and a plain-http one is not kept at all")
check.equal(artwork.audible_cover(None), None, "no product, no cover")

# --- films, series and music ------------------------------------------------------

DUNE = {"tmdbId": 438631, "title": "Dune", "year": 2021,
        "overview": "Paul Atreides travels to Arrakis.", "runtime": 155,
        "genres": ["Science Fiction", "Adventure"], "certification": "PG-13",
        "studio": "Legendary Pictures",
        "images": [{"coverType": "poster", "url": "/MediaCoverProxy/x.jpg",
                    "remoteUrl": TMDB}]}
hit = radarr._result(DUNE, frozenset())
check.equal(hit.get("thumbnailUrl"),
            "https://image.tmdb.org/t/p/w154/v1tRXZ4JtD2Iv6fjkPvT4GiwslV.jpg",
            "a film hit carries its poster")
check.equal((hit.get("genres"), hit.get("certification"), hit.get("studio")),
            (["Science Fiction", "Adventure"], "PG-13", "Legendary Pictures"),
            "and what a summary says beside the blurb")
bare = radarr._result({"tmdbId": 1, "title": "Unlisted", "certification": " ",
                       "genres": ["", None]}, frozenset())
check.that(not {"imageUrl", "thumbnailUrl", "genres", "certification",
                "studio"} & bare.keys(),
           "a film with none of them carries none of the keys, not blanks")

HEARTLAND = {"tvdbId": 70598, "title": "Heartland", "year": 2007,
             "overview": "A family ranch in Alberta.", "network": "CBC",
             "status": "continuing", "genres": ["Drama", "Family"],
             "certification": "TV-PG",
             "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}],
             "remotePoster": TVDB}
hit = sonarr._result(HEARTLAND, frozenset())
check.equal((hit.get("imageUrl"), hit.get("genres"), hit.get("certification"),
             hit.get("status")), (TVDB, ["Drama", "Family"], "TV-PG", "continuing"),
            "a series hit carries its poster, genres, age rating and whether it has ended")

album = buskarr._result({"unit": "album", "source": "deezer", "ref": "302127",
                         "title": "Discovery", "artist": "Daft Punk",
                         "tracks": 14, "imageUrl": DEEZER}, "album")
check.equal(album.get("thumbnailUrl"), DEEZER.replace("400x400", "250x250"),
            "an album hit carries the cover buskarr found it with")
check.equal(album.get("itemKey"), "bk:album:deezer:302127",
            "and is otherwise the hit it always was")
old_buskarr = buskarr._result({"unit": "track", "artist": "Daft Punk",
                               "title": "One More Time", "duration": 320.0,
                               "sources": ["deezer"]}, "track")
check.that("imageUrl" not in old_buskarr,
           "a buskarr that sends no picture gives a row with none")

# --- books -------------------------------------------------------------------------

search.listenarr.audible_search = lambda query, limit=None: [{
    "asin": "B0FQ65NC2F", "title": "Ironbound", "authors": [{"name": "A. Author"}],
    "narrators": [], "lengthMinutes": 600, "imageUrl": AMAZON}]
search.shelves.owned_index = lambda user: (set(), {})
# `search.wants` is the book ledger's own module, whose `states` is tested
# further down, so it is put back straight after.
real_book_states = search.wants.states
search.wants.states = lambda user_key, owned: []
READER = jellyfin.User(id="reader", name="Reader")
found = search.search(READER, "ironbound")
search.wants.states = real_book_states
check.equal(found[0].get("thumbnailUrl"),
            "https://m.media-amazon.com/images/I/51HIZdnqASL._SL160_.jpg",
            "a book hit carries the cover Listenarr's search returned")

original_product = search.store_backed_product
search.store_backed_product = lambda asin: {
    "title": "Ironbound", "product_images": {"500": AMAZON}}
check.equal(search.summary("B0FQ65NC2F").get("imageUrl"), AMAZON,
            "a book's summary carries its cover off the same product lookup")
search.store_backed_product = lambda asin: {"title": "No Cover"}
check.that("imageUrl" not in search.summary("B0NOCOVER1"),
           "and a product with no cover gives a summary with none")
search.store_backed_product = original_product

# The similarity lookup keeps covers, and a list cached before it did is
# fetched again rather than dropped -- but only replaced when Audible answers.
asked: list[str] = []
answers: dict[str, object] = {}


class FakeResponse:
    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): return self
    def json(self): return self._payload


class FakeClient:
    def __init__(self, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def get(self, url, params=None):
        asked.append(url)
        answer = answers.get(httpx.URL(url).host)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer if answer is not None else {"similar_products": []})


audible.httpx = types.SimpleNamespace(
    Client=FakeClient, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout)

NEIGHBOUR = {"asin": "B0NEIGHBR1", "title": "A Neighbour",
             "product_images": {"500": AMAZON}}
check.equal(audible._thin(NEIGHBOUR).get("image"), AMAZON,
            "a neighbour keeps its cover")
check.that("image" in audible._thin({"asin": "B0NOIMAGE1", "title": "Bare"}),
           "and says it has none rather than leaving the key out")

store.put_sims("B0SEEDBOOK", audible.AXIS_RAW,
               [{"asin": "B0NEIGHBR1", "title": "A Neighbour"}])
answers["api.audible.ca"] = {"similar_products": [NEIGHBOUR]}
asked.clear()
neighbours = audible.sims("B0SEEDBOOK")
check.equal(len(asked), 1, "a list cached before covers were kept is fetched again")
check.equal(neighbours[0].get("image"), AMAZON, "and the new one has the cover")
check.equal(store.get_sims("B0SEEDBOOK", audible.AXIS_RAW)[0].get("image"), AMAZON,
            "and replaces the stored list")
asked.clear()
audible.sims("B0SEEDBOOK")
check.equal(asked, [], "a list with covers is served from the cache")

store.put_sims("B0SEEDTWO1", audible.AXIS_RAW,
               [{"asin": "B0OLDNEIGH", "title": "Still Right"}])
answers["api.audible.ca"] = httpx.ConnectError("down")
answers["api.audible.com"] = httpx.ConnectError("down")
check.equal([row["asin"] for row in audible.sims("B0SEEDTWO1")], ["B0OLDNEIGH"],
            "with Audible down, the older list is still served, not nothing")
check.equal(store.get_sims("B0SEEDTWO1", audible.AXIS_RAW),
            [{"asin": "B0OLDNEIGH", "title": "Still Right"}],
            "and left as it was, to be fetched again later")
store.put_sims("B0SEEDNONE", audible.AXIS_RAW, [])
asked.clear()
check.equal(audible.sims("B0SEEDNONE"), [], "an empty list stays empty")
check.equal(asked, [], "and is not fetched again: it has no rows to lack a cover")
answers.clear()

suggestion = compat_nextread._suggestion({"asin": "B0NEIGHBR1", "title": "A Neighbour",
                                          "image": AMAZON})
check.equal(suggestion.get("thumbnailUrl"),
            "https://m.media-amazon.com/images/I/51HIZdnqASL._SL160_.jpg",
            "a suggestion on the shelf carries its cover")
check.that("imageUrl" not in compat_nextread._suggestion({"asin": "B0OLD00001"}),
           "and one from a shelf stored before covers has none")

store.put_product("B0ASKEDFOR", {"asin": "B0ASKEDFOR", "title": "Asked For",
                                 "product_images": {"500": AMAZON}})
book_store_rows = [{"asin": "B0ASKEDFOR", "title": "Asked For", "requested_at": 1.0,
                    "fulfilled_at": None},
                   {"asin": "B0UNCACHED", "title": "Not Cached", "requested_at": 2.0,
                    "fulfilled_at": None}]
book_wants.store.requests_for = lambda user_key: [dict(r) for r in book_store_rows]
rows = {row["asin"]: row for row in book_wants.states("reader", None)}
check.equal(rows["B0ASKEDFOR"].get("imageUrl"), AMAZON,
            "a requested book shows the cover its product lookup cached")
check.that("imageUrl" not in rows["B0UNCACHED"],
           "and one not in the cache has none, rather than an Audible request per row")

# --- what a request keeps ----------------------------------------------------------

media._registry = {
    media.MOVIE: media.Medium(media.MOVIE, "Films", ("movie",), 3, ("lib-movies",)),
    media.SERIES: media.Medium(media.SERIES, "Series", ("series",), 1, ("lib-tv",)),
    media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("lib-music",)),
}
media._owned._value = jellyfin.Owned()
import time  # noqa: E402
media._owned._built_at = time.monotonic()
media.episode_counts = lambda provider_ids: {}
sonarr.acquisition_progress = lambda backend_ids: {}
buskarr.state = lambda ref: None

radarr.add = lambda tmdb, title="", year="", monitored=True: arr.AddResult(
    True, "Sent to Radarr.", "r1", "Dune", "2021", image_url=TMDB,
    overview="Radarr's own words.")
buskarr.add = lambda unit, hit, by, bulk=False: arr.AddResult(True, "Queued.", "job:1",
                                                  hit.get("title", ""))
MATT = jellyfin.User(id="u-matt", name="matt", is_admin=True)

wants.want(MATT, media.MOVIE, "tmdb:438631", "movie",
           {"title": "Dune", "imageUrl": "https://example.org/sent.jpg",
            "overview": "What the phone sent."})
wants.want(MATT, media.MUSIC, "bk:album:deezer:302127", "album",
           {"title": "Discovery", "artist": "Daft Punk", "source": "deezer",
            "ref": "302127", "imageUrl": DEEZER,
            "overview": "French electronic duo"})
wants.want(MATT, media.MUSIC, "bk:album:deezer:9", "album",
           {"title": "Plain", "source": "deezer", "ref": "9",
            "imageUrl": "http://insecure.example/x.jpg"})
listed = {row["itemKey"]: row for row in wants.states(MATT)}
film = listed["tmdb:438631"]
check.equal(film.get("thumbnailUrl"),
            "https://image.tmdb.org/t/p/w154/v1tRXZ4JtD2Iv6fjkPvT4GiwslV.jpg",
            "a film on the request list shows Radarr's poster, not the one sent")
check.equal(film.get("overview"), "Radarr's own words.",
            "and Radarr's blurb, for the same reason")
album_row = listed["bk:album:deezer:302127"]
check.equal((album_row.get("imageUrl"), album_row.get("overview")),
            (DEEZER.replace("400x400", "500x500"), "French electronic duo"),
            "music keeps what its search hit carried, having nothing else")
check.that("imageUrl" not in listed["bk:album:deezer:9"],
           "and a plain-http picture is not kept")

with store.db() as conn:
    conn.execute(
        "INSERT INTO requests (user_key, medium, item_key, unit, title, year, "
        "cost, backend_id, requested_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (MATT.key, media.SERIES, "tvdb:70598", "series", "Heartland", "2007",
         1, "s9", time.time()))
old = {row["itemKey"]: row for row in wants.states(MATT)}["tvdb:70598"]
check.that("imageUrl" not in old and "overview" not in old,
           "a request written before pictures were kept reads with neither")

# The native route takes the picture and blurb too, and a client that sends
# null for either -- or nothing -- is asking exactly as it did before.
jellyfin.user_from_token = lambda token: MATT
api_client = TestClient(main.app, raise_server_exceptions=False)
sent = api_client.post("/api/v1/want", headers={"X-Emby-Token": "matt-token"}, json={
    "medium": "music", "itemKey": "bk:album:deezer:77", "unit": "album",
    "title": "Homework", "source": "deezer", "ref": "77",
    "imageUrl": DEEZER, "overview": None})
check.equal(sent.status_code, 200, "a want with a null blurb is taken, not refused")
unsent = api_client.post("/api/v1/want", headers={"X-Emby-Token": "matt-token"}, json={
    "medium": "music", "itemKey": "bk:album:deezer:78", "unit": "album",
    "title": "Alive", "source": "deezer", "ref": "78"})
check.equal(unsent.status_code, 200, "and one that sends neither, as an older client does")
listed = {row["itemKey"]: row for row in api_client.get(
    "/api/v1/requests", headers={"X-Emby-Token": "matt-token"}).json()["requests"]}
check.equal(listed["bk:album:deezer:77"].get("thumbnailUrl"),
            DEEZER.replace("400x400", "250x250"),
            "the request list the phone reads carries the picture it sent")
check.that("imageUrl" not in listed["bk:album:deezer:78"],
           "and none where none was sent")

# --- the browser pages ---------------------------------------------------------------

jellyfin.user_from_token = lambda token: READER
jellyfin.credential_rejected = lambda force=True: False
jellyfin.library_ids = lambda medium: []
client = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)

signed_out = client.get("/summary", params={"asin": "B0FQ65NC2F"})
check.equal(signed_out.status_code, 401, "signed out, a summary page is not served")
client.cookies.set(sessions.COOKIE_NAME, sessions.issue("reader-token", READER.id))

FILMS = media.Medium(key="movie", label="Films", units=("movie",),
                     daily_cap=3, library_ids=("films",))
BOOKS = media.Medium(key="book", label="Books", units=("book", "series"),
                     daily_cap=3, library_ids=("books",))
media.available = lambda: {"movie": FILMS, "book": BOOKS}
HITS = {
    "movie": [radarr._result(DUNE, frozenset()),
              {"medium": "movie", "unit": "movie", "itemKey": "tmdb:2",
               "title": "No Poster", "owned": False}],
    "book": [{"medium": "book", "unit": "book", "itemKey": "B0FQ65NC2F",
              "title": "Ironbound", "owned": False, "requested": False,
              **artwork.art(AMAZON)}],
}
wants.search = lambda q, medium, unit, user: HITS[medium]
wants.states = lambda user, medium=None, **_: []

page = client.get("/", params={"q": "dune", "medium": "movie"}).text
check.that('src="https://image.tmdb.org/t/p/w154/v1tRXZ4JtD2Iv6fjkPvT4GiwslV.jpg"'
           in page, "a search result draws its poster at row size")
check.equal(page.count('class="cover"'), 1,
            "and a result with no poster draws no empty picture")
check.that('alt=""' in page and 'referrerpolicy="no-referrer"' in page,
           "the picture is decoration, and the catalogue is not told who asked")
check.that("Science Fiction, Adventure." in page and "Rated PG-13." in page,
           "a film's genres and age rating are on the page with its blurb")
check.that('name="imageUrl" value="https://image.tmdb.org/t/p/w500/' in page,
           "and asking for it sends the picture along for the request list")

books_page = client.get("/", params={"q": "ironbound", "medium": "book"}).text
check.that('href="/summary?asin=B0FQ65NC2F"' in books_page
           and "Read the summary of Ironbound" in books_page,
           "a book result links to its own summary, named for the book")

search.store_backed_product = lambda asin: {
    "title": "Ironbound", "authors": [{"name": "A. Author"}],
    "runtime_length_min": 605, "product_images": {"500": AMAZON},
    "sample_url": "https://samples.audible.com/bk/x_sample.mp3",
    "publisher_summary": "<p>First part.</p><p>Second part.</p>"}
summary_page = client.get("/summary", params={"asin": "b0fq65nc2f"})
check.equal(summary_page.status_code, 200, "a book Audible answers for has a page")
text = summary_page.text
check.that("<h2>Ironbound</h2>" in text, "headed with the book's title")
check.that(f'class="summary-cover" src="{AMAZON}"' in text,
           "with its cover at the larger size")
check.that("<p>First part.</p>" in text and "<p>Second part.</p>" in text,
           "and its blurb as paragraphs, not one run-on line of markup")
check.that("10 hours 5 minutes." in text, "and how long it is, in words")
check.that(text.index("Hear a sample of Ironbound") < text.index("First part."),
           "the sample link comes before a blurb that can run to pages")

search.store_backed_product = lambda asin: {}
missing = client.get("/summary", params={"asin": "B0NOTSOLD1"})
check.equal(missing.status_code, 404, "a book Audible did not answer for is not found")
check.that("could not find that book on Audible" in missing.text,
           "and the page says it could not tell, rather than that there is no blurb")
looked: list[str] = []
search.store_backed_product = lambda asin: looked.append(asin) or {}
check.equal(client.get("/summary", params={"asin": "../etc?x"}).status_code, 404,
            "something that is not an ASIN has no summary")
check.equal(looked, [], "and never reaches the catalogue")

harness.cleanup()
raise SystemExit(check.report())
