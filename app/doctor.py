"""What this installation can and cannot do, printed before it matters.

    docker compose run --rm nextup python -m app.doctor

Every failure this reports is one that otherwise has no symptom. A backend
with an empty quality profile, a Radarr on the wrong port, a Jellyfin library
that does not exist yet, a same-origin route the proxy never got -- each of
them leaves a container that starts, reports healthy, and serves a client that
draws no control and says nothing about why.

Read-only throughout: it asks four backends and Jellyfin whether they are
there, and changes nothing.
"""
import sqlite3
import sys

from . import (backends, config, external_books, jellyfin, media,
               selfcheck, store, tmdb)

#: What a medium needs from Jellyfin, said the way a person would look for it.
_LIBRARY_KIND = {"movie": "Movies", "series": "Shows",
                 "music": "Music", "book": "Books"}

#: What a medium is called in a sentence about it. Not the medium key with an
#: "s" on the end, which gives "Seriess" and "Musics".
_PLURAL = {"movie": "Films", "series": "Series", "music": "Music",
           "book": "Books"}


def _jellyfin_line() -> tuple[str, bool]:
    """Jellyfin, which is the one backend nothing works without."""
    if not config.JELLYFIN_TOKEN:
        return ("Jellyfin: JELLYFIN_TOKEN is unset. Nothing will work: it is "
                "how this service reads the library and introspects callers.",
                False)
    try:
        users = jellyfin.all_users()
    except Exception as exc:  # noqa: BLE001 -- any failure is the same news
        return (f"Jellyfin: {config.JELLYFIN_URL} did not answer "
                f"({exc.__class__.__name__}). Inside Docker, 'localhost' is "
                "this container rather than the host.", False)
    if jellyfin.credential_rejected():
        return (f"Jellyfin: {config.JELLYFIN_URL} is refusing this service's "
                "API key. Create one in Dashboard -> API Keys and set "
                "JELLYFIN_TOKEN to it.", False)
    return (f"Jellyfin: {config.JELLYFIN_URL} answered, "
            f"{len(users)} account(s).", True)


def _backend_lines() -> tuple[list[str], bool]:
    lines: list[str] = []
    ok = True
    for status in backends.statuses(force=True):
        if not status.configured:
            lines.append(
                f"{status.name}: {status.detail}. "
                f"{_PLURAL.get(status.medium, status.medium)} "
                "will not be offered.")
            continue
        if status.reachable is False:
            ok = False
            lines.append(f"{status.name}: configured but not answering. "
                         f"{status.detail}")
            continue
        lines.append(f"{status.name}: answered.")
    return lines, ok


def _recommendation_source_lines() -> tuple[list[str], bool]:
    """The outside catalogues a shelf may consult, and whether they answer.

    Every one of these is optional and every one fails silently by design -- a
    shelf built without them is a shelf with fewer signals, not an error. Which
    is exactly why they belong here: an unreachable Radarr shows up as a search
    box that finds nothing, and a mistyped TMDb key shows up as nothing at all.

    Deliberately not in `backends`. That module answers "can this medium be
    acquired", one status per medium, and a rating source acquires nothing. A
    Status with no medium would be a lie in the shape of the type.
    """
    lines: list[str] = []
    ok = True

    if tmdb.configured():
        # A real lookup, not a ping. TMDb's error for a bad key is a 401 on the
        # first request and nothing at all before it, so "configured" and
        # "working" are only distinguishable by asking something.
        found = tmdb.recommendations(_TMDB_PROBE_ID, "movie")
        if found:
            lines.append(
                f"TMDb: answered, {len(found)} neighbour(s) for the probe "
                "title. Films and shows will be recommended from it.")
        else:
            ok = False
            lines.append(
                "TMDb: a key is set but the catalogue returned nothing. "
                "Check TMDB_API_KEY; films and shows will fall back to this "
                "library's own metadata.")
    else:
        lines.append(
            "TMDb: no key, so films and shows are recommended from this "
            "library's metadata alone. Set TMDB_API_KEY to add the "
            "'people who liked this also liked' signal.")

    # Caught rather than allowed to raise. This module is the thing somebody
    # runs when the installation is broken, and a database that has never been
    # initialised is one of the ways it can be -- a doctor that dies of the
    # condition it was called to diagnose is no use at all.
    try:
        connected = store.accounts_with_setting("HARDCOVER_TOKEN")
    except sqlite3.Error as exc:
        ok = False
        lines.append(f"Hardcover accounts: the database could not be read "
                     f"({exc}). Nothing per-account can be reported.")
    else:
        lines.append(
            f"Hardcover accounts: {connected} listener(s) have connected their "
            "own, so their ratings, reading history and want-to-read shelf "
            "shape their own suggestions."
            if connected else
            "Hardcover accounts: nobody has connected one. A listener can, "
            "from Your reading accounts, and it only ever reads their own "
            "shelf.")

    sources = external_books.configured_sources()
    lines.append(
        "Book ratings: " + ", ".join(sources) + "."
        if sources else
        "Book ratings: none configured, so books are ranked without one.")

    if config.HARDCOVER_TOKEN:
        # The one source whose query shape could not be verified when it was
        # written -- doing so needs a token from an account. So it is asked a
        # real question here, and a schema that has moved shows up as a line
        # somebody reads rather than as a signal that never fires.
        try:
            rows = external_books.hardcover_books(_HARDCOVER_PROBE_TITLE)
        except Exception as exc:  # noqa: BLE001 -- the message is the point
            ok = False
            lines.append(f"Hardcover: the query was refused ({exc}). "
                         "Book ratings will come from the other sources.")
        else:
            lines.append(
                f"Hardcover: answered, {len(rows)} row(s) for the probe title.")

    return lines, ok


