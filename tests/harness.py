"""Test setup, and the guard that exists because a suite once wiped production.

Every environment variable a test needs is **set**, never defaulted. The
audiobook service that preceded this one used `os.environ.setdefault` for its
database path, which defers to whatever the surrounding container already had
-- and inside the live container that was the live database. Three test files
ended with `os.remove`. The ledger went with them.

So: the path is set outright, it is always under a temporary directory, and
`cleanup` refuses to delete a file whose name does not identify it as a test
database.
"""
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The only filename shape this module will ever delete.
TEST_DB_PREFIX = "nextup-test-"

_tempdir: str | None = None


def setup(**overrides: str) -> str:
    """Point the app at a throwaway database and an empty deployment.

    Every wiring setting is **cleared**, not merely defaulted around. The four
    Jellyfin variables were set outright from the start and the other thirteen
    were left to whatever the surrounding process happened to hold -- which on
    a laptop and in CI is nothing, and inside the running container is the live
    Radarr, the live Sonarr and their real API keys. Five test files then failed
    there and passed in CI, saying nothing about the code either time: a test
    that forgot to stub `media.available()` read the container's own backends
    and called it a pass, and one that stubbed them read a registry the
    environment had already decided.

    `config` has **two kinds of setting, and they behave oppositely.** The
    names in `_SETTABLE` go through the module's `__getattr__`, so they are read
    from `os.environ` on every access and clearing them here works however late
    a module is imported. Everything else -- `PLAYLIST_OWNER`, `PLAYLIST_NAME`
    and the rest of the plain module-level assignments -- is evaluated once,
    when `config` is first imported, and `__getattr__` is never consulted for a
    name that already exists.

    Reading `settable_names()` is itself what imports `config`, so that import
    freezes the second kind against the environment as it stands a few lines
    above -- before the overrides below are applied. Hence the reload at the
    end, once the environment is final. Without it an override of a
    non-settable name is silently ignored; `test_playlist_upkeep` is the only
    test that notices, because `PLAYLIST_OWNER` is the only such name any test
    overrides.

    The reload is safe here and nowhere else: tests call this before importing
    anything else from `app`, so no module is holding a `from app.config import
    X` copy that a reload would leave stale.
    """
    global _tempdir
    forbid_network()
    _tempdir = tempfile.mkdtemp(prefix="nextup-tests-")
    db_path = os.path.join(_tempdir, f"{TEST_DB_PREFIX}{os.getpid()}.db")

    # Set before the import below, which is the first thing to load
    # `app.config`. `DB_PATH` is not among the settable names and is never
    # cleared -- losing it would point a test at the real database.
    os.environ["DB_PATH"] = db_path
    from app import config
    for name in config.settable_names():
        os.environ.pop(name, None)

    os.environ["JELLYFIN_URL"] = "http://jellyfin.invalid:8096"
    os.environ["JELLYFIN_TOKEN"] = "test-token-not-a-real-one"
    os.environ["JELLYFIN_USER"] = ""
    for key, value in overrides.items():
        os.environ[key] = value

    leaked = sorted(name for name in config.settable_names()
                    if name not in overrides and not name.startswith("JELLYFIN")
                    and os.environ.get(name))
    if leaked:
        raise AssertionError(
            "these settings reached a test from outside it: " + ", ".join(leaked))

    importlib.reload(config)
    return db_path


def cleanup() -> None:
    """Remove the throwaway database, and nothing else.

    The name check is the whole point. A path that does not look like a test
    database is left alone and said so loudly, because the alternative is the
    failure this file is named after.
    """
    global _tempdir
    path = os.environ.get("DB_PATH", "")
    name = os.path.basename(path)
    if name and not name.startswith(TEST_DB_PREFIX):
        raise AssertionError(
            f"refusing to delete {path!r}: not a {TEST_DB_PREFIX}* database")
    if _tempdir and os.path.isdir(_tempdir):
        shutil.rmtree(_tempdir, ignore_errors=True)
    _tempdir = None


