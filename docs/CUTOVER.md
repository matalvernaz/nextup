# The cutover runbook

This homelab's own move from two services to one. Not part of the
distribution: a fresh installation has nothing to migrate and should not read
this.

Written down before it was run, because half of it is irreversible and the
order is the only thing that makes it safe. **Nextread keeps serving its own
traffic until the very last phase.** Nothing about it is touched until the
merged service has been shown to answer the same questions with the same
answers.

## What is actually being changed

| | Before | After |
|---|---|---|
| Containers | `nextup` and `nextread` | `nextup` |
| Databases | two | one, `nextup.db` |
| Jellyfin keys | one each | nextup's |
| `nextup.thealvernaz.space` | request API + pages | unchanged |
| `nextread.thealvernaz.space` | audiobook API + pages | proxied to nextup with `/nextread` added |
| `<jellyfin>/nextup/api` | stripped, to nextup | unchanged |
| `<jellyfin>/nextread/api` | stripped, to nextread | **not stripped**, to nextup |

That last row is the one to get right. The merged service answers
`/nextread/api/v1/*` itself, so the prefix it used to have removed must now be
left on.

## Why the routers move last

Traefik takes its configuration from container labels, and two containers
carrying a router of the same name is a conflict with no good outcome. So the
`nextread*` routers stay on the nextread container for as long as it is
running, and move to nextup in the same step that stops it.

Which means the merged service is verified **on the Docker network directly**,
not through Traefik. That is the right way round anyway: it separates "does
the service answer correctly" from "does the proxy route to it".

## Phases

Each one ends with something checked. Nothing in phase D happens until
phase C has passed.

### A. Prepare, without touching anything live

1. Merge `feature/merge-nextread` to `master`, push, let CI pass.
2. **Back up both live databases**, with `integrity_check` on each copy.
   `nextup.db` and `nextread.db` alike: the migration writes to the first and
   only reads the second, but a backup of the file that is not being written
   to is what makes going back possible at all.
3. Confirm nextup's own Jellyfin API key can read the two book libraries.
   It has never needed to before, and a key that cannot would make every book
   read as not owned -- silently, because an empty library is a legitimate
   answer.

### B. Deploy the merged service alongside nextread

4. Add the book settings to `/opt/stacks/nextup/compose.yaml`:
   `LISTENARR_URL`, `LISTENARR_QUALITY_PROFILE_ID`, `BOOK_LIBRARY_IDS`,
   `BOOK_DAILY_CAP`, `PLAYLIST_NAME`, `AUDIBLE_REGIONS`. **No nextread
   routers yet.**
5. Push the merged tree to the stack and `docker compose up -d --build`.
6. Check: healthy, no restarts, `/api/v1/info` and `/nextup/api/v1/info` say
   `nextup`, `/nextread/api/v1/info` says `nextread`, and `app.doctor` reports
   four backends answering and four media offered.

### C. Migrate, and prove it

7. `python -m app.migrate_nextread /nextread.db` -- dry run first, which
   writes nothing, then `--apply`.
8. Prove it, and this is the gate:
   - every request row in nextread's ledger has a counterpart in nextup's,
     with the same title, authors, requested and fulfilled times;
   - dismissals match;
   - the **same account** gets the **same shelf** out of both services, asked
     of each container directly;
   - `/nextread/api/v1/*` on the merged service answers what the old one does.

### D. Stop nextread, and only now

9. `docker compose down` on the nextread stack. Not `--volumes`, and the
   stack directory stays where it is.
10. Move the `nextread*` routers onto the nextup stack, with the
    Jellyfin-origin one **no longer stripping** its prefix, and `up -d`.
11. Check through Traefik, from outside: both hostnames, both same-origin
    paths, the SSO challenge on the pages, and the API answering its own JSON
    401 rather than a redirect.

## Going back

Nothing is deleted. To reverse: `docker compose up -d` in
`/opt/stacks/nextread`, remove the `nextread*` routers from the nextup stack,
`up -d`, and restore `nextup.db` from the backup taken in step 2 if the
migration itself is what went wrong. The old ledger is untouched throughout --
the migration opens it read-only.
