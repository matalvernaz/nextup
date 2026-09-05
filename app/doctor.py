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
import sys

from . import backends, config, jellyfin, media, selfcheck

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


def _route_line() -> tuple[str, bool]:
    """The same-origin route, which is the easiest half to leave out."""
    if not config.PUBLIC_URL:
        return ("Same-origin route: PUBLIC_URL is unset, so it is not "
                "checked. Set it to where clients reach this service at the "
                "Jellyfin origin and this line becomes useful.", True)
    problem = selfcheck.check(config.PUBLIC_URL)
    if problem:
        return f"Same-origin route: {problem}", False
    return f"Same-origin route: {config.PUBLIC_URL} answers.", True


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

    line, ok = _route_line()
    healthy &= ok
    sections.append("Discovery\n  " + line.removeprefix("Same-origin route: "))

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
