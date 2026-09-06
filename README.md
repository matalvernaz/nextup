# nextup

Ask a Jellyfin server for a film, a series, some music or a book — and be told
what to play next from what it already holds.

Nextup is one small companion service between a Jellyfin server and the media
tools a household already runs. It does not keep a second library, it is not a
media server, and it has no accounts of its own: everybody signs in as
themselves, with the Jellyfin username and password they already use.

## Getting it running

You need Docker and a Jellyfin server. Nothing else is required to start.

```sh
curl -O https://raw.githubusercontent.com/matalvernaz/nextup/master/compose.yaml
docker compose up -d
```

Open `http://<that machine>:8099` and it will ask you two things:

1. **Where Jellyfin is, and a Jellyfin administrator's username and password.**
   Nextup asks Jellyfin for a credential of its own and keeps that. You do not
   need to create an API key, and your password is not stored.
2. **Which of Radarr, Sonarr, Listenarr and buskarr you run** — a URL and a
   key each, with a Test button. Once one answers, its quality profiles are
   listed *from that server*, so there is no number to go and find. Connect
   none of them and Nextup still recommends what to play next from what
   Jellyfin already holds.

That is the whole of it. Everybody else in the household signs in with their
own Jellyfin username and password; Nextup has no accounts of its own.

Put HTTPS in front of it before letting it out of your own network. Signing in
sends a Jellyfin password, and over plain HTTP that crosses the network as
typed — the page says so when it is not encrypted.

### Discover

`/discover` is what to play next, ranked, for each medium this server can rank.
Every row says why it is there.

- **Films** and **Series** need only a Jellyfin library of that kind. One shelf
  each, of what the library already holds and nobody has started, ranked from
  your own playback, favourites and ratings. No Radarr or Sonarr required —
  these are recommendations about what is already there.
- **Books** need Listenarr connected and a books library. Two shelves: what to
  read next from what the library holds, and what to add that it does not. The
  first is also kept in Jellyfin as a reading list, so you can pick one up from
  any Jellyfin app rather than only from this one — refreshed on a schedule,
  not when somebody happens to visit.

Music is the one medium with no recommendations; there it is search and request
only.

A shelf is never built while you wait. The first film shelf on a large library
is around twelve seconds of Jellyfin, so the page says it is working on it and
has the shelf on the next load. After that it is instant for an hour, and
`Work out new recommendations` starts a fresh one.

### Versions

That compose file pins `ghcr.io/matalvernaz/nextup:0.1`, so `docker compose
pull` brings you fixes within the 0.1 series and nothing that would change how
your deployment behaves. Move the line to `:0.2` when you want the next one.
`:latest` is whatever master last built, which is the right thing to run only
if you are following development.

Images are published for `linux/amd64` and `linux/arm64`, because a homelab is
as likely to be a small ARM box as an x86 server.

### Configuring it from a file instead

Every setting on those pages is also an environment variable, and **the
environment wins**: a value in a compose file is a statement about a
deployment, so the page shows it, says it is held there, and does not offer to
change it. `.env.example` lists them all with the reasoning attached. Nothing
in it is required — it is for people who would rather describe a deployment
than click through one.

### When something is not working

```sh
docker compose exec nextup python -m app.doctor
```

Nearly every way this can be misconfigured has no symptom otherwise. A quality
profile left unset disables a whole medium on a container that starts and
reports healthy. A Radarr on the wrong port produces a working search box that
finds nothing. `app.doctor` says all of it in one screen, names the variable or
the address in each case, and exits non-zero.

```
Backends
  radarr: configured but not answering. http://localhost:7878/api/v3/system/status
  could not be reached (ConnectError). Inside Docker, 'localhost' is this
  container rather than the host.
  sonarr: not configured: SONARR_QUALITY_PROFILE_ID unset. Series will not be offered.
  listenarr: answered.

Media offered
  Books: 2 library(ies), units book, series, 3 per account per day.
```

### Stock Jellyfin, and the audiobook fork

Films, television and music work on a stock Jellyfin server.

**Books need the audiobook fork of Jellyfin**, which files a whole audiobook as
a single `AudioBook` item. Stock Jellyfin has no such type: its Books libraries
hold ebooks, and audiobooks on a stock server are music albums.

Nextup checks and tells you, on the Backends page and in `app.doctor`, but it
does not refuse the medium on the strength of that check. It cannot: the same
question that distinguishes a stock server from the fork also cannot
distinguish the fork from a fork whose books library is simply empty, which is
every fresh installation. Refusing there would make the first request
impossible. So the check reports, and connecting Listenarr to a stock Jellyfin
gives you a search box that can ask for a book the library will never show
arriving.

## What it does

- **Recommends** what to play next from what the server already holds — films,
  television and audiobooks — from your own playback, favourites and ratings.
  No second catalogue account and no API key of its own.
- **Searches** the catalogues the tools already proxy: TMDB through Radarr,
  TVDB through Sonarr, Audible through Listenarr, Deezer and MusicBrainz
  through buskarr.
- **Requests** one thing, with a per-medium daily allowance for accounts that
  are not Jellyfin administrators. Asking twice for something still on its way
  is free and spends nothing. Asking again for something that arrived and has
  since left the library is a new request, because otherwise a film deleted
  from Jellyfin could never be asked for again by whoever had it.
