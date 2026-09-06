"""Owned recommendations stay personal, explainable, and user-scoped."""
import harness

harness.setup()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, config, jellyfin, media, recommendations  # noqa: E402

check = harness.Check("recommendations")
config.SERIES_RECOMMENDATION_LIMIT = 10
config.MOVIE_RECOMMENDATION_LIMIT = 10


def show(item_id, title, *, progress=0, rating=None, favorite=False,
         genres=(), people=(), studios=(), created="2026-01-01T00:00:00Z",
         community=7.0):
    user_data = {
        "Played": progress >= 100,
        "PlayedPercentage": progress,
        "IsFavorite": favorite,
    }
    if rating is not None:
        user_data["Rating"] = rating
    return {
        "Id": item_id,
        "Name": title,
        "Genres": list(genres),
        "People": [{"Name": name, "Type": role} for name, role in people],
        "Studios": [{"Name": name} for name in studios],
        "DateCreated": created,
        "CommunityRating": community,
        "UserData": user_data,
    }


# --- ranking is based on this user's evidence -------------------------------
seed = show(
    "seed", "Watched Show", progress=60,
    genres=("Comedy", "Mystery"),
    people=(("Alex Example", "Actor"),),
    studios=("North Studio",))
strong = show(
    "strong", "Strong Match", genres=("Comedy", "Mystery"),
    people=(("Alex Example", "Actor"),), studios=("North Studio",))
genre = show("genre", "Genre Match", genres=("Comedy",))
unrelated = show("unrelated", "No Match", genres=("Western",))
started = show("started", "Already Started", progress=5,
               genres=("Comedy", "Mystery"))
disliked = show("disliked", "Explicitly Disliked", rating=2,
                genres=("Horror",))
positive_only = show("positive-only", "Positive Only", genres=("Mystery",))
mixed = show("mixed", "Mixed Evidence", genres=("Mystery", "Horror"))

built = recommendations.build(
    [seed, strong, genre, unrelated, started, disliked, positive_only, mixed])
ids = [row["id"] for row in built["recommendations"]]
check.equal(built["seed_count"], 3,
            "watched and explicitly disliked shows become signed taste seeds")
check.equal(ids[0], "strong", "the candidate with the strongest evidence ranks first")
check.that("genre" in ids, "genre affinity can support a recommendation")
check.that("unrelated" not in ids, "an unsupported show is not filler")
check.that("started" not in ids, "a started show stays on Jellyfin Next Up")
check.that("disliked" not in ids, "an explicit low rating is never recommended")
scores = {row["id"]: row["score"] for row in built["recommendations"]}
check.that(scores["positive-only"] > scores["mixed"],
           "overlap with a low rating actively lowers a related candidate")
check.that(any("Alex Example" in reason
               for reason in built["recommendations"][0]["reason"]),
           "the strongest row explains its cast evidence")
check.that(any("Comedy" in reason
               for reason in built["recommendations"][0]["reason"]),
           "the strongest row explains its genre evidence")


# --- no history makes a neutral, deterministic recent shelf ----------------
cold = recommendations.build([
    show("older", "Older", created="2025-01-01T00:00:00Z"),
    show("newer", "Newer", created="2026-01-01T00:00:00Z"),
])
check.equal(cold["seed_count"], 0, "a cold profile says it has no seeds")
check.equal([row["id"] for row in cold["recommendations"]],
            ["newer", "older"], "cold-start rows are newest first")
check.that(all(row["source"] == "recent" for row in cold["recommendations"]),
           "cold-start rows make no taste claim")

negative_only = recommendations.build([
    show("disliked-drama", "Disliked Drama", rating=1, genres=("Drama",)),
    show("new-drama", "New Drama", genres=("Drama",),
         created="2026-02-01T00:00:00Z"),
    show("older-comedy", "Older Comedy", genres=("Comedy",),
         created="2026-01-01T00:00:00Z"),
], medium="movie")
check.equal(negative_only["recommendations"][0]["id"], "older-comedy",
            "a low rating changes cold-start order even without positive history")


# --- films use the same proven signals but keep film-specific attribution ---
movie = recommendations.build([
    show("watched-film", "Watched Film", progress=100, genres=("Drama",)),
    show("film-match", "Film Match", genres=("Drama",)),
], medium="movie")
check.equal(movie["ranker_version"], recommendations.MOVIE_RANKER_VERSION,
            "films identify their own ranker version")
check.that("films you've watched" in movie["recommendations"][0]["reason"][0],
           "film explanations do not call the evidence a show")


# --- the Jellyfin read is explicitly user-scoped ---------------------------
calls = []


class FakeResponse:
    def raise_for_status(self):
        return self

    def json(self):
        return {"Items": [{"Id": "same-series", "Name": "One"}]}


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, path, params):
        calls.append((path, params))
        return FakeResponse()


real_client = jellyfin._client
jellyfin._client = lambda: FakeClient()
try:
    rows = jellyfin.recommendation_items_for_user(
        "series", "user-one", ("library-a", "library-b"))
    movie_rows = jellyfin.recommendation_items_for_user(
        "movie", "user-one", ("movie-library",))
