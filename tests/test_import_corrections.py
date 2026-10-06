"""A correction keeps its source, quota, queue order and ownership through real web/API routes."""
import json
import re
import threading
import time
from html import unescape
from urllib.parse import parse_qs, urlsplit

import harness

harness.setup(BUSKARR_URL="http://buskarr.invalid", BUSKARR_API_KEY="k",
              MUSIC_DAILY_CAP="3", MUSIC_TRACK_COST="1", IMPORT_MUSIC_DAILY_SONGS="2",
              IMPORT_PAUSE_SECONDS="0", JELLYFIN_USER="")

from fastapi.testclient import TestClient
from app import arr, buskarr, imports, jellyfin, main, media, playlists, sessions, songs, store, wants

check = harness.Check("import corrections")
store.init()
OWNER = jellyfin.User(id="owner", name="owner", is_admin=False)
OTHER = jellyfin.User(id="other", name="other", is_admin=False)
ADMIN = jellyfin.User(id="admin", name="admin", is_admin=True)
USERS = {u.id: u for u in (OWNER, OTHER, ADMIN)}
jellyfin.user_from_token = lambda token: USERS[token]
jellyfin.credential_rejected = lambda force=True: False
media._registry = {media.MUSIC: media.Medium(media.MUSIC, "Music", buskarr.UNITS, 3, ("music",))}
media._registry_built_at = time.monotonic()
media._registry_settled = True
media.owned = lambda *args, **kwargs: jellyfin.Owned()
wants.states = lambda user, **_: []
imports._library_for = lambda *args: None
client = TestClient(main.app, follow_redirects=False)

def sign_in(user):
    client.cookies.set(sessions.COOKIE_NAME, sessions.issue(user.id, user.id))

def hit(n):
    return buskarr._result(dict(title=f"Chosen Song {n}", artist="Example Artist",
                                source="deezer", ref=str(n), duration=240), "track")

def seed(name, owner=OWNER, count=4, state=imports.READY, playlist=None):
    payload = dict(filename="original.csv", medium="music", unit="track", duplicates=0,
                   blanks=0, headings=["Track", "Artist"], has_source=True)
    if playlist:
        payload['playlist'] = dict(name="Test playlist", id=playlist)
    store.put_import(name, owner.key, "music", state, count, payload,
                     source=b'Track,Artist\r\n"Original",Example Artist\r\n')
    for n in range(2, count + 2):
        row = dict(line=n, source_row=n, line_start=n, title=f"Original Song {n}",
                    artist="Example Artist", year="", label=f"Original Song {n} by Example Artist",
                    state=imports.UNCERTAIN, hit=hit("suggested-" + str(n)), detail="", others=0)
        store.put_import_row(name, row)

acquired = []
def accept(unit, chosen, by, bulk=False):
    acquired.append(dict(title=chosen["title"], bulk=bulk))
    return arr.AddResult(True, "Accepted.", "job:" + chosen["title"], chosen["title"])
buskarr.add = accept
buskarr.state = lambda *args, **kwargs: None
searches = []
def search(query, *args, **kwargs):
    searches.append(query)
    return [hit("replacement")]
wants.search = search

def post(name, line, chosen, **overrides):
    data = dict(import_id=name, import_line=line, medium="music", unit="track",
                item_key=chosen["itemKey"], title=chosen["title"], artist=chosen["artist"],
                source=chosen.get("source", ""), ref=chosen.get("ref", ""))
    data.update(overrides)
    return client.post("/want", data=data)

sign_in(OWNER)
seed("corrections")
page = client.get("/import/corrections")
check.equal(page.status_code, 200, "the review renders")
check.that("Request selected tracks" in page.text and "Ignore selected tracks" in page.text,
           "the requested controls are present")
link = unescape(re.search(r'href="([^"]+)">Search for Original Song 2', page.text).group(1))
query = parse_qs(urlsplit(link).query)
check.equal((query["import_id"], query["import_line"]), (["corrections"], ["2"]),
            "even a single unsuitable result has a contextual search link")
check.that("Example Artist" in query["q"][0], "the search includes the source artist")
searched = client.get(link)
check.equal(searched.status_code, 200, "the correction search renders")
check.equal(acquired, [], "GET review and search never acquire anything")
form = re.search(r'<form method="post" action="/want">(.*?)</form>', searched.text, re.S).group(1)
fields = {unescape(k): unescape(v) for k, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form)}
check.equal((fields["import_id"], fields["import_line"]), ("corrections", "2"),
            "the actual result form preserves the association")
search_form = re.search(r'<form method="get" action="/"[^>]*>(.*?)</form>', searched.text, re.S).group(1)
check.that('name="import_id" value="corrections"' in search_form,
           "editing the search query preserves it too")
