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
browser (Playwright), cache it, and then poll the endpoint over plain HTTP (no
cookies needed). On expiry we transparently re-mint.

First-time setup (installs the headless browser):
    uv run --with playwright playwright install chromium

Examples:
    ./bluestar_cli.py check --from Piraeus --to Patmos --depart 2026-08-08 --return 2026-08-28
    ./bluestar_cli.py poll  --from Piraeus --to Naxos  --depart 2026-07-12 --interval 300
    ./bluestar_cli.py check --from Piraeus --to Patmos --depart 2026-08-08 --adults 2 --headful
"""

import base64
import hashlib
import json
import logging
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import click
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger("bluestar")

BASE_URL = "https://www.bluestarferries.com"
HOMEPAGE = BASE_URL + "/en-gb"
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
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


class TokenExpired(RuntimeError):
    """Raised when the booking engine no longer recognises the state token."""


# ===========================================================================
# HTTP polling
# ===========================================================================


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


# ===========================================================================
# Token minting (headless browser) — Playwright is imported lazily
# ===========================================================================


@dataclass
class Token:
    """Everything the HTTP poller needs for one signed search."""

    state: str
    api_id: str
    timetables: list[tuple[str, str, str]]  # (depPortCode, arrPortCode, "YYYY-MM-DD")


def _decode_timetables(state: str) -> list[tuple[str, str, str]]:
    """Recover (dep, arr, date) legs from the readable tail of the state token.

    The token is base64( <binary signature> + "<apiId>|pax|veh|pets|isReturn|
    DEP|ARR|YYYYMMDD[|DEP|ARR|YYYYMMDD]" ). We split on the pipe-delimited tail so
    the poller's timetables match the signed legs exactly (a mismatch makes the
    engine return no sailings).
    """
    raw = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))
    fields = raw.decode("latin-1").split("|")
    legs_part = fields[5:]  # after apiId, pax, veh, pets, isReturn
    legs = []
    for i in range(0, len(legs_part) - 2, 3):
        dep, arr, ymd = legs_part[i], legs_part[i + 1], legs_part[i + 2]
        if len(ymd) == 8 and ymd.isdigit():
            legs.append((dep, arr, f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"))
    return legs


def _dismiss_cookie_banner(page):
    """Decline non-essential cookies so the OneTrust banner stops blocking clicks."""
    from playwright.sync_api import TimeoutError as PWTimeout

    for sel in ("#onetrust-reject-all-handler", "button:has-text('DECLINE')"):
        btn = page.locator(sel)
        if btn.count():
            try:
                btn.first.click(timeout=3000)
                page.wait_for_timeout(400)
                return
            except PWTimeout:
                pass


def _pick_port(page, placeholder: str, name: str):
    """Open a port dropdown and select the option matching ``name``."""
    page.get_by_placeholder(placeholder).click()
    page.wait_for_timeout(300)
    page.get_by_placeholder("Search").locator("visible=true").first.fill(name)
    page.wait_for_timeout(400)
    option = page.locator(
        ".v-list-item:visible", has_text=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
    )
    option.first.scroll_into_view_if_needed(timeout=4000)
    option.first.click(timeout=8000)


def _open_date_panel(page):
    """Open the departure date panel.

    The readonly date input sits under an invisible overlay, so a normal click is
    intercepted (force is required) and the first force-click only focuses it —
    the menu opens on a subsequent click. Clicking only while the picker is closed
    avoids toggling an already-open panel shut.
    """
    date_input = page.get_by_placeholder("Pick a Date").first
    header = page.locator(".v-date-picker-header__value:visible")
    for _ in range(6):
        if header.count():
            return
        date_input.click(force=True)
        page.wait_for_timeout(1000)
    raise RuntimeError("could not open the date picker")


def _select_day(page, target: str):
    """Navigate the open Vuetify date picker to ``target`` (YYYY-MM-DD) and click it.

    The picker header is ``[prev-arrow, "Month YYYY", next-arrow]``; day cells are
    ``.v-btn`` inside ``.v-date-picker-table`` (sold-out / past days carry
    ``v-btn--disabled`` and are simply not clicked).
    """
    year, month, day = (int(x) for x in target.split("-"))
    want_header = f"{MONTHS[month - 1]} {year}"
    for _ in range(24):
        visible = page.locator(".v-date-picker-header__value:visible").all_inner_texts()
        if want_header in [v.strip() for v in visible]:
            break
        # Header arrows are [prev, next]; the next-month arrow is the rightmost
        # enabled one (prev is disabled while viewing the current month).
        page.locator(".v-date-picker-header:visible .v-btn:not(.v-btn--disabled)").last.click()
        page.wait_for_timeout(450)
    else:
        raise RuntimeError(f"calendar never reached {want_header}")
    table = page.locator(".v-date-picker-table:visible").last
    table.locator(
        ".v-btn:not(.v-btn--disabled)", has_text=re.compile(rf"^{day}$")
    ).first.click(timeout=8000)


def mint_token(
    frm: str, to: str, depart: str, ret: str | None, adults: int, headless: bool = True
) -> Token:
    """Drive the homepage search for the given trip and return a signed Token."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        # Match the desktop layout (the widget is responsive; a narrow viewport
        # rearranges the date panels and breaks the picker selectors).
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        try:
            page.goto(HOMEPAGE, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)  # let the Vue booking widget hydrate
            _dismiss_cookie_banner(page)
            _pick_port(page, "Departure Port", frm)
            _pick_port(page, "Arrival Port", to)

            # Close the arrival dropdown, then open the departure date panel.
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            _open_date_panel(page)
            _select_day(page, depart)
            if ret:
                _select_day(page, ret)
            else:
                page.get_by_text("One Way Trip").click()

            for _ in range(adults - 1):  # default form starts at 1 passenger
                page.get_by_role("button", name="+").first.click()

            page.get_by_text(re.compile(r"^\s*SEARCH\s*→?\s*$")).last.click()
            page.wait_for_url("**/booking?br=*", timeout=20000)
            url = page.url
            html = page.content()
        except PWTimeout as exc:
            try:
                page.screenshot(path="mint_failure.png", timeout=5000, animations="disabled")
            except Exception:  # screenshot is best-effort diagnostics only
                pass
            raise RuntimeError(f"minting timed out: {exc}") from exc
        finally:
            browser.close()

    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["br"][0]
    m = re.search(r"reservationapi/\{0\}/([A-Z0-9]+)", html)
    if not m:
        raise RuntimeError("could not find api_id on booking page")
    return Token(state=state, api_id=m.group(1), timetables=_decode_timetables(state))