finally:
    jellyfin._client = real_client
check.equal(len(calls), 3, "each selected library is read once")
check.that(all(params["userId"] == "user-one" for _, params in calls),
           "every library request carries the authenticated user id")
check.equal(len(rows), 1, "a series present through two views is deduplicated")
check.equal(len(movie_rows), 1, "the same user-scoped reader covers films")
check.equal(calls[-1][1]["includeItemTypes"], "Movie",
            "a film shelf asks Jellyfin for films rather than series")


# --- cache keys include both account and library ----------------------------
library_reads = []
recommendations.jellyfin.library_ids = lambda _medium: ["library-a", "library-b"]
recommendations.jellyfin.recommendation_items_for_user = (
    lambda medium, uid, libraries:
        library_reads.append((medium, uid, tuple(libraries))) or [])
recommendations.forget()
user_one = jellyfin.User(id="user-one", name="One")
user_two = jellyfin.User(id="user-two", name="Two")
recommendations.result(user_one, "LIBRARY-A")
recommendations.result(user_one, "library-a")
recommendations.result(user_two, "library-a")
recommendations.result(user_one, "library-a", medium="movie")
check.equal(library_reads,
            [("series", "user-one", ("library-a",)),
             ("series", "user-two", ("library-a",)),
             ("movie", "user-one", ("library-a",))],
            "cache entries do not cross accounts or media")
check.raises(recommendations.UnknownLibrary,
             lambda: recommendations.result(user_one, "other-library"),
             "a non-TV library is refused")


# --- the API advertises and serves the additive capability -----------------
api._tokens.clear()
api.jellyfin.user_from_token = lambda _token: user_one
api.media.available = lambda: {}
api.recommendations.library_ids = lambda medium: (
    ("tv-library",) if medium == "series" else ("movie-library",))
asked = []
api.recommendations.result = lambda user, library_id, force=False, *, medium: (
    asked.append((user.id, medium, library_id, force)) or {
        "ranker_version": recommendations.ranker_version(medium),
        "seed_count": 1,
        "recommendations": [{
            "id": "strong", "title": "Strong Match", "score": 4.0,
            "reason": ["shares the Comedy genre with shows you've watched"],
            "source": "genre",
        }],
    })

test_app = FastAPI()
test_app.include_router(api.router)
client = TestClient(test_app, raise_server_exceptions=False)
headers = {"X-Emby-Token": "real-user-token"}

caps = client.get("/api/v1/capabilities", headers=headers).json()
blocks = caps["recommendations"]["media"]
check.equal(blocks[0]["medium"], "series",
            "TV recommendations are advertised without Sonarr request support")
check.equal(blocks[1]["medium"], "movie",
            "film recommendations are advertised without Radarr request support")
check.equal(blocks[1]["libraryIds"], ["movie-library"],
            "each capability names the library its shelf covers")

response = client.get(
    "/api/v1/recommendations?medium=series&libraryId=tv-library",
    headers=headers)
check.equal(response.status_code, 200, "the owned-TV shelf answers")
check.equal(response.json()["recommendations"][0]["id"], "strong",
            "the API preserves the Jellyfin item id")
movie_response = client.get(
    "/api/v1/recommendations?medium=movie&libraryId=movie-library&refresh=true",
    headers=headers)
check.equal(movie_response.status_code, 200, "the owned-film shelf answers")
check.equal(asked,
            [("user-one", "series", "tv-library", False),
             ("user-one", "movie", "movie-library", True)],
            "the endpoint passes account, medium, library, and refresh intent")

missing_auth = client.get(
    "/api/v1/recommendations?medium=series&libraryId=library-a")
check.equal(missing_auth.status_code, 401,
            "the recommendation shelf cannot use the browser identity fallback")
unsupported = client.get(
    "/api/v1/recommendations?medium=music&libraryId=library-a",
    headers=headers)
check.equal(unsupported.status_code, 404,
            "media without a recommender are named as unsupported")

# --- a reason names the part this item credits, not the part that matched ---
# Live on 2026-09-06: "Austin Powers in Goldmember" was offered as "features
# Jay Roach", who directs it and appears in no cast anywhere.
crew_seed = show("crew-seed", "Watched Film", progress=80,
                 genres=("Comedy",), people=(("Pat Helm", "Actor"),))
directed = show("directed", "Directed By The Same Hand", genres=("Comedy",),
                people=(("Pat Helm", "Director"),))
double = show("double", "Acted And Wrote", genres=("Comedy",),
              people=(("Pat Helm", "Writer"), ("Pat Helm", "Actor")))
credits = {row["id"]: " ".join(row["reason"])
           for row in recommendations.build(
               [crew_seed, directed, double])["recommendations"]}
check.that("directed by Pat Helm" in credits.get("directed", ""),
           "a director is credited with directing, not with being in the cast")
check.that("features" not in credits.get("directed", ""),
           "a name that never acted in an item is not said to feature in it")
check.that("features Pat Helm" in credits.get("double", ""),
           "somebody credited twice is named by the part a viewer would notice")


harness.cleanup()
raise SystemExit(check.report())
