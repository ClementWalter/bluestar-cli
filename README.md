# Blue Star Ferries cabin watcher

Polls Blue Star Ferries for **cabin availability** on a fully-booked sailing and
alerts the moment a cabin frees up (e.g. someone cancels).

Default search baked in: **Piraeus → Patmos 8 Aug 2026, return Patmos → Piraeus
28 Aug 2026, 1 passenger**. Currently every cabin on both legs is sold out.

## Usage

```bash
./bluestar_cabins.py check                 # one-shot availability snapshot
./bluestar_cabins.py poll --interval 120   # loop; rings terminal bell on a free cabin
./bluestar_cabins.py poll --interval 300 > watch.log 2>&1 &   # background watcher
./bluestar_cabins.py refresh               # how to capture a fresh token
```

`uv` runs it with deps auto-installed (PEP 723 inline metadata) — no setup.

### Other routes / dates

The search is encoded inside a server-**signed** `state` token, so you can't
change route/dates with a flag — you capture a new token for the new search:

1. Search the route/dates/pax on the site and click **SEARCH**.
2. From the results URL `…/en-gb/booking?br=XXXX`, copy the value after `br=` → `--state`.
3. In that page's source find `reservationEndpoint: "/en-gb/reservationapi/{0}/YYYY"` → `--api-id`.
4. `./bluestar_cabins.py check --state XXXX --api-id YYYY`

To watch a different default permanently, also edit `DEFAULT_*` in
`bluestar_cabins.py` and its `DEFAULT_TIMETABLES`.

## How it works (reverse-engineered API)

The booking site is a SPA over an undocumented JSON booking engine. All calls go to:

```
POST https://www.bluestarferries.com/en-gb/reservationapi/{Method}/{api_id}
Headers: X-Requested-With: XMLHttpRequest
         Content-Type: application/x-www-form-urlencoded   # body is still JSON
Body:    {"state": "<br token>", "timetables": [
           {"departurePort":"GR:PIR","arrivalPort":"GR:PMS","departureDate":"2026-08-08"},
           {"departurePort":"GR:PMS","arrivalPort":"GR:PIR","departureDate":"2026-08-28"}]}
```

Relevant methods: `GetItineraries` (sailings **+ cabin availability**, the one we
poll), `GetRouteFrequency` (which days sail), `GetRouteBestPrices`.

`GetItineraries` returns, per sailing, the cabin inventory we care about:

```jsonc
data[].trips[].availabilitySummary.cabinsAccommodation[] = {
  "description": "2 bed cabin",
  "count": 0,            // units left — 0 = sold out, >0 = bookable
  "status": 0,           // 0 sold out · 1 available · 2 limited
  "price": "&euro;114.50",
  "isBerth": true, "isExternal": false, ...
}
```

A sold-out route has every `count == 0`; a cancellation flips one to `count > 0`,
which is exactly what `poll` watches for.

### Auth model

- `api_id` (e.g. `8DEC4F36C5F91C0`) is rendered into the page HTML
  (`reservationEndpoint`) and identifies the booking session.
- `state` / `br` is base64 of `apiId|pax|vehicles|pets|isReturn|leg…` **prefixed
  with a ~21-byte signature** computed client-side. The signature is validated
  server-side (corrupting it returns empty data), so the token cannot be forged
  offline and must be captured from a real search.
- **No cookies required** — the signed token alone authenticates the request.
- A stale token makes the endpoint reply with HTML or empty `data`; the tool
  detects this and tells you to re-capture (`refresh`).

## Tests

```bash
uv run --with pytest --with responses --with requests --with click pytest test_bluestar_cabins.py -q
```
