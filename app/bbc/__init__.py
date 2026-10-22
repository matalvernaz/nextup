"""BBC iPlayer as a medium of its own.

The BBC is not acquired the way films, series and music are. Nothing in the
Sonarr/Radarr world can fetch the BBC's own audio-described versions, which is
the whole point of the medium for a blind listener, so this talks to bbc-ui
instead -- which drives get_iplayer through a UK tunnel and already imports what
it downloads into Jellyfin. This package is nextup's half: the agreed wire shape
(`contract`), the client that speaks it, and a request path that never touches
Sonarr's ledger or TVDB ownership.
"""
