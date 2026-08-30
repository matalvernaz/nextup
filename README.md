# nextup

Ask a Jellyfin server for a film, a series or some music — from a phone, with
no second account.

Nextup is a small companion service that sits between a Jellyfin client and
whatever acquires media for that server. It holds the request list, the daily
allowances, and the answer to "has it arrived yet?". It does not recommend
anything, it does not keep a library, and it is not a media server.

## Why it exists

Radarr and Sonarr authenticate with a single API key that can delete files.
buskarr has no authentication of its own at all. None of the three can safely
be reached from a phone, so something server-side has to stand in front of
them — and once something does, it may as well be the piece that knows who is
asking and how much they have asked for today.

## What it does

- **Search** the catalogues those tools already proxy — TMDB through Radarr,
  TVDB through Sonarr, Deezer/MusicBrainz/Apple through buskarr. No metadata
  account and no second API key.
- **Request** one thing, with a per-medium daily allowance for accounts that
  are not Jellyfin administrators.
- **Report** what became of each request: `on_its_way`, `still_looking`, or
  `in_library`.
- **Cancel** a request, which calls the acquisition off — unless somebody else
  in the household is still waiting for the same thing.

## Authentication

The JSON API authenticates on **the caller's own Jellyfin access token**,
introspected against `GET /Users/Me`. There is no second account, no second
sign-in, and no credential of yours that Nextup stores. A service API key is
rejected: it carries no user context, so there would be nobody to charge the
request to.

The browser pages authenticate differently — they trust a forward-auth proxy's
username header, with a configurable fallback for direct access. **That
fallback is deliberately unreachable from the API**, which is not behind the
proxy. `tests/test_api_auth.py` asserts that a request carrying only the proxy
header is refused.

## Arrival is decided on provider ids

A film has arrived when Jellyfin holds an item whose TMDB id matches the one
Radarr was asked for. A series has arrived when Jellyfin holds it by TVDB id
**and has at least one episode of it** — Sonarr creates the series row the
moment it is added, so counting that as an arrival would close the request
before anything downloaded.

Music is the exception, and reads its state from buskarr instead. buskarr
placed the file and holds the exact `(artist, title, duration)` identity it
placed it under; the only route from a track back to a Jellyfin item is
matching text, which is the near-miss this service otherwise avoids.

## Media are independently optional

A medium is offered when its backend is configured and Jellyfin has a library
of that kind. Configure only `RADARR_*` and Nextup serves films and says
nothing whatever about series or music — `GET /api/v1/capabilities` simply
does not list them, and a client shows no control for what is not listed.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/capabilities` | What this server serves, and this account's allowances |
| `GET` | `/api/v1/search?medium=&q=&unit=` | Catalogue hits, marked with what the library holds |
| `GET` | `/api/v1/requests?medium=` | This account's requests and their states |
| `POST` | `/api/v1/want` | Ask for one thing |
| `POST` | `/api/v1/cancel` | Take one back |

Browser pages live at `/`. They are server-rendered HTML with real forms and
no JavaScript in any path that does something — a screen reader is the primary
interface here.

## Deploying

```
docker pull ghcr.io/matalvernaz/nextup:latest
```

Built for `linux/amd64` and `linux/arm64`, because a homelab is as likely to be
a small ARM box as an x86 server.

Copy `compose.example.yaml` to `compose.yaml`, fill in the backends you have,
and delete the rest. Three Traefik notes are in the example and all three are
load-bearing:

- The API router must **not** sit behind the sign-in proxy. A native client
  cannot complete an oauth2 flow.
- It needs an explicit priority higher than the page router's, because
  Traefik's default priority is the rule's character length.
- **The same API must also be served at the Jellyfin origin under `/nextup`.**
  That is where a client looks when nothing is typed in its settings, and it is
  the single easiest thing to leave out: everything answers correctly on
  Nextup's own host, the app finds nothing, and the feature is simply absent
  with no error anywhere. Check it with

  ```
  curl -i https://your-jellyfin-host/nextup/api/v1/capabilities
  ```

  A 401 is right — that is Nextup asking for a token. A 404 means the router is
  missing and no client will ever find the service.

## Tests

```
python3 tests/test_api_auth.py
python3 tests/test_wants.py
python3 tests/test_store.py
```

They need only `fastapi` and `httpx`, so they run wherever the service runs.
`tests/harness.py` **sets** `DB_PATH` rather than defaulting it, and refuses to
delete any file not named `nextup-test-*`. That guard is not hypothetical: the
service this one is modelled on had a suite that deferred to the surrounding
environment's database path, and inside the live container that was the live
database.

## Licence

MIT.
