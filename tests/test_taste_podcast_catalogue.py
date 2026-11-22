"""The podcast catalogue shelf follows what an account said about its suggestions."""
import harness

harness.setup()

from app import config, itunes, jellyfin, podcasts, store, taste  # noqa: E402

check = harness.Check("marks on the podcast catalogue")
store.init()
config.PODCAST_RECOMMENDATION_LIMIT = 10
USER = jellyfin.User("listener-id", "Listener")
taste._start = lambda target, name: target()

jellyfin.library_ids = lambda medium: ["lib-pods"] if medium == "podcast" else []
jellyfin.podcasts_owned = lambda: []
podcasts.forget()

listening = {"progress": 40}


def library(medium, uid, libraries):
    return [{"Id": "jf-held", "Name": "Held Show", "Genres": ["Comedy", "True Crime"],
             "Studios": [{"Name": "Acme"}], "ProviderIds": {},
             "UserData": {"PlayedPercentage": listening["progress"]}}]


jellyfin.recommendation_items_for_user = library

calls: list[dict] = []
real_suggestions = itunes.suggestions


def recording(genres, studios, exclude_keys, exclude_titles, limit, **marks):
    calls.append({"genres": genres, "studios": studios,
                  "exclude": set(exclude_keys), "marks": marks})
    return [
        {"itemKey": "feed:crime", "title": "Crime Show", "artist": "ACME",
         "genres": ["True Crime"], "itunesId": "11", "reason": ["r"], "score": 2.0},
        {"itemKey": "feed:laughs", "title": "Laughs", "artist": "Other",
         "genres": ["Comedy"], "itunesId": "12", "reason": ["r"], "score": 1.0},
    ]


itunes.suggestions = recording


def keys(rows):
    return [row["itemKey"] for row in rows]


# --- no marks: the search it always was ------------------------------------------
first = podcasts.catalogue_shelf(USER, force=True)
check.equal(calls[-1]["marks"], {},
            "with no marks the catalogue is searched exactly as it always was")
check.equal(calls[-1]["genres"], ["Comedy", "True Crime"], "from the account's genres")
check.equal(calls[-1]["studios"], ["Acme"], "and makers")
check.equal(keys(first), ["feed:crime", "feed:laughs"], "and every row is offered")

# A shelf cached before marks existed is a bare list, and still served.
store.put_external(podcasts._cache_key(USER), first)
check.equal(keys(podcasts.catalogue_shelf(USER)), ["feed:crime", "feed:laughs"],
            "a shelf cached before marks is served as it was")
check.equal(len(calls), 1, "and not rebuilt, with no mark to outdate it")


# --- less like this ----------------------------------------------------------------
# The shelf the mark is made from, as the app saw it.
store.put_external(podcasts._cache_key(USER), {"rows": first, "revision": 0})
from app import feedback, recommendations  # noqa: E402

recommendations.rebuild_after_mark = lambda user, medium: None
feedback.take(USER, "podcast", "feed:crime", taste.CATALOGUE, taste.LESS, False)
mark = taste.marks(USER.key, "podcast")[0]
check.equal(mark.details.get("genres"), ["True Crime"],
            "the mark keeps what the catalogue said about the show")
check.equal(mark.details.get("artist"), "ACME", "including who makes it")
check.equal(len(calls), 2, "the mark rebuilt the cached shelf behind the answer")
check.that("feed:crime" in calls[-1]["exclude"], "and the rebuild leaves the show out")
check.that("feed:crime" not in keys(podcasts.catalogue_shelf(USER)),
           "nor is it served again")

# The library's "Acme" and the mark's "ACME" are one maker. Listened to enough,
# the library wins and the maker keeps its bonus; never both bonus and penalty.
check.equal(calls[-1]["studios"], ["Acme"],
            "a maker liked more than it was marked down keeps its bonus")
check.that("acme" not in calls[-1]["marks"].get("avoid_makers", set()),
           "and is not avoided as well")
listening["progress"] = 10
podcasts.catalogue_shelf(USER, force=True)
check.equal(calls[-1]["studios"], [],
            "marked down more than it is liked, it loses the bonus")
