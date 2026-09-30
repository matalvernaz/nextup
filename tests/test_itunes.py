"""Apple's catalogue: keys, spellings, hit shapes and the shelf built from it."""
import harness

harness.setup()

from app import itunes, store  # noqa: E402

check = harness.Check("itunes")
store.init()

# --- one feed, one key ---------------------------------------------------------
check.equal(itunes.item_key("https://Feeds.Example.invalid/show/"),
            itunes.item_key("https://feeds.example.invalid/show"),
            "host case and a trailing slash do not make two podcasts")
check.that(itunes.item_key("https://a.invalid/x") != itunes.item_key("https://a.invalid/y"),
           "different feeds are different keys")
check.that(itunes.item_key("https://a.invalid/x").startswith("feed:"), "keys say what they are")

# --- titles reduced to what two spellings share ----------------------------------
for title, expected in (("SCP Archives", "SCPARCHIVES"), ("SCPArchives", "SCPARCHIVES"),
                        ("scp-archives", "SCPARCHIVES"), ("Café Noir", "CAFENOIR"),
                        ("", ""), (None, "")):
    check.equal(itunes.normalise_title(title), expected, f"{title!r} normalises")

# --- a catalogue row becomes the hit every other medium's hits look like -------
ROW = {"collectionId": 1131532370, "collectionName": "The Magnus Archives",
       "artistName": "Rusty Quill",
       "feedUrl": "https://feeds.acast.com/public/shows/magnus",
       "artworkUrl600": "https://is1-ssl.mzstatic.com/image/thumb/Podcasts1/v4/aa/bb/cc/mza_1.jpg/600x600bb.jpg",
       "genres": ["Fiction", "Podcasts", "Drama"], "trackCount": 250,
       "releaseDate": "2021-03-25T11:00:00Z"}
hit = itunes._hit(ROW)
check.equal(hit["medium"], "podcast", "medium")
check.equal(hit["unit"], "podcast", "unit")
check.equal(hit["itemKey"], itunes.item_key(ROW["feedUrl"]), "keyed on the feed")
check.equal(hit["title"], "The Magnus Archives", "title")
check.equal(hit["artist"], "Rusty Quill", "who makes it is the artist, as the results page reads it")
check.equal(hit["feedUrl"], ROW["feedUrl"], "the feed travels with the hit")
check.equal(hit["itunesId"], "1131532370", "so does Apple's id")
check.equal(hit["genres"], ["Fiction", "Drama"], "the genre on everything is dropped")
check.equal(hit["episodeCount"], 250, "episode count")
check.equal(hit["year"], "2021", "the year of the newest episode")
check.that(hit["thumbnailUrl"].endswith("160x160bb.jpg"), "artwork is offered at row size")
check.equal(itunes._hit({"collectionName": "No feed"}), None, "a listing without a feed is no hit")

# --- search goes through the cache and never the network in a test --------------
calls = []
itunes._get = lambda url, params: calls.append(params) or [ROW]
first = itunes.search("magnus archives")
check.equal(len(first), 1, "a search answers hits")
check.equal(calls[-1]["term"], "magnus archives", "asked with the words typed")
itunes.search("magnus  archives ")
check.equal(len(calls), 1, "the same words are answered from the cache")
itunes._get = lambda url, params: None
check.equal(itunes.search("brand new words"), [], "a catalogue that did not answer is an empty search, not an error")

# --- the shelf: scored on shared genres, makers, and never what is owned ----------
def fake_search(term, limit=itunes.SEARCH_LIMIT):
    return {
        "Fiction": [
            {**hit, "itemKey": "feed:owned", "title": "Owned Show", "genres": ["Fiction"]},
            {**hit, "itemKey": "feed:drama", "title": "Drama Show", "genres": ["Fiction", "Drama"], "artist": "Someone"},
            {**hit, "itemKey": "feed:rq", "title": "Rusty Show", "genres": ["Fiction"], "artist": "Rusty Quill"},
            {**hit, "itemKey": "feed:titled", "title": "Wooden Overcoats", "genres": ["Fiction"]},
            {**hit, "itemKey": "feed:none", "title": "Cooking", "genres": ["Food"]},
        ],
        "Drama": [
            {**hit, "itemKey": "feed:drama", "title": "Drama Show", "genres": ["Fiction", "Drama"], "artist": "Someone"},
        ],
    }.get(term, [])

itunes.search = fake_search
rows = itunes.suggestions(["Fiction", "Drama"], ["Rusty Quill"],
                          exclude_keys={"feed:owned"},
                          exclude_titles={"wooden overcoats"}, limit=10)
keys = [row["itemKey"] for row in rows]
check.that("feed:owned" not in keys, "an owned podcast is never suggested")
check.that("feed:titled" not in keys, "nor one owned under a title alone")
check.that("feed:none" not in keys, "nor one sharing no genre")
check.equal(keys[0], "feed:rq", "the same maker outranks a second shared genre")
check.equal(keys[1], "feed:drama", "two shared genres come next")
check.that(any("Rusty Quill" in reason for reason in rows[0]["reason"]),
           "and the reason names the maker")
check.that(rows[1]["reason"][0].startswith("shares the Fiction and Drama genres"),
           "a genre reason names the genres shared")
check.equal(itunes.suggestions([], [], set(), set(), 10), [], "nothing to go on is an empty shelf")

harness.cleanup()
raise SystemExit(check.report())
