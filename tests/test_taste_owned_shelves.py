"""Film, show and podcast shelves follow what an account said about its suggestions."""
import threading

import harness

harness.setup()

from app import config, jellyfin, recommendations, store, taste, tmdb  # noqa: E402

check = harness.Check("marks on owned shelves")
store.init()
config.SERIES_RECOMMENDATION_LIMIT = 10
config.MOVIE_RECOMMENDATION_LIMIT = 10
config.PODCAST_RECOMMENDATION_LIMIT = 10
USER = jellyfin.User("viewer-id", "Viewer")


def item(item_id, title, *, progress=0, genres=(), people=(), studios=(),
         provider=None, kind="Series"):
    return {
        "Id": item_id,
        "Name": title,
        "Type": kind,
        "Genres": list(genres),
        "People": [{"Name": name, "Type": "Actor"} for name in people],
        "Studios": [{"Name": name} for name in studios],
        "DateCreated": "2026-01-01T00:00:00Z",
        "CommunityRating": 7.0,
        "ProviderIds": dict(provider or {}),
        "UserData": {"Played": progress >= 100, "PlayedPercentage": progress},
    }


def owned_mark(key, opinion="", had=False, medium="series", revision=1):
    return taste.Mark(medium, key, taste.OWNED, opinion, had, revision=revision)


def ids(built):
    return [row["id"] for row in built["recommendations"]]


def rows(built):
    return {row["id"]: row for row in built["recommendations"]}


watched = item("watched", "Watched", progress=100, genres=("Comedy",),
               people=("Pat Lead",))
comedy = item("comedy", "Comedy Match", genres=("Comedy",), people=("Pat Lead",))
heist = item("heist", "Heist Pick", genres=("Heist",), people=("Robin Crook",))
heist_two = item("heist-two", "Heist Two", genres=("Heist",), people=("Robin Crook",))
western = item("western", "Western", genres=("Western",))
library = [watched, comedy, heist, heist_two, western]


# --- no marks: exactly the shelf there was -------------------------------------
check.equal(recommendations.build(library, "series", marks=[]),
            recommendations.build(library, "series"),
            "an account with no marks gets the shelf it always got")
plain = recommendations.build(library, "series")
check.that("heist" not in ids(plain), "unrelated shows are not offered without a mark")


# --- had and less keep a show off the shelf ----------------------------------------
check.that("comedy" in ids(plain), "the comedy match is offered to begin with")
had = recommendations.build(library, "series", [owned_mark("comedy", had=True)])
check.that("comedy" not in ids(had), "a show already watched is never offered again")
less = recommendations.build(library, "series", [owned_mark("comedy", taste.LESS)])
check.that("comedy" not in ids(less), "less like this takes the show itself away")
check.equal(less["seed_count"], plain["seed_count"] + 1,
            "and makes it a seed that counts against its own kind")

# Pull the same way a low rating does: a second comedy ranks lower.
second = item("comedy-two", "Second Comedy", genres=("Comedy",))
with_second = library + [second]
before = rows(recommendations.build(with_second, "series"))["comedy-two"]["score"]
after_rows = rows(recommendations.build(
    with_second, "series", [owned_mark("comedy", taste.LESS)]))
check.that("comedy-two" not in after_rows or after_rows["comedy-two"]["score"] < before,
           "less like this pushes the same kind of show down")


# --- more like this --------------------------------------------------------------
more = recommendations.build(library, "series", [owned_mark("heist", taste.MORE)])
more_rows = rows(more)
check.that("heist-two" in more_rows,
           "more like this brings in a show like it that had nothing else going for it")
check.that("heist" in more_rows, "the show asked about stays on the shelf")
reasons = " ".join(more_rows["heist-two"]["reason"])
check.that("asked for more of" in reasons,
           "a reason resting only on more like this says so")
check.that("you've seen" not in reasons and "you've watched" not in reasons,
           "and never claims a show nobody has seen was watched")
