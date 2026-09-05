# Merging nextread into nextup

One service, one image, one database, one web interface, four media.

## Why

Nextread and Nextup were split so a new installation would not have to stand
up an audiobook recommender before it could ask for a film. The Companion
distribution answered that a different way -- one `docker compose up` for
both -- and in doing so removed the reason for the split. What is left of the
separation is two configuration files, two databases, two API protocols and,
worst of all, two web interfaces that would each have grown their own search
page.

They are also the same program. Identical dependencies, and both are
`config.py` / `store.py` / `jellyfin.py` / `wants.py` / `api.py` / `main.py`.
The moves are still rewrites, though a mechanical kind: a file landing in
`app/books/` has `from . import audible, config, store` today and needs
`from .. import config, store` and `from . import audible`, because otherwise
`config` resolves to a `app.books.config` that does not exist.

## The shape after the merge

```
app/
  config.py      every backend, every allowance, one file
  store.py       one SQLite file; requests keyed (user_key, medium, item_key)
  jellyfin.py    one client: token introspection, libraries, owned index, playlists
  media.py       the registry -- which media this deployment can actually serve
  arr.py radarr.py sonarr.py buskarr.py listenarr.py    the backends
  books/         audible.py textmodel.py engine.py shelves.py series.py search.py
  recommendations.py   movie-owned-v1, series-owned-v1, and the book shelves
  wants.py       one request path, one ledger, per-medium adapters
  api.py         the unified JSON API
  compat_nextread.py   the old audiobook protocol, translated
  main.py        the web pages: one search box, one medium picker
```

`books/` is a subpackage because the audiobook engine is genuinely
book-specific -- an Audible similarity graph and a TF-IDF taste model with no
analogue in film or television. The backends stay flat beside the ones that
are already there; moving them would be churn without a reason.

## Four media, one ledger

Nextup's ledger is already keyed `(user_key, medium, item_key)`. Nextread's
was `(user_key, asin)`. A book row is `(user_key, 'book', asin)` -- the old key
is the new key with the medium left out, so the migration is an INSERT
... SELECT rather than a reshape.

A book has two units, the way music has three:

| Medium | Units | Backend |
|---|---|---|
| Films | movie | Radarr |
| Series | series | Sonarr |
| Music | artist, album, track | buskarr |
| Books | book, series | Listenarr |

Asking for a book series is then the same control as asking for a music
artist, on machinery that already exists, rather than a second page.

## Backends are detected, not declared

A medium is offered when three things are true, and each is checked
separately so a half-configured install says which half:

1. Its backend has the settings it needs (`configured`).
2. The backend answers (`reachable`) -- a new check. Configuration is shape;
   reachability is fact, and until now an unreachable Radarr was discovered
   only per-search, as a warning nobody watching a first install reads.
3. Jellyfin has a library of that kind.

`GET /api/v1/capabilities` publishes the result, so a client draws controls
for what is actually there. `python -m app.doctor` prints the same three
answers per backend, by environment-variable name, before anything starts.

## Keeping shipped clients working

EchoFin derives its base URL as `override ?? companion + "/nextup"
?? jellyfinOrigin + "/nextup"`, then appends `/api/v1/...`. So one process
answers on three prefixes:

| Prefix | Shape | For |
|---|---|---|
| `/api/v1/*` | the request/recommendation API | a direct address, and the web UI's own host |
| `/nextup/api/v1/*` | the same | a Companion or same-origin address |
| `/nextread/api/v1/*` | the old audiobook protocol | every EchoFin build already in the field |

`/info` answers with the service name the caller asked under, because both
EchoFin and `selfcheck.py` check that name to tell "not installed" from
"broken".

`compat_nextread.py` is a translation over the merged code, not a second
implementation. It is retired when the last build that needs it is gone.

An existing separate `nextread.thealvernaz.space` router keeps working by
adding the `/nextread` prefix at the proxy rather than in the app.

### Books are behind a protocol opt-in, and the reason is a real regression

`GET /api/v1/capabilities` lists `book` **only** when the caller asks for
protocol 2 (`?protocol=2`). Without it the answer is exactly today's three
media, on every prefix.

This is not caution about decoding. `Capabilities.Medium.medium` is a plain
`String` in the shipped client, so a fourth medium decodes cleanly. The
problem is one layer up: `LibraryMenuView.resolveRows` appends a
recommendation row when Nextread covers a library **and** an ask row when
Nextup names it, and a books library would then carry both -- two rows for the
one feature, from the one server, which the client can only tell apart by
renaming them. The merge exists to stop that kind of confusion, not to ship it
to builds already in the field.

