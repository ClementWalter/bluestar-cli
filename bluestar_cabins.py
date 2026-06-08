#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "click", "playwright"]
# ///
"""Check / watch Blue Star Ferries cabin availability for any route and dates.

Blue Star's booking engine exposes an undocumented JSON endpoint,
``POST /en-gb/reservationapi/GetItineraries/{api_id}``, that returns, per sailing,
an ``availabilitySummary.cabinsAccommodation`` list. Each entry carries a
``count`` (units left) and ``status`` (0 sold out, 1 available, 2 limited). When a
route is fully booked every ``count`` is 0; a cancellation flips one to ``> 0``,
which is what this tool watches for.

The request is authenticated by a server-signed ``state`` token (the ``br`` value
on the /booking URL) plus the page-rendered ``api_id``. The signature is computed
client-side and bound to the exact route/dates/pax, so it cannot be forged
offline. We therefore mint a token by reproducing the search in a headless
browser (``bluestar_mint``), cache it, and then poll the endpoint over plain HTTP
(no cookies needed). On expiry we transparently re-mint.

First-time setup: ``uv run --with playwright playwright install chromium``.

Examples:
    ./bluestar_cabins.py check  --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28
    ./bluestar_cabins.py poll   --from Piraeus --to Naxos  --depart 2026-07-12 --interval 300
    ./bluestar_cabins.py check  --from Piraeus --to Patmos --depart 2026-08-08 --adults 2 --headful
"""

import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import click
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger("bluestar")

BASE_URL = "https://www.bluestarferries.com"
ITINERARIES_PATH = "/en-gb/reservationapi/GetItineraries/{api_id}"

# The booking engine only answers when the request mimics the site's own XHR:
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

STATUS = {0: "SOLD OUT", 1: "AVAILABLE", 2: "LIMITED"}
CACHE_DIR = Path.home() / ".cache" / "bluestar"


class TokenExpired(RuntimeError):
    """Raised when the booking engine no longer recognises the state token."""


# --- HTTP polling -----------------------------------------------------------


def fetch_itineraries(state: str, api_id: str, timetables: list) -> dict:
    """Call GetItineraries and return parsed JSON, or raise TokenExpired.

    A stale token/api_id makes the endpoint answer 200 with HTML, and a rejected
    signature answers 200 JSON with empty ``data``; both mean re-mint, not retry.
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


# --- Token acquisition (mint + cache) ---------------------------------------


def _cache_key(frm, to, depart, ret, adults) -> Path:
    raw = f"{frm}|{to}|{depart}|{ret}|{adults}".lower()
    return CACHE_DIR / (hashlib.sha1(raw.encode()).hexdigest()[:16] + ".json")


def get_token(frm, to, depart, ret, adults, headless=True, force=False) -> dict:
    """Return a usable token dict, minting via headless browser if needed.

    Cached on disk per search so repeated polls reuse one browser-minted token.
    """
    cache = _cache_key(frm, to, depart, ret, adults)
    if cache.exists() and not force:
        return json.loads(cache.read_text())
    logger.info("Minting a booking token via headless browser (one-time per search)...")
    import bluestar_mint  # imported lazily so HTTP-only reuse needs no browser

    tok = bluestar_mint.mint_token(frm, to, depart, ret, adults, headless=headless)
    record = {"state": tok.state, "api_id": tok.api_id, "timetables": tok.timetables}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(record))
    logger.info("Token minted (api_id=%s) and cached.", tok.api_id)
    return record


def fetch_with_refresh(frm, to, depart, ret, adults, headless) -> dict:
    """Fetch itineraries, re-minting the token once if it has expired."""
    tok = get_token(frm, to, depart, ret, adults, headless=headless)
    try:
        return fetch_itineraries(tok["state"], tok["api_id"], tok["timetables"])
    except TokenExpired:
        logger.warning("Cached token rejected; re-minting...")
        tok = get_token(frm, to, depart, ret, adults, headless=headless, force=True)
        return fetch_itineraries(tok["state"], tok["api_id"], tok["timetables"])


# --- CLI --------------------------------------------------------------------


def _trip_options(func):
    func = click.option("--from", "frm", required=True, help="Departure port name, e.g. Piraeus.")(func)
    func = click.option("--to", required=True, help="Arrival port name, e.g. Patmos.")(func)
    func = click.option("--depart", required=True, help="Departure date YYYY-MM-DD.")(func)
    func = click.option("--return", "ret", default=None, help="Return date YYYY-MM-DD (omit for one-way).")(func)
    func = click.option("--adults", default=1, help="Number of passengers.")(func)
    func = click.option("--headful", is_flag=True, help="Show the browser while minting (debug).")(func)
    return func


@click.group()
def cli():
    """Check / watch Blue Star Ferries cabin availability for any route."""


@cli.command()
@_trip_options
def check(frm, to, depart, ret, adults, headful):
    """One-shot: print current cabin availability for the given trip."""
    try:
        data = fetch_with_refresh(frm, to, depart, ret, adults, headless=not headful)
    except Exception as exc:
        logger.error("Could not check availability: %s", exc)
        sys.exit(2)
    avail = available_offers(data)
    logger.info("Cabin availability:%s", render(data))
    logger.info("\U0001f389 %d bookable cabin offer(s)!" % len(avail) if avail else "No cabins available yet.")


@cli.command()
@_trip_options
@click.option("--interval", default=120, help="Seconds between polls.")
@click.option("--max-polls", default=0, help="Stop after N polls (0 = forever).")
def poll(frm, to, depart, ret, adults, headful, interval, max_polls):
    """Loop: poll on an interval and ring the terminal bell when a cabin frees up."""
    logger.info("Watching %s->%s %s%s every %ds (Ctrl-C to stop)...",
                frm, to, depart, f"/{ret}" if ret else "", interval)
    n = 0
    while True:
        n += 1
        try:
            data = fetch_with_refresh(frm, to, depart, ret, adults, headless=not headful)
            avail = available_offers(data)
            if avail:
                click.echo("\a", nl=False)  # terminal bell to alert an idle user
                logger.info("\U0001f389 CABIN(S) AVAILABLE!%s", render(data))
                for o in avail:
                    logger.info("  -> %s %s x%d %s on %s",
                                o["route"], o["description"], o["count"], o["price"], o["date"])
            else:
                logger.info("poll #%d: still sold out", n)
        except Exception as exc:
            logger.warning("poll #%d failed: %s", n, exc)
        if max_polls and n >= max_polls:
            logger.info("Reached max-polls=%d, stopping.", max_polls)
            return
        time.sleep(interval)


if __name__ == "__main__":
    cli()
