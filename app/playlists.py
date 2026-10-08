"""A Jellyfin playlist made from an imported list of songs.

Somebody uploads a playlist they have elsewhere and names it. The songs already in the library go
into a playlist of that name straight away, in the order of the file; the rest are asked for like
any imported song, and each goes in at its place in the order when it turns up in the library.

The playlist belongs to the person who imported it. Jellyfin will not say who owns a playlist when
asked with this service's key, and every account can see playlists other people have shared or
opened, so a name match is not ownership. This service therefore remembers the playlists it made
and adds only to those: a later list of the same name goes into the same playlist, and a playlist
of that name it did not make is left alone and the import refused, rather than risk adding songs
to somebody else's.

A song already in the playlist is not added a second time.
"""
import time
from itertools import groupby

from . import config, jellyfin, logs, songs, store

log = logs.get("playlists")

#: The longest name kept. Jellyfin takes longer; a person does not type one.
NAME_LIMIT = 100


class Refused(Exception):
    """The playlist cannot be used, and why, in a sentence to show."""


def clean_name(name: str) -> str:
    return " ".join((name or "").split())[:NAME_LIMIT]


def name_key(name: str) -> str:
    return clean_name(name).casefold()


def resolve(user: jellyfin.User, name: str) -> str:
    """The id of the playlist this list goes into, made if need be.

    Raises Refused for a name nobody can use, and JellyfinUnavailable when
    Jellyfin cannot be asked.
    """
    name = clean_name(name)
    if not name:
        raise Refused("Give the playlist a name.")
    key = name_key(name)
    known = store.import_playlist(user.key, key)
    if known is not None:
        if jellyfin.playlist_items(user.id, known["playlist_id"]) is not None:
            return known["playlist_id"]
        # Deleted in Jellyfin since. Its name is free to be made again.
        store.drop_import_playlist(user.key, key)
    for playlist in jellyfin.visible_playlists(user.id):
        if name_key(playlist.get("Name") or "") == key:
            raise Refused(
                f"There is already a playlist called “{name}” that Nextup did not "
                f"make, so it will not add to it. Give this one another name.")
    playlist_id = jellyfin.create_playlist(user.id, name)
    store.put_import_playlist(user.key, key, name, playlist_id)
    log.info("playlist made user=%s name=%r id=%s", user.key, name, playlist_id)
    return playlist_id


def lock(playlist_id: str):
    """Held across reading a playlist, adding to it and writing down where.

    An import and the pass that puts in songs that turned up can both be
    adding to one playlist; without it each could read the song as missing
    and add it.
    """
    return store.key_lock("playlist", playlist_id)


def _position(current: list[str], placed: dict[int, str], line: int) -> int | None:
    """Where a song from `line` of the list goes in the playlist as it stands.

    Just after the latest earlier line of the same list that is in it, else
    just before the earliest later one, else at the end. Songs somebody put in
    the playlist themselves are left where they are. Where somebody has a song
    in it twice, the copy that keeps the new song between its neighbours is
    the one used: after the last copy of an earlier one, before the first copy
    of a later one.
    """
    indices: dict[str, list[int]] = {}
    for index, item in enumerate(current):
        indices.setdefault(item, []).append(index)
    earlier = [max(indices[item]) for at, item in placed.items()
               if at < line and item in indices]
    if earlier:
        return max(earlier) + 1
    later = [min(indices[item]) for at, item in placed.items()
             if at > line and item in indices]
    return min(later) if later else None


def _insert(user: jellyfin.User, playlist_id: str, import_id: str,
            current: list[str], placed: dict[int, str],
            run: list[tuple[int, str]]) -> None:
    """Put a run of songs, consecutive in the list, in at their place."""
    if not run:
        return
    position = _position(current, placed, run[0][0])
    items = [item for _, item in run]
    jellyfin.add_to_playlist(user.id, playlist_id, items, position)
    at = len(current) if position is None else position
    current[at:at] = items
    placed.update(run)
    store.add_playlist_lines(playlist_id, import_id, run)


