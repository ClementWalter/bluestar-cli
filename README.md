# Blue Star Ferries cabin watcher

Checks and watches **cabin availability** on any Blue Star Ferries route/date, and
alerts the moment a cabin frees up (e.g. someone cancels) on a sold-out sailing.

Route, dates, and passengers are all command-line flags — nothing is hard-coded.

## Setup (one-time)

```bash
uv run --with playwright playwright install chromium
```

## Usage

```bash
# One-shot snapshot
./bluestar_cli.py check --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28

# One-way, different route
./bluestar_cli.py check --from Piraeus --to Naxos --depart 2026-07-12

# Watch: poll every 5 min, ring the terminal bell when a cabin appears
./bluestar_cli.py poll --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28 --interval 300

# Background watcher to a log file
./bluestar_cli.py poll --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28 \
    --interval 300 > ~/bluestar.log 2>&1 &
```

Flags: `--from`, `--to` (port names), `--depart`, `--return` (YYYY-MM-DD; omit
`--return` for one-way), `--adults` (default 1), `--headful` (show the browser
while minting, for debugging), plus `--interval` / `--max-polls` on `poll`.

`uv` runs it with deps auto-installed (PEP 723 inline metadata).

## How it works

The site is a Vue SPA over an undocumented JSON booking engine. Cabin
availability lives in the `GetItineraries` response:

```
POST https://www.bluestarferries.com/en-gb/reservationapi/GetItineraries/{api_id}
Headers: X-Requested-With: XMLHttpRequest
         Content-Type: application/x-www-form-urlencoded   # body is still JSON
Body:    {"state": "<br token>", "timetables": [
           {"departurePort":"GR:PIR","arrivalPort":"GR:PMS","departureDate":"2026-08-08"}, ...]}

data[].trips[].availabilitySummary.cabinsAccommodation[] = {
  "description": "2 bed cabin",
  "count": 0,        // units left — 0 = sold out, >0 = bookable  ← the signal we watch
  "status": 0,       // 0 sold out · 1 available · 2 limited
  "price": "&euro;114.50", ...
}
```

### Why a headless browser is involved

`api_id` is rendered per page-load and `state`/`br` is base64 of
`apiId|pax|veh|pets|isReturn|leg…` **prefixed with a signature computed
client-side and bound to the exact route/dates** (corrupting the signature or
changing the payload returns empty data). The signing routine lives in the
minified Vue bundle and cannot be reproduced offline, so:

1. The tool reproduces the search in a **headless browser** (Playwright) to mint
   a valid `(state, api_id)` for the requested trip.
2. The token is **cached** under `~/.cache/bluestar/` (keyed by the search).
3. It then polls `GetItineraries` over **plain HTTP** (no cookies needed); on
   expiry it transparently re-mints.

## Files

- `bluestar_cli.py` — single entrypoint: CLI (`check` / `poll`), HTTP polling,
  parsing, caching, and the Playwright token minter. All deps in the PEP 723 header.
- `test_bluestar_cli.py` — unit tests.

## Tests

```bash
uv run --with pytest --with responses --with requests --with click --with playwright \
    pytest test_bluestar_cli.py -q
```
