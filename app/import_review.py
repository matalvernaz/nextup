"""Review old music matches without searching, acquiring, or deleting library files.

Run with the service stopped, after backing up its database:
  python -m app.import_review --database /data/nextup.db
  python -m app.import_review --database /data/nextup.db --apply

The default only reports a plan. Explicit user selections are preserved.
"""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from . import imports, media

VERSION = imports.MATCHING_VERSION


def plan(conn: sqlite3.Connection) -> list[dict]:
    changes = []
    batches = conn.execute("SELECT * FROM imports WHERE medium='music' AND state IN ('ready','done','failed')").fetchall()
    for batch in batches:
        payload = json.loads(batch['payload'])
        if payload.get('unit') != 'track' or payload.get('matching_version', 0) >= VERSION:
            continue
        rows = conn.execute('SELECT * FROM import_rows WHERE import_id=? ORDER BY line',
                            (batch['import_id'],)).fetchall()
        for raw in rows:
            old = dict(raw)
            data = json.loads(raw['data'])
            hit = data.get('hit') or {}
            if not hit or data.get('manual'):
                continue
            # A near match that somebody ticked was an explicit choice. Its
            # title is allowed to differ; it must not be undone by this pass.
            if raw['outcome'] and raw['state'] != imports.MATCHED:
                continue
            row = imports.Row(raw['line'], data['title'], data.get('artist') or '', data.get('year') or '')
            if imports.is_strict(row, hit, media.MUSIC):
                continue
            if raw['outcome'] not in ('', imports.WAITING, imports.ASKED, imports.ALREADY):
                continue
            if raw['outcome'] == imports.WAITING:
                pending = conn.execute('SELECT hit FROM import_queue WHERE import_id=? AND line=?',
                                       (batch['import_id'], raw['line'])).fetchall()
                if not any(json.loads(r['hit']).get('itemKey') == hit.get('itemKey') for r in pending):
                    # Older releases did not update the report when a queue
                    # entry was sent or cleared. Its history is ambiguous.
                    continue
            new = dict(old)
            if raw['outcome'] in (imports.ASKED, imports.ALREADY):
                reason = 'earlier_request'
                data['reviewNote'] = ('An earlier automatic request selected a different recording. '
                                      'Review it before choosing a replacement; the earlier request remains in your requests.')
            else:
                reason = 'queued_automatic' if raw['outcome'] == imports.WAITING else 'unresolved_suggestion'
                plausible = imports._close_score(row, hit, media.MUSIC) >= .7
                new['state'] = imports.UNCERTAIN if plausible else imports.MISSING
                new['outcome'] = ''
                new['outcome_detail'] = ''
                data['detail'] = 'This earlier automatic match needs review.' if plausible else imports.NO_MATCH
                if not plausible:
                    data['previousHit'] = hit
                    data['hit'] = None
                    data['others'] = 0
            new['data'] = json.dumps(data)
            changes.append(dict(import_id=batch['import_id'], line=raw['line'], reason=reason,
                                old=old, new=new, title=data['title'], selected=hit.get('title'),
                                candidate_key=hit.get('itemKey', '')))
    return changes


def apply(conn: sqlite3.Connection, changes: list[dict]) -> dict:
    """Apply a reviewed plan atomically; reject a plan if a row has since changed."""
    counts = Counter()
    conn.execute('BEGIN IMMEDIATE')
    try:
        for change in changes:
            old, new = change['old'], change['new']
            current = conn.execute('SELECT * FROM import_rows WHERE import_id=? AND line=?',
                                   (change['import_id'], change['line'])).fetchone()
            if current is None or dict(current) != old:
                raise RuntimeError('The import changed after review; make a fresh plan.')
            if change['reason'] == 'queued_automatic':
                queued = conn.execute('SELECT id,hit FROM import_queue WHERE import_id=? AND line=?',
                                      (change['import_id'], change['line'])).fetchall()
                matching = [r['id'] for r in queued if json.loads(r['hit']).get('itemKey') == change['candidate_key']]
                if not matching:
                    # It may have been sent after the snapshot. Never turn
                    # an accepted request back into an undecided queue row.
                    raise RuntimeError('A queued request changed after review; stop the service and review again.')
                conn.executemany('DELETE FROM import_queue WHERE id=?', [(n,) for n in matching])
                conn.execute('DELETE FROM playlist_pending WHERE import_id=? AND line=?',
                             (change['import_id'], change['line']))
                counts['unqueued'] += len(matching)
            conn.execute('UPDATE import_rows SET state=?,data=?,outcome=?,outcome_detail=? WHERE import_id=? AND line=?',
                         (new['state'], new['data'], new['outcome'], new['outcome_detail'], change['import_id'], change['line']))
            counts[change['reason']] += 1
        for import_id in {c['import_id'] for c in changes}:
            raw = conn.execute('SELECT * FROM imports WHERE import_id=?', (import_id,)).fetchone()
            payload = json.loads(raw['payload'])
            payload['matching_version'] = VERSION
            payload['review_notice'] = ('Earlier automatic matches were checked again. Questionable queued matches '
                                        'now need a decision; explicit selections were preserved. '
                                        'This older import did not retain its original file or record types.')
            undecided = conn.execute("SELECT 1 FROM import_rows WHERE import_id=? AND outcome='' AND state IN ('matched','uncertain') LIMIT 1",
                                     (import_id,)).fetchone()
            state = imports.READY if undecided else imports.DONE
            if raw['state'] == imports.FAILED:
                state = imports.FAILED
            conn.execute('UPDATE imports SET payload=?,state=? WHERE import_id=?',
                         (json.dumps(payload), state, import_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    uri = Path(args.database).resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro')
    with sqlite3.connect(uri, uri=True, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        changes = plan(conn)
        summary = {'changes': dict(Counter(c['reason'] for c in changes)),
                   'rows': [{k: c[k] for k in ('import_id', 'line', 'reason', 'title', 'selected')} for c in changes]}
        if args.apply:
            summary['applied'] = apply(conn, changes)
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