check.equal(calls[-1]["marks"].get("avoid_makers"), {"acme"},
            "and is avoided, under one spelling")
listening["progress"] = 40


# Two spellings in the library itself, each liked a little, and one mark down
# that outweighs them together: no spelling may keep the bonus while the maker
# is avoided.
profile = podcasts._Profile()
profile.add_item({"Genres": ["Crime"], "Studios": [{"Name": "Acme"}]}, 0.3, False)
profile.add_item({"Genres": ["crime"], "Studios": [{"Name": "ACME"}]}, 0.2, False)
profile.add_mark(taste.Mark("podcast", "k", taste.CATALOGUE, taste.LESS, False,
                            details={"artist": "acme", "genres": ["CRIME"]}), -0.75)
check.equal(profile.ranked_makers(), [],
            "every spelling of a maker marked down overall loses the bonus")
check.equal(profile.avoided_makers(), {"acme"}, "and the maker is avoided once")
check.equal(profile.ranked_genres(), [],
            "a genre marked down overall is not searched for under any spelling")
check.equal(profile.avoided_genres(), {"crime"}, "and is avoided once")


# --- more like this on a show nobody here has heard ------------------------------------
store.put_external(podcasts._cache_key(USER), {"rows": [
    {"itemKey": "feed:history", "title": "Old Times", "artist": "Archive Co",
     "genres": ["History"], "itunesId": "13"}], "revision": 0})
feedback.take(USER, "podcast", "feed:history", taste.CATALOGUE, taste.MORE, False)
check.that("History" in calls[-1]["genres"], "more like this adds its genre to the search")
check.equal(calls[-1]["marks"].get("asked_genres"), {"history"},
            "marked as resting only on more like this")
shown = podcasts.catalogue_shelf(USER)
check.that(all(row.get("opinion") is None for row in shown
               if row["itemKey"] != "feed:history"),
           "only the show marked says more like this")


# --- the catalogue's own scoring --------------------------------------------------------
itunes.suggestions = real_suggestions
hits = {
    "comedy": [
        {"itemKey": "feed:a", "title": "Comedy Crime", "artist": "Somebody",
         "genres": ["Comedy", "True Crime"]},
        {"itemKey": "feed:b", "title": "Just Comedy", "artist": "Somebody",
         "genres": ["Comedy"]},
        {"itemKey": "feed:c", "title": "Avoided Maker", "artist": "Bad Co",
         "genres": ["Comedy"]},
    ],
    "history": [
        {"itemKey": "feed:d", "title": "Old Days", "artist": "Archive Co",
         "genres": ["History"]},
    ],
}
itunes.search = lambda term, limit=50: hits.get(term.casefold(), [])
plain = itunes.suggestions(["Comedy", "History"], ["Archive Co"], set(), set(), 10)
check.equal(sorted(row["itemKey"] for row in plain),
            ["feed:a", "feed:b", "feed:c", "feed:d"], "with no marks every hit is offered")
marked = itunes.suggestions(["Comedy", "History"], ["Archive Co"], set(), set(), 10,
                            avoid_genres={"true crime"}, avoid_makers={"bad co"},
                            asked_genres={"history"}, asked_makers={"archive co"})
check.equal(sorted(row["itemKey"] for row in marked), ["feed:b", "feed:d"],
            "an avoided genre or maker counts a show out")
old_days = next(row for row in marked if row["itemKey"] == "feed:d")
check.equal(old_days["reason"], [
    "from Archive Co, like a podcast you asked for more of",
    "shares the History genre with podcasts you asked for more of"],
    "reasons resting only on more like this say so")
check.equal(next(row for row in plain if row["itemKey"] == "feed:d")["reason"], [
    "from Archive Co, whose podcasts you listen to",
    "shares the History genre with podcasts you listen to"],
    "and without marks they read as they always did")