response = client.post("/want", data=fields)
check.equal(response.headers["location"].split("?")[0], "/import/corrections", "request returns to the same list")
check.equal((wants.allowance(OWNER, "music"), wants.import_allowance(OWNER, "music")), (3, 1),
            "the first correction spends only import allowance")
check.equal(acquired, [dict(title="Chosen Song replacement", bulk=True)], "the backend receives a bulk request")
batch = imports.get(OWNER, "corrections")
check.equal([r['line'] for r in imports.view(batch)['close']], [3, 4, 5], "only the corrected row leaves close matches")
check.equal(batch['rows'][0]['hit']['title'], 'Chosen Song replacement', "the selected replacement is persisted")
client.post("/want", data=fields)
check.equal(len(acquired), 1, "a stale result form cannot request its row twice")
for line in (3, 4, 5):
    post("corrections", line, hit(line))
batch = imports.get(OWNER, "corrections")
check.equal((len(acquired), store.queued_count(OWNER.key), wants.allowance(OWNER, "music")), (2, 2, 3),
            "four corrections use two available import slots and queue the rest without exhausting ordinary requests")
check.equal([json.loads(r['hit'])['title'] for r in store.queued(OWNER.key)],
            ['Chosen Song 4', 'Chosen Song 5'], "queued replacements keep source order and chosen identity")
check.equal([r['outcome'] for r in batch['rows']], ['asked', 'asked', 'waiting', 'waiting'],
            "queued corrections are resolved on the import report")
seed('existing', count=1)
post('existing', 2, hit('replacement'))
check.equal(store.import_rows('existing')[0]['outcome'], imports.ALREADY,
            'a correction already requested is resolved immediately even with a backlog')
check.equal(store.queued_count(OWNER.key), 2, 'it does not create another queue entry')

seed("ignore")
before = len(acquired)
client.post("/import/ignore/ignore", data={"line": ["2", "4"]})
batch = imports.get(OWNER, "ignore")
check.equal([r['line'] for r in imports.view(batch)['left']], [2, 4], "ignore changes only the selected rows")
check.equal([r['line'] for r in imports.view(batch)['close']], [3, 5], "other decisions remain available")
check.equal(len(acquired), before, "ignoring does not request anything")
client.post("/import/ignore/ignore", data={})
check.equal(imports.get(OWNER, "ignore")['state'], imports.READY, "an empty ignore is harmless")

sign_in(OTHER)
before = len(searches)
check.equal(client.get(link).status_code, 303, "another account cannot open a correction search")
check.equal(len(searches), before, "ownership is checked before searching")
check.equal(client.get('/import/ignore/source').status_code, 404, "another account cannot download the source")
client.post('/import/ignore/ignore', data={'line': '3'})
post('ignore', 3, hit('stolen'))
check.equal([r['line'] for r in imports.view(imports.get(OWNER, 'ignore'))['close']], [3, 5],
            "another account cannot ignore or replace rows")
check.equal(client.post('/api/v1/import/ignore/replace', headers={'X-Emby-Token': OTHER.id},
                       json={'line': 3, 'hit': hit('stolen')}).status_code, 404, "the API enforces the same owner")

sign_in(OWNER)
before = len(acquired)
post('ignore', 3, hit('wrong-kind'), unit='artist')
post('missing', 3, hit('missing'))
post('', 3, hit('no-batch'))
check.equal(len(acquired), before, "invalid contexts and different units never fall back to ordinary requests")
check.equal(store.import_rows('ignore')[1]['outcome'], '', "invalid candidates do not claim the row")
original = client.get('/import/ignore/source')
check.equal(original.content, b'Track,Artist\r\n"Original",Example Artist\r\n', "the original download preserves bytes")

sign_in(ADMIN)
seed('refused', ADMIN, count=1)
buskarr.add = lambda *args, **kwargs: arr.AddResult(False, 'Cannot fetch this recording.')
response = post('refused', 2, hit('refused'))
check.that('Cannot%20fetch' in response.headers['location'], "a failed selection explains why")
check.equal(store.import_rows('refused')[0]['outcome'], '', "a failed replacement remains available")
check.equal(store.import_rows('refused')[0]['hit']['title'], 'Chosen Song suggested-2', "failure restores the previous suggestion")
buskarr.add = accept
post('refused', 2, hit('retry'))
check.equal(store.import_rows('refused')[0]['outcome'], imports.ASKED, "retry can succeed")

seed('racing', ADMIN, count=1)
entered, release = threading.Event(), threading.Event()
def delayed(*args, **kwargs):
    entered.set()
    if not release.wait(5):
        raise AssertionError('race did not finish')
    return accept(*args, **kwargs)
buskarr.add = delayed
errors = []
def replace():
    try:
        imports.replace_row(ADMIN, 'racing', 2, hit('racing'))
    except Exception as exc:
        errors.append(str(exc))
