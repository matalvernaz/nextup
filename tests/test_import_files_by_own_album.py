"""A song from a list is filed under the list's own album, not whichever release a search found.

2026-10-10: of 21 songs waiting in an import queue, 14 were about to start an album nobody had,
several named after compilations the catalogue happened to match: "One Kiss" under "Hot Summer
Party Mix", NERO's "Welcome Reality" under "Me and You", where the file said "One Kiss - Single"
and "Welcome Reality (Deluxe Version)". The recording was right; the album it was labelled with,
which buskarr files by, was a stranger's. The list's own album is now the label, with Apple's
" - Single"/" - EP" taken off, and the catalogue's year only when it is the same release.
"""
import harness

harness.setup(BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              MUSIC_DAILY_CAP="3", MUSIC_TRACK_COST="1",
              IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="")

from app import imports, media, store, wants  # noqa: E402

check = harness.Check("import files by own album")
store.init()

Row = imports.Row

print("filed_under")
check.equal(imports.filed_under(Row(1, "One Kiss", "Calvin Harris, Dua Lipa",
                                    album="One Kiss - Single"),
                                {"album": "Hot Summer Party Mix"}),
            "One Kiss", "a compilation the catalogue found gives way to the list's single")
check.equal(imports.filed_under(Row(1, "Welcome Reality", "Nero",
                                    album="Welcome Reality (Deluxe Version)"),
                                {"album": "Me and You"}),
            "Welcome Reality (Deluxe Version)", "and to the list's album, edition and all")
check.equal(imports.filed_under(Row(1, "Rage Valley", "Knife Party", album="Rage Valley - EP"),
                                {"album": "Rage Valley"}),
            None, "the same release leaves the catalogue's label and year alone")
check.equal(imports.filed_under(Row(1, "Thru These Walls", "Phil Collins",
                                    album="Hello, I Must Be Going! (Remastered)"),
                                {"album": "Hello, I Must Be Going!"}),
            None, "an edition of the same album is the same release")
check.equal(imports.filed_under(Row(1, "Crazy", "Gnarls Barkley"), {"album": "St. Elsewhere"}),
            None, "a row naming no album changes nothing")
check.equal(imports.filed_under(Row(1, "Song", "Act", album="An Album"), {}),
            "An Album", "a hit with no album takes the list's")


class Catalogue:
    def __init__(self, answers):
        self.answers = answers

    def __call__(self, query, medium, unit="", user=None, sources=()):
        return list(self.answers.get(query, []))


def hit(title, album, year="2019"):
    return {"itemKey": f"bk:track:{title}|{album}", "medium": "music", "unit": "track",
            "title": title, "artist": "Calvin Harris, Dua Lipa", "album": album, "year": year,
            "durationSeconds": 214.0, "source": "deezer"}


print("match")
wants.search = Catalogue({"Calvin Harris, Dua Lipa One Kiss": [hit("One Kiss", "Hot Summer Party Mix")]})
found = imports.match(None, media.MUSIC, "track", Row(4, "One Kiss", "Calvin Harris, Dua Lipa",
                                                     album="One Kiss - Single"), set())
check.equal(found["hit"]["title"], "One Kiss", "the recording the catalogue found is still the ask")
check.equal(found["hit"]["album"], "One Kiss", "filed under the list's own album")
check.that("year" not in found["hit"], "without the compilation's year")

wants.search = Catalogue({"Calvin Harris, Dua Lipa One Kiss": [hit("One Kiss", "One Kiss")]})
found = imports.match(None, media.MUSIC, "track", Row(4, "One Kiss", "Calvin Harris, Dua Lipa",
                                                     album="One Kiss - Single"), set())
check.equal((found["hit"]["album"], found["hit"].get("year")), ("One Kiss", "2019"),
            "a hit on the list's own album keeps its label and year")

harness.cleanup()
raise SystemExit(check.report())
