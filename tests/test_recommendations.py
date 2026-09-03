"""The first TV recommendation slice stays personal, explainable and owned."""
import harness

harness.setup()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, config, jellyfin, media, recommendations  # noqa: E402

check = harness.Check("recommendations")
config.SERIES_RECOMMENDATION_LIMIT = 10


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
unrelated = show("unrelated", "No Match", genres=("Horror",))
started = show("started", "Already Started", progress=5,
               genres=("Comedy", "Mystery"))
disliked = show("disliked", "Explicitly Disliked", rating=2,
                genres=("Comedy", "Mystery"))

built = recommendations.build(
    [seed, strong, genre, unrelated, started, disliked])
ids = [row["id"] for row in built["recommendations"]]
check.equal(built["seed_count"], 2, "both watched shows become taste seeds")
check.equal(ids[0], "strong", "the candidate with the strongest evidence ranks first")
check.that("genre" in ids, "genre affinity can support a recommendation")
check.that("unrelated" not in ids, "an unsupported show is not filler")
check.that("started" not in ids, "a started show stays on Jellyfin Next Up")
check.that("disliked" not in ids, "an explicit low rating is never recommended")
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
    rows = jellyfin.series_for_user("user-one", ("library-a", "library-b"))
finally:
    jellyfin._client = real_client
check.equal(len(calls), 2, "each selected library is read once")
check.that(all(params["userId"] == "user-one" for _, params in calls),
           "every library request carries the authenticated user id")
check.equal(len(rows), 1, "a series present through two views is deduplicated")


# --- cache keys include both account and library ----------------------------
library_reads = []
recommendations.jellyfin.library_ids = lambda _medium: ["library-a", "library-b"]
recommendations.jellyfin.series_for_user = (
    lambda uid, libraries: library_reads.append((uid, tuple(libraries))) or [])
recommendations.forget()
user_one = jellyfin.User(id="user-one", name="One")
user_two = jellyfin.User(id="user-two", name="Two")
recommendations.result(user_one, "LIBRARY-A")
recommendations.result(user_one, "library-a")
recommendations.result(user_two, "library-a")
check.equal(library_reads,
            [("user-one", ("library-a",)), ("user-two", ("library-a",))],
            "the same user's shelf is cached without crossing accounts")
check.raises(recommendations.UnknownLibrary,
             lambda: recommendations.result(user_one, "other-library"),
             "a non-TV library is refused")


# --- the API advertises and serves the additive capability -----------------
api._tokens.clear()
api.jellyfin.user_from_token = lambda _token: user_one
api.media.available = lambda: {}
api.recommendations.library_ids = lambda: ("library-a",)
asked = []
api.recommendations.result = lambda user, library_id: (
    asked.append((user.id, library_id)) or {
        "ranker_version": recommendations.RANKER_VERSION,
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
check.equal(blocks[0]["libraryIds"], ["library-a"],
            "capability names the library the shelf covers")

response = client.get(
    "/api/v1/recommendations?medium=series&libraryId=library-a",
    headers=headers)
check.equal(response.status_code, 200, "the owned-TV shelf answers")
check.equal(response.json()["recommendations"][0]["id"], "strong",
            "the API preserves the Jellyfin item id")
check.equal(asked, [("user-one", "library-a")],
            "the endpoint passes its authenticated account and library")

missing_auth = client.get(
    "/api/v1/recommendations?medium=series&libraryId=library-a")
check.equal(missing_auth.status_code, 401,
            "the recommendation shelf cannot use the browser identity fallback")
unsupported = client.get(
    "/api/v1/recommendations?medium=movie&libraryId=library-a",
    headers=headers)
check.equal(unsupported.status_code, 404,
            "media without a recommender are named as unsupported")

harness.cleanup()
raise SystemExit(check.report())