# ===========================================================================
# Token cache + refresh
# ===========================================================================


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
    tok = mint_token(frm, to, depart, ret, adults, headless=headless)
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


# ===========================================================================
# CLI
# ===========================================================================


def send_ntfy(target: str, title: str, message: str, priority: str = "high", tags: str = "ship"):
    """Publish a notification to an ntfy topic (bare name or full URL)."""
    url = target if target.startswith("http") else f"https://ntfy.sh/{target}"
    requests.post(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Click": HOMEPAGE,
        },
        timeout=15,
    )


def _offers_signature(offers: list[dict]) -> tuple:
    """Stable identity of an availability set, to notify only when it changes."""
    return tuple(sorted((o["route"], o["date"], o["description"], o["sailing"], o["count"]) for o in offers))


def _trip_options(func):
    func = click.option("--from", "frm", required=True, envvar="BSF_FROM", help="Departure port name, e.g. Piraeus.")(func)
    func = click.option("--to", required=True, envvar="BSF_TO", help="Arrival port name, e.g. Patmos.")(func)
    func = click.option("--depart", required=True, envvar="BSF_DEPART", help="Departure date YYYY-MM-DD.")(func)
    func = click.option("--return", "ret", default=None, envvar="BSF_RETURN", help="Return date YYYY-MM-DD (omit for one-way).")(func)
    func = click.option("--adults", default=1, envvar="BSF_ADULTS", help="Number of passengers.")(func)
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
@click.option("--interval", default=120, envvar="BSF_INTERVAL", help="Seconds between polls.")
@click.option("--max-polls", default=0, help="Stop after N polls (0 = forever).")
@click.option("--notify-ntfy", envvar="NTFY_URL", default=None,
              help="ntfy topic name or full URL to push to when a cabin frees up.")
def poll(frm, to, depart, ret, adults, headful, interval, max_polls, notify_ntfy):
    """Loop: poll on an interval, alert (bell + optional ntfy push) when a cabin frees up."""
    trip = f"{frm}->{to} {depart}" + (f"/{ret}" if ret else "")
    logger.info("Watching %s every %ds (Ctrl-C to stop)...", trip, interval)
    if notify_ntfy:
        # A startup ping confirms the notification channel is wired up correctly.
        try:
            send_ntfy(notify_ntfy, f"Bluestar watcher started: {trip}",
                      "Watching for cabin availability. You'll get a push when one frees up.",
                      priority="low", tags="eyes")
            logger.info("Sent ntfy startup ping.")
        except Exception as exc:
            logger.warning("ntfy startup ping failed: %s", exc)
    n, last_sig = 0, None
    while True:
        n += 1
        try:
            data = fetch_with_refresh(frm, to, depart, ret, adults, headless=not headful)
            avail = available_offers(data)
            if avail:
                click.echo("\a", nl=False)  # terminal bell to alert an idle user
                logger.info("\U0001f389 CABIN(S) AVAILABLE!%s", render(data))
                sig = _offers_signature(avail)
                # Notify on first appearance and whenever the available set changes,
                # not every interval (avoids spamming once a cabin is up).
                if notify_ntfy and sig != last_sig:
                    body = "\n".join(
                        f"{o['description']} x{o['count']} {o['price']} — {o['route']} {o['date']}"
                        for o in avail
                    )
                    try:
                        send_ntfy(notify_ntfy, f"🎉 Cabin available: {trip}", body)
                        logger.info("Sent ntfy alert.")
                    except Exception as exc:
                        logger.warning("ntfy alert failed: %s", exc)
                last_sig = sig
            else:
                last_sig = None  # reset so a later reappearance re-notifies
                logger.info("poll #%d: still sold out", n)
        except Exception as exc:
            logger.warning("poll #%d failed: %s", n, exc)
        if max_polls and n >= max_polls:
            logger.info("Reached max-polls=%d, stopping.", max_polls)
            return
        time.sleep(interval)


if __name__ == "__main__":
    cli()
