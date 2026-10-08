"""A song in an imported list is matched to its own recording, not the first exact title.

Written from a real import (2026-10-05). The catalogue lists the studio cut beside live albums,
rehearsal takes, re-recordings and single edits, all with the same title and credit, and the
importer asked for whichever came first: a live "Linger" from a Paris concert for a list whose
own Album column said "Stars: The Best of the Cranberries", "Satisfaction" from Flashpoint for
Out of Our Heads. The list's file named the album of every song, and its "Apple - id" column
names the very recording, whose length Apple will tell anybody who asks.
"""
import time

import harness

harness.setup(BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              MUSIC_DAILY_CAP="3", MUSIC_TRACK_COST="1",
              IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="")

from app import buskarr, imports, itunes, jellyfin, media, songs, store, wants

check = harness.Check("import recording fit")
store.init()


def hit(title, album, length, artist="The Cranberries", source="deezer"):
    return {"itemKey": f"bk:track:{title}|{album}|{length}", "medium": "music", "unit": "track",
            "title": title, "artist": artist, "album": album, "durationSeconds": length,
            "source": source}


class Catalogue:
    """Answers each (query, sources) from a table, and writes down what it was asked."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, query, medium, unit="", user=None, sources=()):
        self.asked.append((query, tuple(sources)))
        return list(self.answers.get((query, tuple(sources)), []))


def matched(row, answers, known=None, unit="track"):
    catalogue = Catalogue(answers)
    wants.search = catalogue
    found = imports.match(None, media.MUSIC, unit, row, set(), known)
    return found, catalogue.asked


DEEZER, EVERYWHERE = ("deezer",), ()

print("the file's album decides between exact hits")
row = imports.Row(2, "Linger", "The Cranberries", album="Stars: The Best of the Cranberries 1992-2002")
paris = hit("Linger", "Live 2010 - Zenith Paris, 22.03.10", 295.0)
stars = hit("Linger", "Stars: The Best of the Cranberries 1992-2002", 274.8, source="itunes")
found, asked = matched(row, {
    ("The Cranberries Linger", DEEZER): [paris],
    ("The Cranberries Linger", EVERYWHERE): [paris, stars],
})
check.equal(found["hit"]["album"], stars["album"], "the hit on the file's own album is asked for")
check.equal(found["state"], imports.MATCHED, "and it is still an exact match, asked for at once")
check.equal(asked, [("The Cranberries Linger", DEEZER), ("The Cranberries Linger", EVERYWHERE)],
            "a Deezer answer with only a live album sends it to the full search, and no further")
check.that("fits the list best" in found["detail"], f"the detail says why: {found['detail']!r}")

print("\nDeezer is enough when it already has the album")
found, asked = matched(row, {("The Cranberries Linger", DEEZER): [paris, stars]})
check.equal(found["hit"]["album"], stars["album"], "the album hit wins wherever it is in the list")
check.equal(len(asked), 1, "and one search was all it took")

print("\na search naming the album, when neither search found it")
row = imports.Row(3, "(I Can't Get No) Satisfaction", "The Rolling Stones", album="Out of Our Heads")
query = "The Rolling Stones (I Can't Get No) Satisfaction"
stones = dict(artist="The Rolling Stones")
london = hit("(I Can't Get No) Satisfaction", "Live in London", 224.9, **stones, source="itunes")
flash = hit("(I Can't Get No) Satisfaction", "Flashpoint", 344.0, **stones, source="musicbrainz")
heads = hit("(I Can't Get No) Satisfaction", "Out of Our Heads", 223.4, **stones, source="itunes")
found, asked = matched(row, {
    (query, DEEZER): [hit("(I Can't Get No) Satisfaction (Live)", "GRRR Live!", 419.0, **stones)],
    (query, EVERYWHERE): [london, flash],
    (query + " Out of Our Heads", EVERYWHERE): [flash, heads],
}, known={"title": "(I Can't Get No) Satisfaction", "artist": "The Rolling Stones",
          "album": "Out of Our Heads", "duration": 222.9})
check.equal(found["hit"]["album"], "Out of Our Heads", "the album search finds the studio cut")
check.equal(asked[-1], (query + " Out of Our Heads", EVERYWHERE),
            "it was asked for because the right length was only on a live album")

print("\nApple's length is the recording: it beats the album")
row = imports.Row(4, "Fly Like an Eagle", "Steve Miller Band", album="Greatest Hits 1974-78")
miller = dict(artist="Steve Miller Band")
album_cut = hit("Fly Like An Eagle", "Greatest Hits 1974-78", 283.0, **miller)
single_edit = hit("Fly Like an Eagle", "Rock Anthems of the 70s", 186.0, **miller)
eagle = "Steve Miller Band Fly Like an Eagle"
apple_eagle = {"title": "Fly Like an Eagle", "artist": "Steve Miller Band",
               "album": "Greatest Hits 1974-78", "duration": 185.2}
found, asked = matched(row, {(eagle, DEEZER): [album_cut, single_edit]}, known=apple_eagle)
check.equal(found["hit"]["durationSeconds"], 186.0,
            "the single edit the person had, though another hit carries their album's name")
check.equal(asked[-1], (eagle + " Greatest Hits 1974-78", EVERYWHERE),
            "and the album was still searched for, since the edit was on another album")
greatest_edit = hit("Fly Like an Eagle", "Greatest Hits 1974-78", 185.5, **miller, source="itunes")
found, asked = matched(row, {(eagle, DEEZER): [album_cut, single_edit],
                             (eagle + " Greatest Hits 1974-78", EVERYWHERE): [greatest_edit]},
                       known=apple_eagle)
check.equal((found["hit"]["album"], found["hit"]["durationSeconds"]),
            ("Greatest Hits 1974-78", 185.5),
            "where the album search finds the edit on his own album, that one")

print("\na hit crediting a guest the row does not is the duet")
row = imports.Row(10, "Bubbly", "Colbie Caillat", album="Coco")
colbie = dict(artist="Colbie Caillat")
duet = hit("Bubbly (feat. Amos Lee)", "This Time Around", 198.0, **colbie)
coco = hit("Bubbly", "Coco", 196.28, **colbie, source="itunes")
found, asked = matched(row, {("Colbie Caillat Bubbly", DEEZER): [duet],
                             ("Colbie Caillat Bubbly", EVERYWHERE): [duet, coco]},
                       known={"title": "Bubbly", "artist": "Colbie Caillat", "album": "Coco",
                              "duration": 196.3})
check.equal(found["hit"]["title"], "Bubbly", "the song on Coco, not the duet of the same length")
# With only a length to go on, the album cannot tell them apart: the guest has to.
row = imports.Row(11, "Bubbly", "Colbie Caillat")
solo = hit("Bubbly", "Some Compilation", 196.0, **colbie)
by_length = {"title": "Bubbly", "artist": "Colbie Caillat", "album": "", "duration": 196.3}
found, asked = matched(row, {("Colbie Caillat Bubbly", DEEZER): [duet, solo]}, known=by_length)
check.equal(found["hit"]["title"], "Bubbly", "of two hits of the right length, not the duet")
found, asked = matched(row, {("Colbie Caillat Bubbly", DEEZER): [duet],
                             ("Colbie Caillat Bubbly", EVERYWHERE): [duet, solo]}, known=by_length)
check.equal((found["hit"]["title"], len(asked)), ("Bubbly", 2),
            "and a duet alone on Deezer does not settle the search")
for title, credit, other, extra in [
        ("Bubbly", "Colbie Caillat", "Bubbly (feat. Amos Lee)", True),
        ("Mood (feat. iann dior)", "24kGoldn", "Mood (feat. iann dior)", False),
        ("Best Friend", "Saweetie, Doja Cat", "Best Friend (feat. Doja Cat)", False),
        ("Stay", "Zedd & Alessia Cara", "Stay (feat. Alessia Cara)", False),
        ("Lovely Day", "Bill Withers", "Lovely Day", False)]:
    check.equal(songs.extra_guest(title, credit, other), extra, f"{other!r} for {title!r} by {credit!r}")

print("\nApple's record of some other song is no evidence")
row = imports.Row(4, "Fly Like an Eagle", "Steve Miller Band", album="Greatest Hits 1974-78")
wanted = imports._wanted(row, {"title": "Something Else Entirely", "artist": "Nobody",
                               "album": "Elsewhere", "duration": 100.0})
check.equal((wanted.album, wanted.duration), ("Greatest Hits 1974-78", None),
            "a lookup that answered with another song is set aside for the row's own album")

print("\nwith nothing but a title, a live album is the last choice")
row = imports.Row(5, "Crazy", "Gnarls Barkley")
gnarls = dict(artist="Gnarls Barkley")
basement = hit("Crazy", "Live from the Basement", 295.0, **gnarls)
elsewhere = hit("Crazy", "St. Elsewhere", 182.0, **gnarls)
found, asked = matched(row, {("Gnarls Barkley Crazy", DEEZER): [basement, elsewhere]})
check.equal(found["hit"]["album"], "St. Elsewhere", "the studio album over the live one")
found, asked = matched(row, {("Gnarls Barkley Crazy", DEEZER): [basement],
                             ("Gnarls Barkley Crazy", EVERYWHERE): [basement, elsewhere]})
check.equal((found["hit"]["album"], len(asked)), ("St. Elsewhere", 2),
            "a live album alone on Deezer sends the row to the full search")
found, asked = matched(row, {("Gnarls Barkley Crazy", DEEZER): [basement]})
check.equal(found["hit"]["album"], basement["album"],
            "and when a live take is all there is, it is still the match")

print("\na row that is itself a live take is not steered away from live albums")
wanted = imports._wanted(imports.Row(6, "Bridge Over Troubled Water [Live]", "Simon & Garfunkel",
                                     album="Greatest Hits"), None)
check.equal(imports._fit(hit("x", "The Concert in Central Park", 300.0), wanted)[1], 0,
            "no mark against a live album for a live row")

print("\na few seconds nearer Apple's length does not beat the record itself")
row = imports.Row(8, "Shop Around", "The Miracles", album="Hi ...We're The Miracles")
miracles = dict(artist="The Miracles")
studio = hit("Shop Around", "Hi ...We're The Miracles", 165.0, **miracles)
rerecorded = hit("Shop Around", "Recorded Live On Stage", 170.0, **miracles)
found, asked = matched(row, {("The Miracles Shop Around", DEEZER): [rerecorded, studio]},
                       known={"title": "Shop Around", "artist": "The Miracles",
                              "album": "Hi ...We're The Miracles", "duration": 170.8})
check.equal(found["hit"]["album"], studio["album"],
            "the studio cut on his own album, though the live one is nearer by five seconds")
check.equal(len(asked), 1, "and the album's own hit settles the search")

print("\nan album named after the artist is not every compilation with the name in it")
row = imports.Row(9, "Rosalita (Come Out Tonight)", "Bruce Springsteen",
                  album="The Essential Bruce Springsteen [Disc 1]")
boss = dict(artist="Bruce Springsteen")
live_one = hit("Rosalita (Come Out Tonight)", "Bruce Springsteen", 630.0, **boss)
wild = hit("Rosalita (Come Out Tonight)", "The Wild, the Innocent & the E Street Shuffle", 422.0, **boss)
essential = hit("Rosalita (Come Out Tonight)", "The Essential Bruce Springsteen", 424.0, **boss)
query = "Bruce Springsteen Rosalita (Come Out Tonight)"
found, asked = matched(row, {(query, DEEZER): [live_one, wild],
                             (query, EVERYWHERE): [live_one, wild],
                             (query + " The Essential Bruce Springsteen [Disc 1]", EVERYWHERE): [essential]})
check.equal(found["hit"]["album"], "The Essential Bruce Springsteen",
            "the album he named, found by asking for it, not an album called Bruce Springsteen")
check.equal(songs.same_album("The Essential Bruce Springsteen", "Bruce Springsteen",
                             "Bruce Springsteen"), False,
            "an album named after the artist is only itself")
check.equal(songs.same_album("The Essential Bruce Springsteen", "The Essential Bruce Springsteen [Disc 1]",
                             "Bruce Springsteen"), True, "but the same compilation still is")

print("\nan album list is matched as it always was")
row = imports.Row(7, "Kind of Blue", "Miles Davis")
first = {"itemKey": "bk:album:deezer:1", "medium": "music", "unit": "album", "title": "Kind of Blue",
         "artist": "Miles Davis", "album": "", "source": "deezer"}
second = dict(first, itemKey="bk:album:deezer:2")
found, asked = matched(row, {("Miles Davis Kind of Blue", DEEZER): [first, second]}, unit="album")
check.equal((found["hit"]["itemKey"], len(asked)), ("bk:album:deezer:1", 1),
            "the first exact album, from one search")

print("\nwhich album titles name the same release")
for a, b, same in [
        ("Meteora", "Meteora (Deluxe Edition)", True),
        ("Greatest Hits", "Simon & Garfunkel's Greatest Hits", True),
        ("Come and Get Your Love - Single", "Come and Get Your Love", True),
        ("Legend: The Best Of Bob Marley And The Wailers",
         "Legend - The Best Of Bob Marley And The Wailers (Remastered)", True),
        ("Berry Is On Top", "Chuck Berry Is on Top", True),
        ("Любовь", "Любовь (Deluxe)", True),
        ("Greatest Hits", "Greatest Hits Live", False),
        ("Hits", "Greatest Hits", False),
        ("Coco", "Coco - Summer Sessions", False),
        ("", "Coco", False)]:
    check.equal(songs.same_album(a, b), same, f"{a!r} and {b!r}")
for name, staged in [("Live in Rotterdam 1985", True), ("MTV Unplugged", True),
                     ("Coco - Summer Sessions", True), ("Route 66 - The Alternate Takes", True),
                     ("Outtakes", True), ("Frampton Comes Alive!", False),
                     ("Magical Mystery Tour", False), ("Delivery", False), ("Demon Days", False)]:
    check.equal(songs.not_the_record(name), staged, f"{name!r} is a performance or reworking")

print("\nan export's Apple id column is read, library ids are not")
sheet = imports.read(
    "Track name,Artist name,Album,Playlist name,Type,ISRC,Apple - id\n"
    "Linger,The Cranberries,Stars,Library Songs,Favorite,IEABA9300001,1440833098\n"
    "Dreams,The Cranberries,Stars,Library Songs,Favorite,,i.EYVblWbIRD\n")
read_rows, _, _ = imports.rows(sheet, media.MUSIC, "track")
check.equal([row.apple_id for row in read_rows], ["1440833098", ""],
            "a catalogue id is kept and a library id dropped")

print("\nApple is asked in batches, store by store, and remembers its answers")
calls = []


def song(track_id, title, length):
    return {"wrapperType": "track", "trackId": int(track_id), "trackName": title,
            "artistName": "Somebody", "collectionName": "Somewhere",
            "trackTimeMillis": int(length * 1000)}


def apple(url, params):
    ids = params["id"].split(",")
    calls.append((params["country"], len(ids)))
    if params["country"] == "us":
        return [song(i, f"Song {i}", 200) for i in ids if int(i) % 2 == 0]
    if params["country"] == "ca":
        return [song(i, f"Song {i}", 201) for i in ids if int(i) % 3 == 0]
    return []


itunes._get = apple
ids = [str(n) for n in range(1000, 1200)]
known = itunes.songs_by_id(ids + ["i.library", "", "1000"])
check.equal(calls, [("us", 150), ("us", 50), ("ca", 100), ("gb", 67)],
            "two hundred ids: two calls to the first store, then only the misses, store by store")
check.equal(len(known), 100 + 33, "every song any store sold, once each")
check.equal(known["1002"], {"title": "Song 1002", "artist": "Somebody", "album": "Somewhere",
                            "duration": 200.0}, "with its album and length in seconds")
calls.clear()
check.equal(len(itunes.songs_by_id(ids)), 133, "asked again, the same answer")
check.equal(calls, [], "from what was kept, with no call at all, sold or not")


def silent(url, params):
    calls.append((params["country"], len(params["id"].split(","))))
    return None


itunes._get = silent
check.equal(itunes.songs_by_id(["5000"]), {}, "a store that does not answer gives nothing")
itunes._get = apple
calls.clear()
check.equal(itunes.songs_by_id(["5000"]), {"5000": {"title": "Song 5000", "artist": "Somebody",
                                                    "album": "Somewhere", "duration": 200.0}},
            "and is asked again next time, because no answer is not 'not sold'")

print("\nan import of such a file asks for the recording Apple names")
OWNER = jellyfin.User(id="owner", name="owner", is_admin=False)
media._registry = {media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("music",))}
media._registry_built_at = time.monotonic()
media._registry_settled = True
media.owned = lambda *args, **kwargs: jellyfin.Owned()
imports._library_for = lambda *args: None
asked_for = []


def want(user, medium, item_key, unit="", hit=None, **kwargs):
    asked_for.append(hit)
    return "on_its_way", ""


wants.want = want
itunes._get = lambda url, params: [
    {"wrapperType": "track", "trackId": 1440833098, "trackName": "Fly Like an Eagle",
     "artistName": "Steve Miller Band", "collectionName": "Greatest Hits 1974-78",
     "trackTimeMillis": 185200}]
wants.search = Catalogue({("Steve Miller Band Fly Like an Eagle", DEEZER): [album_cut, single_edit]})
batch_id = imports.start(
    OWNER, media.MUSIC, "track", "export.csv",
    "Track name,Artist name,Album,Type,Apple - id\n"
    "Fly Like an Eagle,Steve Miller Band,Greatest Hits 1974-78,Favorite,1440833098\n")
deadline = time.monotonic() + 5
while imports.get(OWNER, batch_id)["state"] == imports.READING and time.monotonic() < deadline:
    time.sleep(.01)
check.equal([h["durationSeconds"] for h in asked_for], [186.0],
            "the single edit Apple names was asked for, not the album cut")

harness.cleanup()
raise SystemExit(check.report())