- **Reports** what became of each request: `on_its_way`, `still_looking` or
  `in_library`, with aired, queued and in-library episode counts for
  television.
- **Cancels** a request, calling the acquisition off — unless somebody else in
  the household is still waiting for the same thing.

## Four media, one interface

| Medium | Units | Backend | Recommends from |
|---|---|---|---|
| Films | movie | Radarr | your playback, favourites and ratings |
| Series | series | Sonarr | the same |
| Music | artist, album, track | buskarr | — |
| Books | book, series | Listenarr | an Audible similarity graph and a local text model |

One search box with one picker for what kind of thing you want, not four
screens. Asking for a whole book series is the same control as asking for a
music artist, with a different scope.

## Backends are detected, not declared

A medium is offered when three things are true, each checked separately so a
half-configured install can say which half:

1. Its backend has the settings it needs.
2. The backend answers. Configuration is a shape; reachability is a fact, and
   conflating them is what makes a wrong port look like an empty catalogue.
3. Jellyfin has a library of that kind.

`GET /api/v1/capabilities` publishes the result, so a client draws controls for
what is actually there and nothing at all for what is not. A backend that is
configured and briefly silent keeps its medium and says so, rather than
vanishing and taking somebody's list of outstanding requests with it.

Those three are what it takes to *ask* for something. A film or television
shelf on `/discover` needs only the third: it ranks what the library already
holds, so it is there whether or not an acquisition tool is.

## Signing in

The pages authenticate against Jellyfin (`/Users/AuthenticateByName`) and keep
the access token in a cookie this app signs. Your password is not stored, not
logged, and goes nowhere but Jellyfin.

The JSON API authenticates differently and never sees a password: it
introspects **the caller's own Jellyfin access token** against `GET /Users/Me`,
with no fallback. A service API key is rejected — it carries no user context,
so there would be nobody to charge the request to.

Where a forward-auth proxy is in front of the pages, its header wins. That path
is deliberately unreachable from the API, which is not behind the proxy;
`tests/test_api_auth.py` asserts a request carrying only the proxy header is
refused, under every prefix.

## Arrival is decided on provider ids

A film has arrived when Jellyfin holds an item whose TMDB id matches the one
Radarr was asked for. A whole-series request has arrived when Jellyfin holds
every episode Sonarr currently counts as aired — future episodes stay
monitored, and the request is not closed after its first episode.

Specials are on neither side of that comparison. Sonarr's aired total does not
include them, so the episodes counted here must not either: a series with a
special in the library would otherwise reach the total with real episodes still
missing, one free episode per special.

Books and music are the two exceptions, for the same underlying reason: their
identity does not survive the trip. An audiobook arrives tagged with whichever
ASIN the *other* marketplace issued for the same edition, so arrival is decided
on the title with an author to agree with it. Music reads its state from
buskarr, which placed the file and holds the exact `(artist, title, duration)`
it placed it under.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/info` | That this service is here. The only route needing no token |
| `GET` | `/api/v1/capabilities` | What this server serves, and this account's allowances |
| `GET` | `/api/v1/search?medium=&q=&unit=` | Catalogue hits, marked with what the library holds |
| `GET` | `/api/v1/requests?medium=` | This account's requests and their states |
| `GET` | `/api/v1/recommendations?medium=&libraryId=` | Unstarted items from this account's library, ranked |
| `POST` | `/api/v1/want` | Ask for one thing |
| `POST` | `/api/v1/cancel` | Take one back |

Served on three prefixes, because a native client derives its address rather
than being told it: `/api/v1` for an address typed in directly, `/nextup/api/v1`
for one derived from a Jellyfin origin, and `/nextread/api/v1` for the
audiobook protocol that shipped before these two services became one.

`capabilities` takes a `protocol`. Without one the answer is films, series and
music — exactly what shipped. `?protocol=2` adds books. An unknown protocol is
a 400 rather than the nearest shape, so a client newer than its server and a
client with a typo do not both look like a server with fewer media.

## Discovery, and the one thing with no symptom

A native client with nothing typed in its settings looks for this service at
the Jellyfin origin, under `/nextup`. That needs a proxy rule, and leaving it
out is the single easiest half of a deployment to forget: everything answers
correctly on Nextup's own address, the app probes the Jellyfin origin, gets
Jellyfin's own 404, concludes no such service is installed, and draws nothing
at all. There is no error anywhere.

```sh
curl -s https://your-jellyfin-host/nextup/api/v1/info
```

`{"service":"nextup","protocol":1,"protocols":[1,2]}` is right. A 404 means no
client will ever find it. Set `PUBLIC_URL` and Nextup runs that check itself, a
minute after start and hourly after, logging an error that names the address.
Comma-separate it to watch more than one prefix.
Never a health-check failure: your proxy would drop a container that is still
serving its own hostname perfectly well.

## Tests

```sh
./run-tests.sh
```

Each file is a standalone script needing only `fastapi` and `httpx`, so the
suite runs wherever the service runs. `tests/harness.py` **sets** `DB_PATH`
rather than defaulting it, and refuses to delete any file not named
`nextup-test-*`. That guard is not hypothetical: the audiobook service now
merged into this one had a suite that deferred to the surrounding
environment's database path, and inside the live container that was the live
database.

## Licence

MIT.