#: Fight Club, which TMDb has had since the beginning and which has neighbours.
#: A probe needs a title the catalogue certainly knows, so that an empty answer
#: means the key is wrong rather than the film being obscure.
_TMDB_PROBE_ID = "550"

#: Likewise: a title every book catalogue carries.
_HARDCOVER_PROBE_TITLE = "Dune"


def _library_lines() -> tuple[list[str], bool]:
    """Which media are actually served, which is the answer clients act on."""
    lines: list[str] = []
    ok = True
    try:
        offered = media.available()
    except Exception as exc:  # noqa: BLE001
        return ([f"Media: could not be worked out ({exc.__class__.__name__})."],
                False)
    if not offered:
        return (["Media: none. Every backend is either unconfigured or "
                 "has no matching Jellyfin library, so a client will show no "
                 "controls at all."], False)
    for key, found in sorted(offered.items()):
        if key == "book" and found.library_ids:
            # A books library exists on stock Jellyfin too, and there it holds
            # ebooks. Offering the medium would give a search box that can ask
            # for audiobooks and a library that can never show one arrive.
            serves = jellyfin.serves_audiobooks()
            if serves is False:
                ok = False
                lines.append(
                    "Books: a Books library was found, but this Jellyfin does "
                    "not file audiobooks as whole books. That needs the "
                    "audiobook fork; on stock Jellyfin the library holds "
                    "ebooks and nothing would ever read as arrived.")
                continue
            if serves is None:
                lines.append("Books: could not ask this Jellyfin whether it "
                             "serves audiobooks; will be retried.")
        if not found.library_ids:
            ok = False
            lines.append(
                f"{found.label}: offered, but Jellyfin has no "
                f"{_LIBRARY_KIND.get(key, 'matching')} library. Requests "
                "will work and nothing will ever read as arrived.")
        else:
            lines.append(f"{found.label}: {len(found.library_ids)} "
                         f"library(ies), units {', '.join(found.units)}, "
                         f"{found.daily_cap} per account per day.")
    return lines, ok


def _route_lines() -> tuple[list[str], bool]:
    """The same-origin routes, which are the easiest half to leave out."""
    if not config.PUBLIC_URLS:
        return (["Same-origin route: PUBLIC_URL is unset, so it is not "
                 "checked. Set it to where clients reach this service at the "
                 "Jellyfin origin and this line becomes useful."], True)
    lines: list[str] = []
    ok = True
    for url in config.PUBLIC_URLS:
        problem = selfcheck.check(url)
        if problem:
            lines.append(f"Same-origin route: {problem}")
            ok = False
        else:
            lines.append(f"Same-origin route: {url} answers.")
    return lines, ok


def report() -> str:
    """The whole check, as text. Nothing here writes anything anywhere."""
    sections: list[str] = []
    healthy = True

    line, ok = _jellyfin_line()
    healthy &= ok
    sections.append("Jellyfin\n  " + line.removeprefix("Jellyfin: "))

    lines, ok = _backend_lines()
    healthy &= ok
    sections.append("Backends\n  " + "\n  ".join(lines))

    lines, ok = _library_lines()
    healthy &= ok
    sections.append("Media offered\n  " + "\n  ".join(lines))

    lines, ok = _recommendation_source_lines()
    healthy &= ok
    sections.append("Recommendation sources\n  " + "\n  ".join(lines))

    lines, ok = _route_lines()
    healthy &= ok
    sections.append("Discovery\n  " + "\n  ".join(
        line.removeprefix("Same-origin route: ") for line in lines))

    verdict = ("Everything this installation is configured for is answering."
               if healthy else
               "Something above will not work. Each line names the setting or "
               "the address to look at.")
    return "\n\n".join(sections) + "\n\n" + verdict + "\n"


def exit_code(text: str) -> int:
    """0 when the report found nothing wrong, so a script can gate on it."""
    return 0 if text.rstrip().endswith("is answering.") else 1


def main() -> int:
    text = report()
    print(text, end="")
    return exit_code(text)


if __name__ == "__main__":
    sys.exit(main())
