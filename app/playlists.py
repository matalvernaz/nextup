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


def _position(current: list[str], placed: dict[int, str], line: int) -> int | None:
    """Where a song from `line` of the list goes in the playlist as it stands.

    Just after the latest earlier line of the same list that is in it, else
    just before the earliest later one, else at the end. Songs somebody put in
    the playlist themselves are left where they are.
    """
    index_of: dict[str, int] = {}
    for index, item in enumerate(current):
        index_of.setdefault(item, index)
    earlier = [index_of[item] for at, item in placed.items()
               if at < line and item in index_of]
    if earlier:
        return max(earlier) + 1
    later = [index_of[item] for at, item in placed.items()
             if at > line and item in index_of]
    return min(later) if later else None


def add_found(user: jellyfin.User, playlist_id: str, import_id: str,
              found: list[tuple[int, str]]) -> int:
    """Put songs already in the library into the playlist, in list order.

    `found` is (line, item id) in the order of the file. A song already in the
    playlist is not added again, but its line is still written down, so a
    later arrival knows where it stands. Returns how many were added.
    """
    if not found:
        return 0
    current = jellyfin.playlist_items(user.id, playlist_id)
    if current is None:
        raise Refused("The playlist has been deleted in Jellyfin.")
    present = set(current)
    new = []
    for line, item in found:
        if item not in present:
            new.append(item)
            present.add(item)
    placed = store.playlist_lines(playlist_id, import_id)
    jellyfin.add_to_playlist(user.id, playlist_id, new,
                             _position(current, placed, found[0][0]) if new else None)
    store.add_playlist_lines(playlist_id, import_id, found)
    return len(new)


def wait_for(user: jellyfin.User, playlist_id: str, import_id: str, line: int,
             title: str, artist: str, album: str = "") -> None:
    """Remember a song of the list that is not in the library yet."""
    store.add_pending(user.key, playlist_id, import_id, line, title, artist,
                      album)


def summary(import_id: str, playlist: dict | None) -> dict | None:
    """What a page or a client says about a list's playlist."""
    if not playlist:
        return None
    return {"name": playlist.get("name") or "",
            "added": store.playlist_line_count(import_id),
            "pending": store.pending_count(import_id)}


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
        current = jellyfin.playlist_items(user.id, playlist_id)
        if current is None:
            dropped = store.drop_pending_for(playlist_id)
            log.info("playlist %s is gone; %d waiting song(s) dropped",
                     playlist_id, dropped)
            continue
        placed = store.playlist_lines(playlist_id, import_id)
        for row in rows:
            item = library.find(row["title"], row["artist"], row["album"])
            if item is None:
                continue
            if item not in current:
                position = _position(current, placed, row["line"])
                jellyfin.add_to_playlist(user.id, playlist_id, [item], position)
                current.insert(len(current) if position is None else position,
                               item)
                added += 1
            store.add_playlist_lines(playlist_id, import_id, [(row["line"], item)])
            placed[row["line"]] = item
            store.drop_pending(row["id"])
    if added:
        log.info("playlists: %d song(s) that turned up were added", added)
    return added
