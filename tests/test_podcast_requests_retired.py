"""Podcast requests from before podcasts were added through Jellyfin, closed once.

Podcasts are added in EchoFin now, through the Jellyfin fork (Matt,
2026-10-27). At start-up the requests this service took for them are closed in
one transaction: every podcast request deleted, outstanding or arrived; a
podcast ask waiting for a keyholder declined, saying where shows are added
now; a queued imported podcast row refused, saying the same; somebody's usual
choice of episodes, and any podcast cap or allowance reset, deleted. Nothing
of any other medium is touched, a second start does nothing, and a failure
part way leaves everything as it was.
"""
import sqlite3
import time

import harness

harness.setup()

from app import held, store  # noqa: E402

check = harness.Check("podcast requests retired")
SENTENCE = "Podcasts are added in EchoFin now: Library, Podcasts, Add a Show."
ANNA, BEN = "u-anna", "u-ben"


def sql(statement, *args):
    with store.db() as conn:
        return [tuple(row) for row in conn.execute(statement, args).fetchall()]


def marker():
    return sql("SELECT value FROM meta WHERE key=?", store.PODCASTS_RETIRED)


def other_media():
    """Every row the migration must not touch, as plain tuples."""
    return {
        "requests": sql("SELECT * FROM requests WHERE medium!='podcast' ORDER BY item_key"),
        "held": sql("SELECT * FROM held_requests WHERE medium!='podcast' ORDER BY item_key"),
        "queue": sql("SELECT * FROM import_queue WHERE medium!='podcast' ORDER BY id"),
        "rows": sql("SELECT * FROM import_rows WHERE import_id='songs' ORDER BY line"),
        "settings": sql("SELECT * FROM user_settings WHERE name!='PODCAST_EPISODES' "
                        "ORDER BY user_key, name"),
        "caps": sql("SELECT * FROM account_caps WHERE medium!='podcast' ORDER BY user_key"),
        "resets": sql("SELECT * FROM allowance_resets WHERE medium!='podcast' ORDER BY user_key"),
    }


# A database as the last release left it: every table, and no marker.
store.init()
check.that(marker() != [], "a new database is marked at its first start")
sql("DELETE FROM meta WHERE key=?", store.PODCASTS_RETIRED)

# --- one of every kind of podcast row, and the same kinds for other media -------
store.record(ANNA, "podcast", "feed:coming", "podcast", "Still Coming", "", 1, "pg-1")
store.record(ANNA, "podcast", "feed:arrived", "podcast", "Already Here", "", 1, "pg-2")
sql("UPDATE requests SET fulfilled_at=? WHERE item_key='feed:arrived'", time.time())
store.record(ANNA, "movie", "tmdb:1", "movie", "A Film", "2020", 1, "7")
store.record(BEN, "series", "tvdb:2", "series", "A Show", "2021", 1, "8")

store.hold(ANNA, "podcast", "feed:held", "podcast", "Past The Limit", "", "", "",
           {"title": "Past The Limit"}, "latest:3")
store.hold(BEN, "podcast", "feed:said-no", "podcast", "Said No To", "", "", "",
           {"title": "Said No To"}, "")
store.decline_held("podcast", "feed:said-no", "Not this one.", "matt")
store.hold(ANNA, "movie", "tmdb:9", "movie", "A Held Film", "", "", "",
           {"title": "A Held Film"}, "")

store.put_import("pods", ANNA, "podcast", "done", 1, {"medium": "podcast"})
store.put_import_row("pods", {"line": 2, "state": "matched", "title": "Queued Show"},
                     "waiting")
store.queue_rows(ANNA, "podcast", "pods", [(2, "Queued Show", {"title": "Queued Show"})])
store.put_import("songs", ANNA, "music", "done", 1, {"medium": "music"})
store.put_import_row("songs", {"line": 2, "state": "matched", "title": "A Song"},
                     "waiting")
store.queue_rows(ANNA, "music", "songs", [(2, "A Song", {"title": "A Song"})])

