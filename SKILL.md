---
name: bluestar-cli
description: Check and watch Blue Star Ferries cabin (berth) availability for a route and dates, and detect when a sold-out cabin frees up (e.g. a cancellation). Use when asked to check ferry cabin availability, monitor a fully-booked Blue Star Ferries sailing, or poll for cabins to open up. Greek island ferries (Piraeus, Patmos, Naxos, Santorini, Crete, etc.).
---

# Blue Star Ferries cabin watcher

`bluestar` queries Blue Star Ferries' booking engine for per-sailing cabin
availability and can poll until a cabin frees up. Route, dates, and passenger
count are all CLI flags.

- "Are there cabins on the Piraeus→Patmos ferry on 8 August?"
- "Watch this sold-out sailing and tell me when a cabin opens up."
- Any Blue Star Ferries cabin/berth availability question for a specific trip.

Not for: airline seats, other ferry operators, or general schedules (this is
cabin inventory only, though seat/vehicle/pet counts are in the same response).

## How to invoke

`bluestar` is on `$PATH` once `uv run ~/Documents/claudine/scripts/link_clis.py`
has linked `bin/bluestar`; otherwise run `<skill-dir>/bin/bluestar`. It is a PEP
723 script run by `uv`, no venv needed. Once per machine, install the headless
browser used to mint tokens:

```bash
uv run --with playwright playwright install chromium
```

## Authentication

None. Each search mints a server-signed booking token in a headless browser (see
Gotchas); no account or cookie is used.

## Output conventions

- `check` prints the availability per leg on stdout, then one summary line;
  `--json` prints `{"available": [offers], "status": "<one-line status>"}`.
- `✅ <type> x<count> <price> [<vessel> <datetime>]` is a bookable cabin
  (`count > 0`); `❌ all cabins SOLD OUT` means every cabin type has
  `count == 0` on that leg.
- Progress, poll results and warnings go to stderr through logging.
- `check` exits 2 when availability could not be fetched (e.g. minting failed);
  `poll` logs the failure and keeps going.

## Commands

```bash
# Read
bluestar check --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28   # one-shot snapshot
bluestar check --from Piraeus --to Naxos --depart 2026-07-12 --adults 2 --json      # one-way, JSON
bluestar check --from Piraeus --to Patmos --depart 2026-08-08 --headful             # watch the minting browser

# Watch (runs until stopped; rings the terminal bell + logs when a cabin appears)
bluestar poll --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28 --interval 300
bluestar poll --from Piraeus --to Patmos --depart 2026-08-08 --notify-ntfy my-topic  # push via ntfy
```

Flags (both commands): `--from`, `--to` (port display names, e.g. `Piraeus`,
`Patmos`, `Naxos`), `--depart`, `--return` (YYYY-MM-DD; omit `--return` for
one-way), `--adults` (default 1), `--headful` (show the browser while minting —
use to debug a minting failure). `poll` adds `--interval` (seconds),
`--max-polls` (0 = forever), `--notify-ntfy` (env `NTFY_URL`) and
`--heartbeat-hours`. Every trip flag also reads a `BSF_*` env var (`BSF_FROM`,
`BSF_TO`, `BSF_DEPART`, `BSF_RETURN`, `BSF_ADULTS`, `BSF_INTERVAL`).

## Recipes

```bash
# Is any cabin bookable right now? (empty list = sold out)
bluestar check --from Piraeus --to Patmos --depart 2026-08-08 --json | jq '.available'

# Watch a sold-out round trip in the background, logging to a file
bluestar poll --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28 \
    --interval 300 2> ~/bluestar.log &
```

To actually book, open the site/app — this tool only reads availability.

## Gotchas

The booking engine endpoint (`POST /en-gb/reservationapi/GetItineraries/{api_id}`)
needs a server-signed `state` token bound to the exact route/dates; the signature
is computed in the site's minified JS and can't be forged. So the tool:

1. Mints `(state, api_id)` by reproducing the search in a **headless browser**
   (Playwright) — this is the slow step (~10–15s), done once per distinct search.
2. Caches the token under `~/.cache/bluestar/` (keyed by from/to/dates/adults).
3. Polls `GetItineraries` over plain HTTP (no cookies); re-mints automatically if
   the token expires.

- **Minting times out / wrong result**: re-run with `--headful` to watch the
  browser; a `mint_failure.png` screenshot is written on Playwright timeouts.
  Causes are usually a changed cookie banner, a port name that doesn't match a
  dropdown option, or a date with no sailing (greyed in the calendar).
- **Use exact port display names** as they appear on bluestarferries.com (the
  tool types them into the site's search box and clicks the matching option).
- **Stale results**: delete the cached token in `~/.cache/bluestar/` to force a
  fresh mint, or just wait — `poll` re-mints on expiry.
- **A route/date returns no sailings**: that day may have no crossing; pick a day
  that sails (the site greys out non-sailing days).

## Tests

```bash
uv run --script tests/test_bluestar_cli.py
```