worker = threading.Thread(target=replace)
worker.start()
check.that(entered.wait(5), 'a replacement has reached the backend')
imports.ignore_lines(ADMIN, 'racing', {2})
check.raises(wants.Denied, lambda: imports.replace_row(ADMIN, 'racing', 2, hit('second')),
             'a concurrent replacement cannot claim the same row')
release.set()
worker.join(5)
check.equal(errors, [], 'the claimed replacement completes')
check.equal(store.import_rows('racing')[0]['outcome'], imports.ASKED, 'ignore cannot overwrite an in-flight request')
buskarr.add = accept

placed = []
seed('owned', ADMIN, count=1, playlist='playlist-test')
imports._library_for = lambda *args: songs.LibraryIndex([
    {'Id': 'library-id', 'Name': 'Chosen Song owned', 'Artists': ['Example Artist']}])
playlists.add_found = lambda user, playlist_id, import_id, lines: placed.append((playlist_id, import_id, lines))
before = len(acquired)
post('owned', 2, hit('owned'))
check.equal(len(acquired), before, 'an alternative already in the library is not acquired again')
check.equal(placed, [('playlist-test', 'owned', [(2, 'library-id')])], 'the library alternative goes into its playlist at the source position')
imports._library_for = lambda *args: None
pending = []
playlists.wait_for = lambda *args, **kwargs: pending.append(args)
seed('pending-playlist', ADMIN, count=1, playlist='playlist-test')
post('pending-playlist', 2, hit('pending'))
check.equal(pending[0][2:6], ('pending-playlist', 2, 'Chosen Song pending', 'Example Artist'),
            'playlist waiting metadata uses the chosen replacement')

seed('api', ADMIN, count=2)
response = client.post('/api/v1/import/api/ignore', headers={'X-Emby-Token': ADMIN.id}, json={'lines': [2]})
check.equal(response.status_code, 200, 'the API can ignore selected rows')
response = client.post('/api/v1/import/api/replace', headers={'X-Emby-Token': ADMIN.id},
                       json={'line': 3, 'hit': hit('api')})
check.equal(response.status_code, 200, 'the API can make an import correction')
check.equal(store.import_rows('api')[1]['outcome'], imports.ASKED, 'the API uses the same source-row outcome')

# Legacy reviews retain the earlier request while allowing an explicit replacement.
seed('legacy-review', ADMIN, count=1, state=imports.DONE)
old = store.import_rows('legacy-review')[0]
old.update(state=imports.MATCHED, reviewNote='The earlier automatic selection needs review.')
store.put_import_row('legacy-review', old, imports.ASKED)
page = client.get('/import/legacy-review')
check.that('Earlier requests to review' in page.text, 'old automatic requests have an actionable review section')
link = unescape(re.search(r'href="([^"]+)">Search for Original Song 2', page.text).group(1))
check.equal(client.get(link).status_code, 200, 'a flagged earlier request can open its correction search')
post('legacy-review', 2, hit('legacy-correction'))
revised = store.import_rows('legacy-review')[0]
check.equal(revised['previousHit'], old['hit'], 'a legacy correction preserves the earlier selection for audit')
check.that(revised.get('manual') and not revised.get('reviewNote'), 'a completed correction clears only its review flag')
check.equal(imports.view(imports.get(ADMIN, 'legacy-review'))['review'], [], 'the corrected row leaves earlier requests to review')

# Uploading preserves even the encoding, duplicate records and skipped Type fields.
source = 'Track name,Artist name,Type\r\nAlpha,Example Artist,track\r\nAlpha,Example Artist,track\r\nAn Album,Example Artist,album\r\n'.encode('utf-16')
wants.search = lambda *args, **kwargs: []
upload = client.post('/import', data={'medium': 'music', 'unit': 'track'},
                     files={'listing': ('encoded.csv', source, 'text/csv')})
where = upload.headers['location']
check.equal(client.get(where + '/source').content, source, 'real uploads keep the complete original bytes')
batch_id = where.rsplit('/', 1)[1]
deadline = time.monotonic() + 5
while imports.get(ADMIN, batch_id)['state'] == imports.READING and time.monotonic() < deadline:
    time.sleep(.01)
uploaded = imports.get(ADMIN, batch_id)
check.equal((uploaded['duplicates'], len(uploaded['skipped'])), (1, 1), 'the report explains duplicate and type filtering separately')
check.that('Row 2 of your file' in client.get(where).text, 'the missing-match section shows spreadsheet row numbers')
store.prune_imports(time.time() + 1)
check.equal(store.import_source(batch_id), None, 'the saved source expires with its import report')

harness.cleanup()
raise SystemExit(check.report())
