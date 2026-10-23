"""BBC iPlayer as a medium of its own.

The BBC is not acquired the way films, series and music are. Nothing in the
Sonarr/Radarr world can fetch the BBC's own audio-described versions, which is
the whole point of the medium for a blind listener, so this talks to bbc-ui
instead -- which drives get_iplayer through a UK tunnel and already imports what
it downloads into Jellyfin. This package is nextup's half: the agreed wire shape
(`contract`), the client that speaks it, and a request path that never touches
Sonarr's ledger or TVDB ownership.
"""

#: The medium key, as `radarr.MEDIUM` is "movie". One string, shared by the
#: registry, the ledger and the request router.
MEDIUM = "bbc"

#: The one unit a BBC ask is in: a single episode. "Get the whole series" is a
#: bounded batch of these, not a unit of its own.
UNIT = "episode"


def configured() -> bool:
    """Whether this deployment can reach bbc-ui at all.

    Configuration only, never reachability -- the same split every backend keeps
    (see `backends`). An unset URL means BBC is simply not offered; whether a
    configured bbc-ui is actually answering is the probe's separate question, so
    a tunnel that is momentarily down never makes the medium vanish.

    `config` is imported inside the function rather than at module scope so that
    importing this package -- which `contract` and its pure-data test do -- pulls
    in no settings.
    """
    from .. import config
    return bool(config.BBC_UI_URL)