#: The outside catalogues, by hostname. Blocked in tests; everything else is
#: not.
#:
#: A blanket "no HTTP at all" guard is the wrong shape here and was tried: the
#: backend probes are *supposed* to make a request to a Radarr that is not
#: there, and half the suite asserts on what happens when one fails. What must
#: never happen is a test reaching a public catalogue, which is a different and
#: much smaller list.
FORBIDDEN_HOSTS = (
    "audible.com", "audible.ca", "audible.co.uk", "audible.com.au",
    "audible.de", "audible.fr", "audible.it", "audible.es",
    "audible.co.jp", "audible.in", "audible.com.br",
    "api.themoviedb.org",
    "openlibrary.org",
    "api.hardcover.app",
    "googleapis.com",
)

_network_guarded = False


def forbid_network() -> None:
    """Make a test that reaches a public catalogue fail loudly, once.

    Five of them are now one `httpx.Client` call away from a test that forgot
    to stub one -- Audible, TMDb, Open Library, Hardcover and Google Books --
    and each has its own way of punishing a suite that reaches it. Audible and
    Open Library rate-limit, Google Books shares one daily quota across every
    anonymous caller and was measured already exhausted from this address, and
    all of them make a green run depend on the network being up.

    The failure is a raised assertion rather than a stub answering nothing,
    because "no rating" is a legitimate result this code path is designed to
    produce. A silent stub would let a broken lookup pass as a book nobody has
    rated.
    """
    global _network_guarded
    if _network_guarded:
        return
    import httpx

    real_send = httpx.Client.send

    def guarded(self, request, **kwargs):
        host = (request.url.host or "").lower()
        if any(host == name or host.endswith("." + name)
               for name in FORBIDDEN_HOSTS):
            raise AssertionError(
                f"a test tried to reach {request.url} for real. Stub the "
                f"catalogue rather than asking it.")
        return real_send(self, request, **kwargs)

    httpx.Client.send = guarded
    _network_guarded = True


def no_book_ratings() -> None:
    """Silence the community-rating lookups for a test that is not about them.

    Any test that builds a real book shelf reaches `external_books`, which is
    three public catalogues. Stubbed to "nobody has rated this", which is a
    state the ranker has to handle anyway and leaves every other signal exactly
    where the test put it.

    Not the default, and not done inside `setup`. A test that builds a shelf
    without saying this gets `forbid_network`'s assertion, which is a sentence
    naming the catalogue -- better than a silently different shelf.
    """
    from app import external_books
    external_books.rating = lambda title, authors, token=None: None
    external_books.cached_rating = lambda title, authors: None
    external_books.pending = lambda title, authors, token=None: False


class Check:
    """A minimal assertion tally, so a file can run under plain python3.

    No test dependency on purpose: the container this ships in has fastapi and
    httpx and nothing else, and a suite that cannot run where the code runs is
    a suite that stops being run.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.passed = 0
        self.failures: list[str] = []

    def that(self, condition: bool, description: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append(description)

    def equal(self, actual, expected, description: str) -> None:
        self.that(actual == expected, f"{description}: {actual!r} != {expected!r}")

    def raises(self, exc_type, fn, description: str) -> None:
        try:
            fn()
        except exc_type:
            self.passed += 1
            return
        except Exception as other:
            self.failures.append(
                f"{description}: raised {type(other).__name__}, not {exc_type.__name__}")
            return
        self.failures.append(f"{description}: nothing raised")

    def report(self) -> int:
        for failure in self.failures:
            print(f"  FAIL {failure}")
        print(f"{self.name}: {self.passed} passed, {len(self.failures)} failed")
        return 1 if self.failures else 0


def use(name: str) -> str:
    """Point one test at its own database, whatever the environment says.

    The other shape of the same guard as `setup`, kept because the audiobook
    suite that came across with the book engine is written against it. Named
    rather than temporary-directoried so a failing run leaves something to
    look at, and `discard` is what deletes it.
    """
    forbid_network()
    path = os.path.join(tempfile.gettempdir(), f"{TEST_DB_PREFIX}{name}.db")
    if os.path.exists(path):
        os.remove(path)
    os.environ["DB_PATH"] = path
    os.environ.setdefault("JELLYFIN_URL", "http://jellyfin.invalid:8096")
    os.environ.setdefault("JELLYFIN_TOKEN", "test-token-not-a-real-one")
    os.environ.setdefault("JELLYFIN_USER", "")
    return path


def discard(path: str) -> None:
    """Delete a test database, and refuse anything that is not one."""
    if not os.path.basename(path).startswith(TEST_DB_PREFIX):
        raise SystemExit(
            f"refusing to delete {path}: that is not a test database")
    if os.path.exists(path):
        os.remove(path)
