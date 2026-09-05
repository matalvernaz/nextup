"""The book engine's view of the one store.

Two things live here. The first is a translation: the audiobook code asks
about ASINs and this hands those questions to the shared ledger as
``medium='book'`` rows. Keeping the translation in one small module meant the
engine, the shelves, the series planner and the request path all moved between
services without a single call site changing, so a failure during the merge
could only be in one of these functions.

The second is the small amount of state that is genuinely book-only and has no
counterpart in film or television: the run log, the ranked snapshots that make
a later request attributable to the run that suggested it, and the feedback
those snapshots are matched against.

The caches -- the Audible similarity graph, the cached products, the ASIN
aliases, the shelves, the document vectors -- are re-exported from the shared
store unchanged. They were never keyed by medium and there is nothing to
translate.
"""
import json
import time

from .. import config, store
from ..store import (  # noqa: F401  (re-exported for the book engine)
    db,
    dismiss,
    dismissed_asins,
    forget_shelf,
    get_audible_alias,
    get_product,
    get_shelf,
    get_sims,
    get_vectors,
    init,
    rekey_users,
    put_audible_alias,
    put_product,
    put_shelf,
    put_sims,
    put_vectors,
    user_key_scheme,
)

#: The medium every row written through this module belongs to.
MEDIUM = "book"

#: Books have one unit here. Asking for a whole series is a separate path that
#: records one row per book, because a series is not acquired as one thing.
UNIT = "book"


def _authors_of(payload) -> list[str]:
    """The stored author list. Empty for a row written before the column."""
    if not payload:
        return []
    try:
        names = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str) and n]


def record_request(user_key: str, asin: str, title: str,
                   authors: list | tuple = ()) -> bool:
    """Log that this account asked for a book. True when it is a new request.

    Idempotent on purpose: a second tap on the same book must not restart the
    "still looking" clock or spend another day's allowance.

    The title and authors are kept because the ASIN is not enough to recognise
    the book when it lands. It arrives tagged with whichever ASIN the other
    marketplace issued for the same edition, so arrival is decided on the title
    with an author to agree with it.
    """
    return store.record(user_key, MEDIUM, asin, UNIT, title, "", 1, "",
                        authors=json.dumps(list(authors)))


def requests_for(user_key: str) -> list[dict]:
    """Every book this account has asked for, newest first."""
    rows = store.active(user_key, MEDIUM, arrived_since=0.0)
    return [{"asin": row["item_key"], "title": row["title"],
             "authors": _authors_of(row["authors"]),
             "requested_at": row["requested_at"],
             "fulfilled_at": row["fulfilled_at"]}
            for row in rows]


def requests_since(user_key: str, cutoff: float) -> int:
    """How many books this account has asked for since `cutoff`.

    Drives the daily cap. Sums cost rather than counting rows, which for books
    is the same number -- every book costs one -- but keeps the arithmetic in
    one place for the day a book series is priced like a music artist.
    """
    return store.spent_today(user_key, MEDIUM, cutoff)


def fulfil_requests(user_key: str, asins: set) -> None:
    """Stop the clock on requests whose book has since reached the library."""
    store.mark_arrived(user_key, MEDIUM, set(asins))


def forget_request(user_key: str, asin: str) -> bool:
    """Erase one request. True when there was one to erase.

    A delete rather than another timestamp column: an abandoned request is not
    a state the shelf has anything to say about, and leaving the row would keep
    it counting against the day's allowance for a book nobody is getting.
    """
    return store.forget(user_key, MEDIUM, asin)


