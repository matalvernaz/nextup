"""Reviewing legacy data must not undo explicit selections or erase already-sent requests."""
import json
import sqlite3

import harness
path = harness.setup()
from app import import_review, imports, store

check = harness.Check('legacy import review')
store.init()
payload = dict(medium='music', unit='track', filename='old.csv')
store.put_import('old', 'owner', 'music', imports.DONE, 5, payload)
for line, state, outcome, title in [(2, 'matched', 'waiting', 'Song (Live)'),
                                    (3, 'uncertain', 'waiting', 'Song (Live)'),
                                    (4, 'matched', 'asked', 'Song (Live)'),
                                    (5, 'held', '', 'Completely Unrelated'),
                                    (6, 'matched', 'waiting', 'Song')]:
    hit = dict(title=title, artist='Artist', unit='track', itemKey='key:' + str(line))
    row = dict(line=line, title='Song', artist='Artist', label='Song by Artist', hit=hit, state=state)
    store.put_import_row('old', row, outcome)
    if outcome == 'waiting':
        store.queue_rows('owner', 'music', 'old', [(line, title, hit)])
store.put_import('new', 'owner', 'music', imports.DONE, 1,
                 dict(payload, matching_version=imports.MATCHING_VERSION))
store.put_import_row('new', dict(row, line=2, hit=dict(hit, title='Song (Live)')), 'waiting')

conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
changes = import_review.plan(conn)
check.equal([(c['line'], c['reason']) for c in changes],
            [(2, 'queued_automatic'), (4, 'earlier_request'), (5, 'unresolved_suggestion')],
            'only old unconfirmed automatic mismatches and unrelated suggestions are reviewed')
check.equal(store.queued_count('owner'), 3, 'planning is read-only')
summary = import_review.apply(conn, changes)
check.equal(summary['unqueued'], 1, 'only the unsafe automatic request is removed from the queue')
check.equal([r['line'] for r in store.queued('owner')], [3, 6], 'manual selections and safe automatic requests keep their place')
rows = {r['line']: r for r in store.import_rows('old')}
check.equal((rows[2]['state'], rows[2]['outcome']), ('uncertain', ''), 'the removed queue row needs a decision')
check.equal((rows[4]['outcome'], bool(rows[4].get('reviewNote'))), ('asked', True), 'already-sent requests are retained with an explanation')
check.equal((rows[5]['state'], rows[5]['hit']), ('missing', None), 'an unrelated held candidate no longer resolves the row')
check.equal(import_review.plan(conn), [], 'applying the review is idempotent')
check.equal(json.loads(store.get_import('old')['payload'])['matching_version'], imports.MATCHING_VERSION,
            'the completed review is recorded')

# A stale plan must not overwrite a later user decision.
store.put_import('stale', 'owner', 'music', 'ready', 1, payload)
store.put_import_row('stale', dict(row, line=2, state='uncertain', hit=dict(hit, title='Unrelated')), '')
stale = import_review.plan(conn)
store.set_import_outcome('stale', 2, 'left')
check.raises(RuntimeError, lambda: import_review.apply(conn, stale), 'a stale plan is refused atomically')
check.equal(store.import_rows('stale')[0]['outcome'], 'left', 'the newer user choice survives')
conn.close()
harness.cleanup()
raise SystemExit(check.report())