# --- a show marked more like this is never like itself ------------------------------
# The search finds the marked show again beside one like it: the marked one stays,
# says why, and earns nothing from its own genre or maker.
hits["history"] = [
    {"itemKey": "feed:self", "title": "Marked Show", "artist": "Archive Co",
     "genres": ["History"]},
    {"itemKey": "feed:like", "title": "Like It", "artist": "Archive Co",
     "genres": ["History"]},
]
both = itunes.suggestions(["History", "Comedy"], ["Archive Co"], set(), set(), 10,
                          asked_genres={"history"}, asked_makers={"archive co"},
                          asked_keys={"feed:self"}, own_genres=["Comedy"],
                          own_makers=[])
by_key = {row["itemKey"]: row for row in both}
check.equal((by_key["feed:self"]["score"], by_key["feed:self"]["reason"]),
            (0.0, ["you asked for more like this"]),
            "the marked show is kept, scored without its own mark")
check.that(by_key["feed:like"]["score"] > 0 and any(
    "asked for more of" in reason for reason in by_key["feed:like"]["reason"]),
    "while a show like it is pulled up by the mark, and says so")

# The same through the shelf, from the mark to the row the app is given.
itunes.search = lambda term, limit=50: hits.get(term.casefold(), [])
store.put_external(podcasts._cache_key(USER), {"rows": [
    {"itemKey": "feed:self", "title": "Marked Show", "artist": "Archive Co",
     "genres": ["History"], "itunesId": "21"}], "revision": 0})
feedback.take(USER, "podcast", "feed:self", taste.CATALOGUE, taste.MORE, False)
shelf = {row["itemKey"]: row for row in podcasts.catalogue_shelf(USER)}
check.that("feed:self" in shelf, "the marked show is still offered")
check.equal(shelf["feed:self"].get("opinion"), "more", "and says it was asked about")
check.equal(shelf["feed:self"]["reason"], ["you asked for more like this"],
            "without being called like a podcast you asked for more of: itself")

# When the search does not bring the marked show back at all, it is kept from
# what the catalogue said when it was marked, so its way back is still there.
hits["history"] = [
    {"itemKey": "feed:like", "title": "Like It", "artist": "Archive Co",
     "genres": ["History"]},
]
podcasts.catalogue_shelf(USER, force=True)
again = {row["itemKey"]: row for row in podcasts.catalogue_shelf(USER)}
check.that("feed:self" in again, "a marked show the search missed stays on the list")
check.equal(again.get("feed:self", {}).get("opinion"), "more", "still saying so")
check.equal(again.get("feed:self", {}).get("title"), "Marked Show",
            "with what it was called")

# And a full list keeps it, past the limit.
config.PODCAST_RECOMMENDATION_LIMIT = 1
hits["history"] = [
    {"itemKey": "feed:like", "title": "Like It", "artist": "Archive Co",
     "genres": ["History"]},
    {"itemKey": "feed:self", "title": "Marked Show", "artist": "Archive Co",
     "genres": ["History"]},
]
crowded = itunes.suggestions(["History"], ["Archive Co"], set(), set(), 1,
                             asked_keys={"feed:self"}, own_genres=[], own_makers=[])
check.equal([row["itemKey"] for row in crowded], ["feed:like", "feed:self"],
            "a marked show stays on a full list, after the rest")
config.PODCAST_RECOMMENDATION_LIMIT = 10

# A mark taken back is as if it had never been made: a show rated low here is
# still not held against its genre, as it never was.
SECOND = jellyfin.User("second-id", "Second")
jellyfin.recommendation_items_for_user = lambda medium, uid, libraries: [
    {"Id": "jf-good", "Name": "Good Show", "Genres": ["Comedy"], "Studios": [],
     "ProviderIds": {}, "UserData": {"PlayedPercentage": 40}},
    {"Id": "jf-poor", "Name": "Poor Show", "Genres": ["Drama"], "Studios": [],
     "ProviderIds": {}, "UserData": {"Rating": 2}},
]
itunes.suggestions = recording
taste.record(SECOND.key, "podcast", "jf-poor", taste.OWNED, taste.LESS, False)
taste.record(SECOND.key, "podcast", "jf-poor", taste.OWNED, "", False)
podcasts.catalogue_shelf(SECOND, force=True)
check.equal(calls[-1]["marks"], {},
            "a mark taken back leaves the search as it was with no mark at all")

harness.cleanup()
raise SystemExit(check.report())
