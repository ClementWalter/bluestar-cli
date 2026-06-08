---
name: bluestar-cabins
description: Check and watch Blue Star Ferries cabin (berth) availability for a route and dates, and detect when a sold-out cabin frees up (e.g. a cancellation). Use when asked to check ferry cabin availability, monitor a fully-booked Blue Star Ferries sailing, or poll for cabins to open up. Greek island ferries (Piraeus, Patmos, Naxos, Santorini, Crete, etc.).
---

# Blue Star Ferries cabin watcher

`bluestar_cli.py` queries Blue Star Ferries' booking engine for per-sailing cabin
availability and can poll until a cabin frees up. Route, dates, and passenger
count are all CLI flags.

## When to use this

- "Are there cabins on the Piraeus→Patmos ferry on 8 August?"
- "Watch this sold-out sailing and tell me when a cabin opens up."
- Any Blue Star Ferries cabin/berth availability question for a specific trip.

Not for: airline seats, other ferry operators, or general schedules (this is
cabin inventory only, though seat/vehicle/pet counts are in the same response).

## Setup (once per machine)

```bash
uv run --with playwright playwright install chromium
```

The script is `uv`-run with deps in its PEP 723 header — no venv needed.

## Commands

One-shot snapshot:
```bash
./bluestar_cli.py check --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28
```

Watch on an interval (rings the terminal bell + logs when a cabin appears):
```bash
./bluestar_cli.py poll --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28 --interval 300
```

Flags (both commands): `--from`, `--to` (port display names, e.g. `Piraeus`,
`Patmos`, `Naxos`), `--depart`, `--return` (YYYY-MM-DD; omit `--return` for
one-way), `--adults` (default 1), `--headful` (show the browser while minting —
use to debug a minting failure). `poll` adds `--interval` (seconds) and
`--max-polls` (0 = forever).

## Interpreting output

- `❌ all cabins SOLD OUT` — every cabin type has `count == 0` on that leg.
- `✅ <type> x<count> <price> [<vessel> <datetime>]` — a bookable cabin
  (`count > 0`). `poll` keeps reporting "still sold out" until one appears.
- `check` exits non-zero (2) if availability could not be fetched (e.g. minting
  failed); `poll` logs the failure and keeps going.

To actually book, open the site/app — this tool only reads availability.

## How it works (so you can reason about failures)

The booking engine endpoint (`POST /en-gb/reservationapi/GetItineraries/{api_id}`)
needs a server-signed `state` token bound to the exact route/dates; the signature
is computed in the site's minified JS and can't be forged. So the tool:

1. Mints `(state, api_id)` by reproducing the search in a **headless browser**
   (Playwright) — this is the slow step (~10–15s), done once per distinct search.
2. Caches the token under `~/.cache/bluestar/` (keyed by from/to/dates/adults).
3. Polls `GetItineraries` over plain HTTP (no cookies); re-mints automatically if
   the token expires.

### Troubleshooting

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
uv run --with pytest --with responses --with requests --with click --with playwright \
    pytest test_bluestar_cli.py -q
```
