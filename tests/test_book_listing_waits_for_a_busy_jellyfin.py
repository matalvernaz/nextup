"""The whole-library book listing waits for a Jellyfin that is busy, not just slow.

Every request to Jellyfin gave up after 30 seconds. Listing an account's whole
book library -- 3,700 books with their people, overviews and play state -- took
15 and 22 seconds for Matt's two book libraries on a quiet server, and 55 and 61
seconds while Jellyfin ran a library scan (measured 2026-11-01). So whenever a
scan was running, the listing timed out: requests whose books had arrived were
not marked as arrived, a followed series could not be looked at, and the
shelves went stale. The logs had fourteen of those timeouts in a day, all on
the two accounts with the most books.

The listing now has a longer wait of its own. The ordinary timeout is left as
it was for every other request, which answer in well under a second.

The test is a real HTTP server on this machine that takes its time, with both
waits scaled down: one second of thinking against a quarter-second ordinary
timeout and a five-second listing one.
"""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("book-listing-busy-jellyfin")

harness.discard(DB_PATH)

import httpx

from app import jellyfin

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


#: How long the pretend Jellyfin takes to answer a listing.
THINKING_SECONDS = 1.0


class BusyJellyfin(BaseHTTPRequestHandler):
    """Answers every `/Items` with one book, after thinking about it."""

    def do_GET(self):
        time.sleep(THINKING_SECONDS)
        body = b'{"Items": [{"Id": "book-1", "Name": "Mossflower", "Type": "AudioBook"}], "TotalRecordCount": 1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        # The test prints only what went wrong.
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), BusyJellyfin)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["JELLYFIN_URL"] = f"http://127.0.0.1:{server.server_address[1]}"

# One book library, so the listing makes exactly one request.
jellyfin.library_ids = lambda medium: ["library-1"] if medium == "book" else []

# Both waits scaled down by the same idea: the ordinary one is shorter than
# the server's thinking, the listing one comfortably longer.
jellyfin._TIMEOUT = httpx.Timeout(0.25, connect=1.0)
jellyfin._LISTING_TIMEOUT = httpx.Timeout(5.0, connect=1.0)

try:
    listed = jellyfin.books("user-1")
except jellyfin.JellyfinUnavailable as exc:
    failures.append(f"the book listing gave up on a busy Jellyfin: {exc}")
else:
    check("the book listing waits for a busy Jellyfin and gets the book",
          [book["Id"] for book in listed], ["book-1"])

# An ordinary request still gives up on the same slow server at the ordinary
# timeout, so the longer wait is the listing's alone.
try:
    with jellyfin._client() as client:
        client.get("/Items").raise_for_status()
    failures.append("an ordinary request waited past the ordinary timeout")
except httpx.TimeoutException:
    pass

server.shutdown()
harness.discard(DB_PATH)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("ok")
