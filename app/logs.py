"""Logging setup, and the one rule about what may be logged.

Never log an access token. `fingerprint` exists so a support question about
"which token was rejected" can be answered without the log becoming a place
credentials are kept.
"""
import hashlib
import logging
import sys

from . import config

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("nextup")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.propagate = False
    _configured = True


def get(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"nextup.{name}")


def fingerprint(token: str) -> str:
    """A short stable stand-in for a token, safe to write down."""
    if not token:
        return "none"
    return hashlib.sha256(token.encode()).hexdigest()[:12]
