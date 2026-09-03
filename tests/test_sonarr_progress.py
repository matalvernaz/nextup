"""The Sonarr facts behind a whole-series request's progress sentence."""
import harness

harness.setup(
    SONARR_URL="http://sonarr.invalid", SONARR_API_KEY="k",
    SONARR_QUALITY_PROFILE_ID="4",
)

from app import sonarr  # noqa: E402

check = harness.Check("sonarr progress")


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return self

    def json(self):
        return self.body


class Client:
    def __init__(self, queue):
        self.queue = queue
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def get(self, path, params=None):
        self.requests.append((path, params))
        if path == "/series":
            return Response([
                {
                    "id": 89,
                    "statistics": {"episodeCount": 153},
                },
                {
                    "id": 90,
                    "statistics": {"episodeCount": 24},
                },
            ])
        return Response(self.queue)


class Tool:
    configured = True

    def __init__(self, client):
        self._client = client

    def client(self, timeout=None):
        return self._client


client = Client([
    {"id": 1, "seriesId": 89, "episodeId": 10},
    {"id": 2, "seriesId": 89, "episodeId": 11},
    {"id": 3, "seriesId": 89, "episodeId": 11},
    {"id": 4, "seriesId": 90, "episodeId": 12},
])
sonarr.backend = lambda: Tool(client)
progress = sonarr.acquisition_progress({"89", "90"})

check.equal(progress, {
    "89": sonarr.AcquisitionProgress(153, 2),
    "90": sonarr.AcquisitionProgress(24, 1),
}, "series statistics and distinct queued episodes are retained")
check.equal(client.requests, [
    ("/series", None),
    ("/queue/details", None),
], "any number of request rows costs two Sonarr reads")

# Queue detail is useful but not authoritative for the total. A malformed
# queue response must not throw away the two counts the series row supplied.
client = Client({"unexpected": "shape"})
sonarr.backend = lambda: Tool(client)
progress = sonarr.acquisition_progress({"89"})
check.equal(progress, {"89": sonarr.AcquisitionProgress(153, None)},
            "unknown queue detail does not erase the known episode total")

check.equal(sonarr._count(True), None,
            "a JSON boolean is not accepted as an episode count")
check.equal(sonarr._count(-1), None,
            "a negative count is rejected")

harness.cleanup()
raise SystemExit(check.report())