check.equal(more_rows["heist"]["reason"], ["you asked for more like this"],
            "the asked-about show is not explained by its own evidence")
check.equal(more_rows["heist"]["score"], 0.1,
            "nor scored by it: only its quality tiebreak is left")
plain_reasons = rows(plain)["comedy"]["reason"]
check.equal(rows(recommendations.build(
    library, "series", [owned_mark("heist", taste.MORE)]))["comedy"]["reason"],
    plain_reasons, "evidence from what was watched keeps its own wording")


# --- TMDb names a watched voter, never one only asked about -------------------------
real_configured, real_neighbours = tmdb.configured, tmdb.recommendations
tmdb.configured = lambda: True
voting = {"10": ["30"], "20": ["30"]}
tmdb.recommendations = lambda seed_id, medium: voting.get(seed_id, [])
try:
    seen_film = item("seen", "Seen Film", progress=100, provider={"Tmdb": "10"},
                     kind="Movie")
    asked_film = item("asked", "Asked Film", provider={"Tmdb": "20"}, kind="Movie")
    target = item("target", "Target Film", provider={"Tmdb": "30"}, kind="Movie")
    films = [seen_film, asked_film, target]
    both = rows(recommendations.build(
        films, "movie", [owned_mark("asked", taste.MORE, medium="movie")]))
    check.equal(both["target"]["reason"][0],
                "watched by people who liked Seen Film, a film you've seen",
                "with a watched voter and an asked one, the watched one is named")
    voting = {"20": ["30"]}
    only_asked = rows(recommendations.build(
        films, "movie", [owned_mark("asked", taste.MORE, medium="movie")]))
    check.equal(only_asked["target"]["reason"][0],
                "watched by people who liked Asked Film, which you asked for more of",
                "with only an asked voter, it is not called a film you've seen")
finally:
    tmdb.configured, tmdb.recommendations = real_configured, real_neighbours


# Two films only asked about, each TMDb's neighbour of the other, and nothing
# watched: neither is evidence for the other.
real_configured, real_neighbours = tmdb.configured, tmdb.recommendations
tmdb.configured = lambda: True
tmdb.recommendations = lambda seed_id, medium: {"41": ["42"], "42": ["41"]}.get(seed_id, [])
try:
    first_asked = item("one", "First Asked", provider={"Tmdb": "41"}, kind="Movie")
    second_asked = item("two", "Second Asked", provider={"Tmdb": "42"}, kind="Movie")
    pair = rows(recommendations.build(
        [first_asked, second_asked], "movie",
        [owned_mark("one", taste.MORE, medium="movie", revision=1),
         owned_mark("two", taste.MORE, medium="movie", revision=2)]))
    check.equal(pair["two"]["reason"], ["you asked for more like this"],
                "a film only asked about is not a reason for another one asked about")
    check.equal(pair["two"]["source"], "asked", "nor counted as its neighbour")
finally:
    tmdb.configured, tmdb.recommendations = real_configured, real_neighbours

# A full shelf still keeps what was asked for more of: its row is where its
# own "Undo more like this" is.
config.SERIES_RECOMMENDATION_LIMIT = 1
crowded = recommendations.build(library, "series", [owned_mark("western", taste.MORE)])
check.that("western" in ids(crowded) and len(ids(crowded)) == 2,
           "a show asked for more of stays on a full list, past the cut")
config.SERIES_RECOMMENDATION_LIMIT = 10


# --- podcasts: a catalogue mark follows the show into the library --------------------
FEED = "https://example.invalid/feed.xml"
from app import itunes  # noqa: E402

feed_key = itunes.item_key(FEED)
added = item("added", "Added Show", genres=("True Crime",), kind="Podcast",
             provider={"PodcastFeed": FEED})
listened = item("listened", "Listened", progress=100, genres=("True Crime",),
                kind="Podcast")
podcast_library = [listened, added]
offered = recommendations.build(podcast_library, "podcast")
check.that("added" in ids(offered), "the added show is offered to begin with")
catalogue_had = taste.Mark("podcast", feed_key, taste.CATALOGUE, "", True,
                           title="Added Show", revision=1)
