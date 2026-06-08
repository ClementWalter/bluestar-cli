#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
"""Mint a signed Blue Star Ferries booking token by driving the real search.

The ``br`` / ``state`` token is signed client-side from a per-page-render
``api_id`` and the search payload; the signing routine is buried in the site's
minified Vue bundle and cannot be reproduced offline. So to support an arbitrary
route/date we reproduce the search in a headless browser and read back the
resulting ``br`` (from the /booking URL) and ``api_id`` (from the page), which a
lightweight HTTP poller then reuses. Run ``playwright install chromium`` once.
"""

import base64
import logging
import re
import urllib.parse
from dataclasses import dataclass

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

logger = logging.getLogger("bluestar.mint")

HOMEPAGE = "https://www.bluestarferries.com/en-gb"
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


@dataclass
class Token:
    """Everything the HTTP poller needs for one signed search."""

    state: str
    api_id: str
    timetables: list[tuple[str, str, str]]  # (depPortCode, arrPortCode, "YYYY-MM-DD")


def _decode_timetables(state: str) -> list[tuple[str, str, str]]:
    """Recover (dep, arr, date) legs from the readable tail of the state token.

    The token is base64( <binary signature> + "<apiId>|pax|veh|pets|isReturn|
    DEP|ARR|YYYYMMDD[|DEP|ARR|YYYYMMDD]" ). We split on the api_id marker and
    walk the pipe-delimited remainder so the poller's timetables match the
    signed legs exactly (a mismatch makes the engine return no sailings).
    """
    raw = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))
    text = raw.decode("latin-1")
    fields = text.split("|")
    # fields: [..sig+apiId, pax, veh, pets, isReturn, DEP, ARR, DATE, (DEP, ARR, DATE)]
    legs_part = fields[5:]
    legs = []
    for i in range(0, len(legs_part) - 2, 3):
        dep, arr, ymd = legs_part[i], legs_part[i + 1], legs_part[i + 2]
        if len(ymd) == 8 and ymd.isdigit():
            legs.append((dep, arr, f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"))
    return legs


def _dismiss_cookie_banner(page):
    """Decline non-essential cookies so the OneTrust banner stops blocking clicks."""
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
    # The visible dropdown's own search box (each dropdown renders its own).
    search = page.get_by_placeholder("Search").locator("visible=true").first
    search.fill(name)
    page.wait_for_timeout(400)
    # Vuetify renders options as .v-list-item; match the visible one for this port.
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

            # Open the departure date panel and wait for the Vuetify picker to render.
            page.wait_for_timeout(400)
            # The readonly date input sits under an invisible overlay; a normal
            # click is intercepted, so force the click to trigger the v-menu.
            # Close the arrival dropdown first so its menu does not shadow ours.
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


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    args = dict(arg.split("=", 1) for arg in sys.argv[1:])
    tok = mint_token(
        args.get("from", "Piraeus"),
        args.get("to", "Patmos"),
        args.get("depart", "2026-08-08"),
        args.get("return") or None,
        int(args.get("adults", "1")),
        headless=args.get("headless", "1") != "0",
    )
    print(json.dumps({"api_id": tok.api_id, "timetables": tok.timetables, "state_len": len(tok.state)}))
