#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "click"]
# ///
"""Poll Blue Star Ferries cabin availability and alert when cabins free up.

Blue Star's booking engine exposes an undocumented JSON endpoint,
``POST /en-gb/reservationapi/GetItineraries/{api_id}``, that returns, per
sailing, an ``availabilitySummary.cabinsAccommodation`` list. Each entry carries
a ``count`` (units left) and a ``status`` (0 sold out, 1 available, 2 limited).
When a route is fully booked every cabin reports ``count == 0``; a cancellation
flips one to ``count > 0``. This tool replays that request on an interval so a
freed cabin can be caught the moment it reappears.

The request is authenticated by a signed, opaque ``state`` token (the ``br``
query-string value the website puts on the /booking URL) plus the matching
``api_id`` rendered into the booking page. Both are produced by performing a
search on the site; the signature is computed client-side and cannot be forged,
so the token must be captured from a real search (see ``--help`` of ``refresh``).
Cookies are not required — the token alone authenticates the call.
"""

import logging
import sys
import time

import click
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger("bluestar")

BASE_URL = "https://www.bluestarferries.com"
# Endpoint path is templated with the page-specific api_id; method name is fixed.
ITINERARIES_PATH = "/en-gb/reservationapi/GetItineraries/{api_id}"

# A signed search captured from the live site for Piraeus -> Patmos, 8 Aug ->
# 28 Aug 2026, 1 passenger. The token embeds the route/dates/pax, so it is only
# valid for exactly this search. Override with --state/--api-id (or `refresh`)
# for other searches or once this token expires server-side.
DEFAULT_API_ID = "8DEC4F36C5F91C0"
DEFAULT_STATE = (
    "Uu9qDaTowUpgPuw4_jFOXrjs6sZBOERFQzRGMzZDNUY5MUMwfDF8MHwwfFRydWV8"
    "R1I6UElSfEdSOlBNU3wyMDI2MDgwOHxHUjpQTVN8R1I6UElSfDIwMjYwODI4"
)
DEFAULT_TIMETABLES = [
    ("GR:PIR", "GR:PMS", "2026-08-08"),
    ("GR:PMS", "GR:PIR", "2026-08-28"),
]

# The booking engine rejects the call unless it looks like the site's own XHR:
# it reads the JSON body but insists on the form-urlencoded content type.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Origin": BASE_URL,
}

# availabilitySummary.cabinsAccommodation[].status values.
STATUS = {0: "SOLD OUT", 1: "AVAILABLE", 2: "LIMITED"}


class TokenExpired(RuntimeError):
    """Raised when the booking engine no longer recognises the state token."""


def fetch_itineraries(state: str, api_id: str, timetables: list[tuple[str, str, str]]) -> dict:
    """Call GetItineraries and return the parsed JSON, or raise TokenExpired.

    The endpoint answers 200 with HTML (not JSON) when the token/api_id is stale,
    and 200 with an empty ``data`` list when the signature does not validate; both
    mean the caller needs a fresh token rather than a transient retry.
    """
    url = BASE_URL + ITINERARIES_PATH.format(api_id=api_id)
    payload = {
        "state": state,
        "timetables": [
            {"departurePort": dep, "arrivalPort": arr, "departureDate": date}
            for dep, arr, date in timetables
        ],
    }
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    if "application/json" not in resp.headers.get("content-type", ""):
        raise TokenExpired("endpoint returned non-JSON (token/api_id no longer valid)")
    data = resp.json()
    if data.get("hasError"):
        raise TokenExpired(f"engine error: {data.get('errorMessages')}")
    if not data.get("data") or not any(leg.get("trips") for leg in data["data"]):
        raise TokenExpired("no sailings returned (signature rejected or token expired)")
    return data


def cabins_for_leg(leg: dict) -> list[dict]:
    """Flatten every cabin offer across all sailings of one timetable leg."""
    offers = []
    for trip in leg.get("trips", []):
        summary = trip.get("availabilitySummary", {})
        for cabin in summary.get("cabinsAccommodation", []):
            offers.append(
                {
                    "sailing": trip.get("departureDateTime", "?"),
                    "vessel": trip.get("vessel", {}).get("name", "?"),
                    "description": cabin.get("description", "?"),
                    "count": cabin.get("count", 0),
                    "status": cabin.get("status", 0),
                    "price": (cabin.get("price") or "").replace("&euro;", "€"),
                }
            )
    return offers