check.that("added" not in ids(recommendations.build(
    podcast_library, "podcast", [catalogue_had])),
    "already listened, said on the catalogue shelf, still holds once it is added")

# A podcast marked on the catalogue shelf that the library does not hold.
history = item("history-show", "History Show", genres=("History",), kind="Podcast")
unheld_more = taste.Mark("podcast", "feed:elsewhere", taste.CATALOGUE, taste.MORE,
                         False, title="Elsewhere",
                         details={"genres": ["History"], "artist": "Studio"},
                         revision=2)
pulled = rows(recommendations.build(
    [listened, history], "podcast", [unheld_more]))
check.that("history-show" in pulled,
           "more like this on a show the library lacks still pulls its genre")
check.that("asked for more of" in " ".join(pulled["history-show"]["reason"]),
           "and says that is why")


# More like this, said on the catalogue list, then taken back from the row the
# show has in the library: the newest thing said wins, so it is taken back.
LISTENER = jellyfin.User("listener-id", "Listener")
taste.record(LISTENER.key, "podcast", feed_key, taste.CATALOGUE, taste.MORE, False,
             title="Added Show")
added_shelf = recommendations.build(podcast_library, "podcast",
                                    taste.marks(LISTENER.key, "podcast"))
check.equal(rows(recommendations.present(LISTENER, "podcast", added_shelf))
            ["added"].get("opinion"), "more",
            "the library's row says more like this, said of it before it was added")
taste.record(LISTENER.key, "podcast", "added", taste.OWNED, "", False)
check.equal(rows(recommendations.present(LISTENER, "podcast", added_shelf))
            ["added"].get("opinion"), None,
            "and Undo more like this on that row takes it back")
retaken = recommendations.build(podcast_library, "podcast",
                                taste.marks(LISTENER.key, "podcast"))
check.equal(retaken, offered, "leaving the shelf as if it had never been said")


# --- every read is filtered against the marks as they stand -------------------------
calls: list[str] = []
jellyfin.library_ids = lambda medium: ["lib"]


def listing(medium, uid, libraries):
    calls.append(medium)
    return library


jellyfin.recommendation_items_for_user = listing
# Rebuilds run in place here, so what they did can be looked at straight away.
taste._start = lambda target, name: target()
recommendations.forget()
first = recommendations.result(USER, medium="series")
check.that("comedy" in ids(first), "the shelf is built before any mark")
check.equal(len(calls), 1, "built once")
taste.record(USER.key, "series", "comedy", taste.OWNED, "", True)
taste.record(USER.key, "series", "heist", taste.OWNED, taste.MORE, False)
cached = recommendations.result(USER, medium="series")
check.that("comedy" not in ids(cached),
           "a shelf built before already watched never offers it again")
check.equal(len(calls), 2, "and the mark outdated it, so a rebuild ran behind")
check.equal(rows(recommendations.result(USER, medium="series"))
            .get("heist", {}).get("opinion"), "more",
            "a show marked more like this says so")
recommendations.result(USER, medium="series")
check.equal(len(calls), 2, "a shelf built from the newest marks is not rebuilt again")


# --- a rebuild asked for during a rebuild always happens -----------------------------
rebuilds = taste.BehindRebuilds("test")
taste._start = lambda target, name: threading.Thread(target=target, daemon=True).start()
gate = threading.Event()
entered = threading.Event()
runs: list[int] = []


def slow_build():
    runs.append(len(runs))
    entered.set()
    gate.wait(5)


rebuilds.request("key", slow_build)
entered.wait(5)
rebuilds.request("key", slow_build)
rebuilds.request("key", slow_build)
gate.set()
for _ in range(500):
    if not rebuilds.running("key"):
        break
    threading.Event().wait(0.01)
check.equal(len(runs), 2,
            "requests during a build give exactly one more build, never none")

harness.cleanup()
raise SystemExit(check.report())
