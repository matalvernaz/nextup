"""Every playlist Nextup makes belongs to its owner alone.

2026-10-10: a listener could see every other account's reading list ("Next Read — defender" and
the rest) and another person's playlist, and EchoFin offered to delete them. Jellyfin makes a new
playlist public unless the request says otherwise, and neither place here that makes one said.
"""
import json

import httpx

import harness

harness.setup(JELLYFIN_USER="")

from app import jellyfin  # noqa: E402

check = harness.Check("playlists are private")
made = []


def handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/Playlists":
        made.append(json.loads(request.content))
        return httpx.Response(200, json={"Id": f"p{len(made)}"})
    if request.method == "GET" and request.url.path == "/Items":
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})
    return httpx.Response(204)


real_client = jellyfin._client
jellyfin._client = lambda: httpx.Client(base_url="http://jellyfin.invalid",
                                        transport=httpx.MockTransport(handler))
try:
    check.equal(jellyfin.create_playlist("user-1", "Road Trip"), "p1",
                "an imported list's playlist is made")
    check.equal(made[0].get("IsPublic"), False, "and kept to its owner")
    check.equal(jellyfin.set_playlist("user-1", "Next Read — kid", ["i1", "i2"]), "p2",
                "a reading list is made")
    check.equal(made[1].get("IsPublic"), False, "and kept to its owner too")
    check.equal(made[1].get("UserId"), "user-1", "owned by the account it is for")
finally:
    jellyfin._client = real_client

harness.cleanup()
raise SystemExit(check.report())
