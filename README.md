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

## Notifications (ntfy)

`poll --notify-ntfy <topic-or-url>` (or env `NTFY_URL`) pushes to an
[ntfy.sh](https://ntfy.sh) topic when a cabin frees up. Subscribe to the same
topic in the ntfy phone app (or open `https://ntfy.sh/<topic>` in a browser) to
receive it. A low-priority startup ping is sent so you can confirm the channel.
It notifies on first appearance and whenever the available set changes — not
every interval.

## Deploy as an always-on watcher (Fly.io)

Runs `poll` 24/7 in a tiny container that mints tokens headlessly and pushes to
ntfy. Trip params live in `fly.toml` (`[env]`); the ntfy target is a secret.

```bash
fly auth login
fly launch --no-deploy --copy-config          # creates the app from fly.toml (rename if the name clashes)
fly secrets set NTFY_URL=<your-ntfy-topic>     # e.g. bluestar-cabins-xxxx
fly deploy
fly logs                                       # watch it run
```

To change the watched trip later: edit `[env]` in `fly.toml` and `fly deploy`
(or `fly secrets set BSF_DEPART=... BSF_RETURN=...`). It's a worker with no
inbound HTTP service, sized at 1 GB so Chromium has headroom while minting.

## Files

- `bluestar_cli.py` — single entrypoint: CLI (`check` / `poll`), HTTP polling,
  parsing, caching, ntfy notify, and the Playwright token minter. All deps in the
  PEP 723 header.
- `Dockerfile`, `fly.toml`, `.dockerignore` — always-on deployment.
- `test_bluestar_cli.py` — unit tests.

## Tests

```bash
uv run --with pytest --with responses --with requests --with click --with playwright \
    pytest test_bluestar_cli.py -q
```