`/info` gains `"protocols": [1, 2]` alongside its existing `"protocol": 1`.
The shipped client decodes `protocol` and ignores unknown keys
(`NextupService.Presence`, `CodingKeys.version = "protocol"`), so the
advertisement costs nothing and a future build can negotiate up.

## The web interface

`templates/index.html` already renders one search form with a
`<select name="medium">` populated from the registry, and per-medium unit
radios that appear only where a medium has more than one unit. Books become a
fourth option in that select. Nextread's `search.html` is the one-medium
special case of the page that already exists, so it goes.

Identity for those pages was a forward-auth proxy header, which meant an
installation without oauth2-proxy rendered a 403 dead end -- and the Companion
deliberately exposed no pages at all, so as published the package had no
interface a stranger could reach. The pages now sign in against Jellyfin
itself (`POST /Users/AuthenticateByName`), keep the returned access token in a
signed cookie, and resolve identity through the same `user_from_token` path
the API already uses. The proxy header remains supported and takes precedence
where it is present.

Three details are decided rather than left to the implementation:

- **The signing secret is generated on first boot** and kept in `meta`. Not an
  environment variable: a stranger should not have to mint a secret to sign
  in, and one they are forced to invent will be `changeme`.
- **Plain HTTP is allowed.** A first install reached at
  `http://server:8097` over a home network is the case this whole slice
  exists for. The cookie carries `Secure` only when the request arrived over
  HTTPS -- `X-Forwarded-Proto` where a proxy set it -- and the form says
  plainly that the connection is not encrypted when it is not.
- **One device id per installation**, kept beside the secret, so
  `X-Emby-Authorization` does not mint a fresh Jellyfin device row per
  sign-in.

## Where this has got to

Slices 1 to 8 are done and the suite is green at 23 files. Verified by running
the merged service: all three prefixes answer with the right service name, the
sign-in page renders with its unencrypted-connection warning, a proxy header
alone is refused on every prefix, and `/healthz` stays healthy while Jellyfin
is unreachable rather than restart-looping.

Slice 9, this homelab's own cutover, is deliberately not done. It stops a live
stack and migrates two live databases, so it waits for a decision rather than
happening at the end of a coding session.

Two things are knowingly left for later, neither blocking:

- **The book recommendation shelf has no page yet.** Books search, request and
  report through the unified page; the own/discover shelves are still served
  only over `/nextread/api/v1/shelves`, which is what EchoFin reads. The web
  page for them is the next piece of UI, not a merge step.
- **Four of the audiobook suite's fifteen files are still unported**:
  `test_api_auth`, `test_multiuser`, `test_selfcheck`, `test_template`. The
  first two need the merged page and API surface they now have, so they can
  come across; the last two duplicate checks this repo already makes.

## Order of work

Each slice leaves the suite green and the service runnable.

1. **Merge the store.** One schema, both sets of tables, requests keyed by
   medium. Migration for an existing nextread database.
2. **Merge config and Jellyfin.** One `config.py`; one `jellyfin.py` with the
   book reads and playlist writes folded in.
3. **Move the book engine** into `app/books/`, on the merged store and config.
4. **Books as a medium.** `listenarr.configured`, the registry entry, the two
   units, the request path through `wants.py`.
5. **Backend reachability**, in the registry, in `/capabilities`, and in
   `python -m app.doctor`.
6. **The unified API**, plus `compat_nextread.py` and the prefix mounts.
7. **The web pages**: native sign-in, one search page, book shelves.
8. **Distribution**: one `compose.yaml` a stranger can run, a `v0` tag, the
   README rewritten around a first install rather than around this homelab.
9. **This homelab's own cutover**, which is not part of the distribution and
   is written down so it does not get done ad hoc: stop the nextread stack,
   back up both databases, migrate, add a `/nextread`-prefix router for the
   old hostname, update `catalog.json`, retire the Companion stack, and leave
   the Companion repository archived with a README pointing here.

Slices 1 to 3 land together or not at all, and **nextread's seventeen test
files come with them**. They are the regression protection for the 4,400 lines
being moved -- `test_scoring.py`, `test_series_want.py`, `test_asin_identity.py`
and the rest -- and a merge that leaves them behind is a rewrite pretending to
be a move. They are ported onto this repository's harness, whose deletion
guard knows only `nextup-test-*`; nextread's harness is not carried across.

Within those slices the book code keeps calling `record_request(user_key,
asin, ...)` and its siblings through thin shims in `app/books/` that supply
`medium='book'`. Rewriting several hundred call sites across `wants.py`,
`series.py`, `engine.py` and `shelves.py` in the same slice that moves them
would make a failure impossible to attribute.
