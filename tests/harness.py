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
    """Point the app at a throwaway database. Returns its path."""
    global _tempdir
    _tempdir = tempfile.mkdtemp(prefix="nextup-tests-")
    db_path = os.path.join(_tempdir, f"{TEST_DB_PREFIX}{os.getpid()}.db")

    os.environ["DB_PATH"] = db_path
    os.environ["JELLYFIN_URL"] = "http://jellyfin.invalid:8096"
    os.environ["JELLYFIN_TOKEN"] = "test-token-not-a-real-one"
    os.environ["JELLYFIN_USER"] = ""
    for key, value in overrides.items():
        os.environ[key] = value
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