# Written as the old release wrote them: the episodes setting is no longer one
# `put_user_setting` accepts.
for user_key, name, value in ((ANNA, "PODCAST_EPISODES", "latest:3"),
                              (BEN, "PODCAST_EPISODES", "all"),
                              (ANNA, "SERIES_SEASONS", "first")):
    sql("INSERT INTO user_settings (user_key, name, value, set_at) VALUES (?,?,?,?)",
        user_key, name, value, time.time())
store.set_cap(ANNA, "podcast", 5)
store.set_cap(ANNA, "movie", 3)
store.reset_allowance(ANNA, "podcast")
store.reset_allowance(BEN, "series")

untouched = other_media()
check.equal(len(untouched["requests"]), 2, "two requests for other media to leave alone")

# --- a failure part way leaves everything as it was -----------------------------
# The last thing the migration deletes refuses to go, so every statement before
# it has already run inside the transaction when it fails.
sql("CREATE TRIGGER refuse BEFORE DELETE ON allowance_resets "
    "WHEN old.medium='podcast' BEGIN SELECT RAISE(ABORT, 'refused'); END")
check.raises(sqlite3.IntegrityError, store.init,
             "a migration that fails part way stops start-up with the error")
check.equal(len(sql("SELECT 1 FROM requests WHERE medium='podcast'")), 2,
            "and the podcast requests are still there")
check.equal(sql("SELECT state FROM held_requests WHERE item_key='feed:held'"),
            [("waiting",)], "the held ask still waiting")
check.equal(sql("SELECT 1 FROM user_settings WHERE name='PODCAST_EPISODES'"),
            [(1,), (1,)], "the choices of episodes still kept")
check.equal(marker(), [], "and nothing marked as done")
sql("DROP TRIGGER refuse")

# --- the migration ----------------------------------------------------------------
store.init()

check.equal(sql("SELECT item_key FROM requests WHERE medium='podcast'"), [],
            "every podcast request goes, outstanding or arrived")

declined = store.held(ANNA, "podcast", "feed:held")
check.equal(declined["state"], store.HELD_DECLINED,
            "an ask past the limit, waiting for a keyholder, is declined")
check.equal(declined["reason"], SENTENCE, "saying where shows are added now")
check.equal(declined["decided_by"], "", "with nobody named as deciding")
check.that(declined["decided_at"] is not None, "and when")
shown = held.describe(declined)
check.equal((shown["state"], shown["reason"], "decidedBy" in shown),
            (held.DECLINED, SENTENCE, False),
            "and the asker's list shows it as a no, with the sentence")

said_no = store.held(BEN, "podcast", "feed:said-no")
check.equal((said_no["reason"], said_no["decided_by"]), ("Not this one.", "matt"),
            "a no a keyholder already gave stays theirs")

refused = store.import_rows("pods")
check.equal([(row["outcome"], row["outcomeDetail"]) for row in refused],
            [("refused", SENTENCE)],
            "a queued imported podcast row is refused, saying the same")
check.equal(sql("SELECT 1 FROM import_queue WHERE medium='podcast'"), [],
            "and leaves the queue")

check.equal(sql("SELECT 1 FROM user_settings WHERE name='PODCAST_EPISODES'"), [],
            "everybody's usual choice of episodes goes")
check.equal(sql("SELECT 1 FROM account_caps WHERE medium='podcast'"), [],
            "so do podcast caps")
check.equal(sql("SELECT 1 FROM allowance_resets WHERE medium='podcast'"), [],
            "and podcast allowance resets")
check.that(marker() != [], "and it is marked as done")

check.equal(other_media(), untouched,
            "nothing of any other medium, and no other setting, is touched")

# --- once ----------------------------------------------------------------------------
store.record(BEN, "podcast", "feed:later", "podcast", "Written Later", "", 1, "pg-3")
store.hold(BEN, "podcast", "feed:held-later", "podcast", "Held Later", "", "", "",
           {"title": "Held Later"}, "")
store.init()
check.equal(sql("SELECT item_key FROM requests WHERE medium='podcast'"),
            [("feed:later",)], "a second start leaves a podcast row alone")
check.equal(store.held(BEN, "podcast", "feed:held-later")["state"], store.HELD_WAITING,
            "and a waiting ask waiting")

harness.cleanup()
raise SystemExit(check.report())