def available_offers(data: dict) -> list[dict]:
    """Return only the cabin offers that are actually bookable (count > 0)."""
    found = []
    for leg in data["data"]:
        route = f"{leg['timetable']['departurePort']}->{leg['timetable']['arrivalPort']}"
        date = leg["timetable"]["departureDate"]
        for offer in cabins_for_leg(leg):
            if offer["count"] > 0:
                found.append({"route": route, "date": date, **offer})
    return found


def render(data: dict) -> str:
    """Build a human-readable summary of cabin availability per leg."""
    lines = []
    for leg in data["data"]:
        tt = leg["timetable"]
        lines.append(f"\n  {tt['departurePort']} -> {tt['arrivalPort']}  ({tt['departureDate']})")
        offers = cabins_for_leg(leg)
        if not offers:
            lines.append("    (no cabin-bearing sailings)")
            continue
        avail = [o for o in offers if o["count"] > 0]
        lines.append(
            f"    {len(offers)} cabin types across {len({o['sailing'] for o in offers})} "
            f"sailing(s); {len(avail)} bookable"
        )
        for o in avail:
            lines.append(
                f"    ✅ {o['description']:<22} x{o['count']:<3} {o['price']:>9}  "
                f"[{o['vessel']} {o['sailing']}]"
            )
        if not avail:
            lines.append("    ❌ all cabins SOLD OUT")
    return "\n".join(lines)


# --- Shared options ---------------------------------------------------------


def _state_options(func):
    func = click.option("--state", default=DEFAULT_STATE, help="Signed 'br' token from a search.")(
        func
    )
    func = click.option("--api-id", default=DEFAULT_API_ID, help="Page api_id matching the token.")(
        func
    )
    return func


@click.group()
def cli():
    """Check / poll Blue Star Ferries cabin availability."""


@cli.command()
@_state_options
def check(state: str, api_id: str):
    """One-shot: print current cabin availability for the configured search."""
    try:
        data = fetch_itineraries(state, api_id, DEFAULT_TIMETABLES)
    except TokenExpired as exc:
        logger.error("Token rejected: %s", exc)
        logger.error("Capture a fresh token: run `bluestar_cabins.py refresh` for instructions.")
        sys.exit(2)
    avail = available_offers(data)
    logger.info("Cabin availability:%s", render(data))
    if avail:
        logger.info("\U0001f389 %d bookable cabin offer(s) found!", len(avail))
    else:
        logger.info("No cabins available yet.")


@cli.command()
@_state_options
@click.option("--interval", default=120, help="Seconds between polls.")
@click.option("--max-polls", default=0, help="Stop after N polls (0 = forever).")
def poll(state: str, api_id: str, interval: int, max_polls: int):
    """Loop: poll on an interval and ring the terminal bell when a cabin frees up."""
    logger.info("Polling every %ds (Ctrl-C to stop)...", interval)
    n = 0
    while True:
        n += 1
        try:
            data = fetch_itineraries(state, api_id, DEFAULT_TIMETABLES)
            avail = available_offers(data)
            if avail:
                # \a rings the terminal bell so an idle user gets a real alert.
                click.echo("\a", nl=False)
                logger.info("\U0001f389 CABIN(S) AVAILABLE!%s", render(data))
                for o in avail:
                    logger.info(
                        "  -> %s %s x%d %s on %s",
                        o["route"], o["description"], o["count"], o["price"], o["date"],
                    )
            else:
                logger.info("poll #%d: still sold out", n)
        except TokenExpired as exc:
            logger.error("poll #%d: token rejected (%s) - need a fresh --state/--api-id", n, exc)
        except requests.RequestException as exc:
            logger.warning("poll #%d: network error: %s", n, exc)
        if max_polls and n >= max_polls:
            logger.info("Reached max-polls=%d, stopping.", max_polls)
            return
        time.sleep(interval)


@cli.command()
def refresh():
    """Print instructions for capturing a fresh state token + api_id."""
    click.echo(
        """\
The `state` token is a server-signed value; it cannot be generated offline.
To capture a fresh one for any search:

  1. Open https://www.bluestarferries.com/en-gb in a browser.
  2. Search your route/dates/passengers and click SEARCH.
  3. On the results URL  .../en-gb/booking?br=XXXX  copy the value after `br=`.
     That is your --state token.
  4. View source on that page and find:
        reservationEndpoint: "/en-gb/reservationapi/{0}/YYYY"
     The YYYY is your --api-id.
  5. Run:  ./bluestar_cabins.py check --state XXXX --api-id YYYY

The token encodes the exact route/dates/pax, so a new search is needed to change
any of them. Tokens stay valid until the booking session expires server-side.
"""
    )


if __name__ == "__main__":
    cli()