def add_found(user: jellyfin.User, playlist_id: str, import_id: str,
              found: list[tuple[int, str]]) -> int:
    """Put songs already in the library into the playlist, in list order.

    `found` is (line, item id) in the order of the file. A song already in the
    playlist is not added again, but its line is still written down, so a
    later arrival knows where it stands. Returns how many were added.
    """
    if not found:
        return 0
    with lock(playlist_id):
        current = jellyfin.playlist_items(user.id, playlist_id)
        if current is None:
            raise Refused("The playlist has been deleted in Jellyfin.")
        placed = store.playlist_lines(playlist_id, import_id)
        # A song already in the playlist splits the batch: it stays where it
        # is, and the new songs either side of it go either side of it. Its
        # line is written down first, so the run before it can be placed in
        # front of it.
        present = set(current)
        # In the order of the file. A song that is on several of a file's
        # playlists reaches each of them from the line it was first found on,
        # so the lines handed in here are not always in order.
        found = sorted(found)
        anchors = [(line, item) for line, item in found if item in present]
        placed.update(anchors)
        store.add_playlist_lines(playlist_id, import_id, anchors)
        run: list[tuple[int, str]] = []
        seen: set[str] = set()
        added = 0
        for line, item in found:
            if item in present or (run and any(run[-1][0] < at < line for at in placed)):
                # A song already in the playlist, or one placed since that
                # sits between this line and the run's last: the run goes in
                # first, so nothing lands on the wrong side of it.
                _insert(user, playlist_id, import_id, current, placed, run)
                added += len(run)
                run = []
            if item not in present and item not in seen:
                # The same song on two lines of a list goes in once.
                seen.add(item)
                run.append((line, item))
        _insert(user, playlist_id, import_id, current, placed, run)
        return added + len(run)


def wait_for(user: jellyfin.User, playlist_id: str, import_id: str, line: int,
             title: str, artist: str, album: str = "",
             item_id: str = "") -> None:
    """Remember a song of the list that is not in the playlist yet.

    `item_id` is for a song already found in the library that could not be
    put in just then: it goes in as that very item, not whatever a second
    search would pick.
    """
    store.add_pending(user.key, playlist_id, import_id, line, title, artist,
                      album, item_id)


def summary(import_id: str, playlist: dict | None) -> dict | None:
    """What a page or a client says about a list's playlist.

    `inPlaylist` counts the list's songs that are in it, including any that
    were already there; `pending`, the ones still to go in.
    """
    if not playlist:
        return None
    playlist_id = playlist.get("id") or ""
    return {"id": playlist_id, "name": playlist.get("name") or "",
            "inPlaylist": store.playlist_line_count(import_id, playlist_id),
            "pending": store.pending_count(import_id, playlist_id)}


def summaries(import_id: str, batch: dict) -> list[dict]:
    """`summary` for every playlist a list fills: the form's, then the file's own."""
    found = [batch.get("playlist")] + list(batch.get("playlists") or [])
    seen: set[str] = set()
    out = []
    for playlist in found:
        if playlist and playlist.get("id") not in seen:
            seen.add(playlist.get("id"))
            out.append(summary(import_id, playlist))
    return out


def resolve_pending() -> int:
    """Put in the songs that have turned up in the library since. Returns how many.

    One read of the library per pass, and only when something is waiting. A
    playlist deleted in Jellyfin since takes its waiting songs with it, and a
    song waited for longer than PLAYLIST_PENDING_DAYS is given up on.
    """
    store.prune_pending(time.time() - config.PLAYLIST_PENDING_DAYS * 86400)
    waiting = store.pending_all()
    if not waiting:
        return 0
    library = songs.LibraryIndex.load()
    by_id = {uid: name for name, uid in jellyfin.all_users().items()}
    added = 0
    for (playlist_id, import_id), group in groupby(
            waiting, key=lambda row: (row["playlist_id"], row["import_id"])):
        rows = list(group)
        name = by_id.get(rows[0]["user_key"])
        if not name:
            store.drop_pending_for(playlist_id)
            continue
        user = jellyfin.user(name)
        with lock(playlist_id):
            current = jellyfin.playlist_items(user.id, playlist_id)
            if current is None:
                dropped = store.drop_pending_for(playlist_id)
                log.info("playlist %s is gone; %d waiting song(s) dropped",
                         playlist_id, dropped)
                continue
            placed = store.playlist_lines(playlist_id, import_id)
            for row in rows:
                item = ((row["item_id"] if library.has(row["item_id"]) else None)
                        or library.find(row["title"], row["artist"], row["album"]))
                if item is None:
                    continue
                if item not in current:
                    _insert(user, playlist_id, import_id, current, placed,
                            [(row["line"], item)])
                    added += 1
                else:
                    placed[row["line"]] = item
                    store.add_playlist_lines(playlist_id, import_id,
                                             [(row["line"], item)])
                store.drop_pending(row["id"])
    if added:
        log.info("playlists: %d song(s) that turned up were added", added)
    return added