def outstanding_request_users(asin: str) -> set:
    """Every account still waiting on this book, including the one asking.

    Cancelling calls an acquisition off, and the row in Listenarr belongs to
    the household rather than to whoever asked last. The shared helper excludes
    the caller, which is the right question for "may I delete this"; this is
    the wider one the book path asks.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT user_key FROM requests WHERE medium=? AND item_key=? "
            "AND fulfilled_at IS NULL", (MEDIUM, asin)).fetchall()
    return {row["user_key"] for row in rows}


def ordered_asins() -> set:
    """Every book still on order for the household: any account, unfulfilled.

    What "already being looked for" means, and nothing more. `suppressed_asins`
    is the wider net the shelf uses and folds in dismissals and fulfilled
    requests; read as "on order" it would tell somebody a book they hid is on
    its way.
    """
    return store.outstanding_item_keys(MEDIUM)


def suppressed_asins(user_key: str) -> set:
    """Every acquisition, plus the books this account has hidden.

    Requests are suppressed for everyone rather than only for the person who
    made them: Listenarr is shared, so a book one listener asks for is acquired
    once and should not still be offered to the other nine as if it were
    unowned.
    """
    cutoff = time.time() - config.DISMISS_TTL_DAYS * 86400
    with db() as conn:
        rows = conn.execute(
            "SELECT item_key AS asin FROM requests WHERE medium=? "
            "UNION SELECT asin FROM dismissed "
            "WHERE user_key=? AND dismissed_at>?",
            (MEDIUM, user_key, cutoff)).fetchall()
    return {row["asin"] for row in rows}


def undismiss(user_key: str, asin: str) -> bool:
    """Remove this account's active dismissal. True when one existed."""
    return store.restore(user_key, asin)


# --- The run log, and what makes a request attributable ----------------------


def start_run(user_key: str) -> int:
    with db() as conn:
        cur = conn.execute("INSERT INTO runs(user_key,started_at) VALUES(?,?)",
                           (user_key, time.time()))
        return cur.lastrowid


def finish_run(run_id: int, seeds: int, owned: int, unowned: int,
               note: str = "") -> None:
    with db() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, seeds=?, owned=?, unowned=?, "
            "note=? WHERE id=?",
            (time.time(), seeds, owned, unowned, note, run_id))


def record_recommendations(run_id: int, user_key: str, surface: str,
                           rows: list[dict], ranker_version: str) -> None:
    """Persist the ranked rows needed to attribute later feedback."""
    now = time.time()
    values = []
    for rank, row in enumerate(rows, start=1):
        recommendation_id = row.get("recommendation_id")
        item_key = row.get("id") if surface == "owned" else row.get("asin")
        if not recommendation_id or not item_key:
            continue
        values.append((recommendation_id, run_id, user_key, surface, item_key,
                       rank, float(row.get("score") or 0),
                       row.get("source") or "unknown",
                       json.dumps(row.get("why") or []), ranker_version, now))
    if not values:
        return
    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO recommendation_items("
            "recommendation_id,run_id,user_key,surface,item_key,rank,score,"
            "source,reasons,ranker_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)


def record_feedback(user_key: str, asin: str, action: str,
                    recommendation_id: str | None = None) -> None:
    """Record an outcome, accepting attribution only for this account's ASIN.

    An unvalidated `recommendation_id` is dropped rather than stored: it is
    supplied by the caller, and a row claiming one account acted on another's
    recommendation would quietly corrupt every comparison made from this table.
    """
    validated = None
    with db() as conn:
        if recommendation_id:
            row = conn.execute(
                "SELECT 1 FROM recommendation_items "
                "WHERE recommendation_id=? AND user_key=? AND item_key=?",
                (recommendation_id, user_key, asin)).fetchone()
            if row:
                validated = recommendation_id
        conn.execute(
            "INSERT INTO feedback_events("
            "user_key,asin,action,recommendation_id,occurred_at) "
            "VALUES(?,?,?,?,?)",
            (user_key, asin, action, validated, time.time()))


def prune_attribution() -> None:
    """Bound attribution history without orphaning surviving feedback."""
    cutoff = time.time() - config.ATTRIBUTION_RETENTION_DAYS * 86400
    with db() as conn:
        conn.execute("DELETE FROM feedback_events WHERE occurred_at<?", (cutoff,))
        conn.execute(
            "DELETE FROM recommendation_items AS recommendation "
            "WHERE created_at<? AND NOT EXISTS ("
            "SELECT 1 FROM feedback_events AS feedback "
            "WHERE feedback.recommendation_id="
            "recommendation.recommendation_id)", (cutoff,))


def last_run(user_key: str):
    """The most recent finished run for this account, or None."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE user_key=? AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (user_key,)).fetchone()
