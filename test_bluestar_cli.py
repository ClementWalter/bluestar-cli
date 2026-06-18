#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "requests", "click", "responses", "playwright"]
# ///
"""Unit tests for cabin parsing, the token-expiry guard, and token plumbing.

The network call is stubbed (via `responses`) so the tests exercise the real
branching — JSON-vs-HTML detection, empty-data detection, count filtering — and
the browser is never launched (only the pure token-decode helper is tested).
"""

import importlib.util
from pathlib import Path

import pytest
import responses


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bsc = _load("bluestar_cli")
mint = bsc  # mint helpers now live in the same consolidated module

TIMETABLES = [("GR:PIR", "GR:PMS", "2026-08-08")]


@pytest.fixture
def sold_out_leg():
    """A leg whose only sailing has every cabin at count 0."""
    return {
        "timetable": {"departurePort": "GR:PIR", "arrivalPort": "GR:PMS", "departureDate": "2026-08-08"},
        "trips": [
            {
                "departureDateTime": "2026-08-08 23:55",
                "vessel": {"name": "Blue Star 2"},
                "availabilitySummary": {
                    "cabinsAccommodation": [
                        {"description": "4 bed cabin", "count": 0, "status": 0, "price": "&euro;104.00"},
                        {"description": "2 bed cabin", "count": 0, "status": 0, "price": "&euro;114.50"},
                    ]
                },
            }
        ],
    }


@pytest.fixture
def available_leg():
    """A leg with a single bookable 2-bed cabin (count 5)."""
    return {
        "timetable": {"departurePort": "GR:PMS", "arrivalPort": "GR:PIR", "departureDate": "2026-08-28"},
        "trips": [
            {
                "departureDateTime": "2026-08-28 03:00",
                "vessel": {"name": "Blue Star 2"},
                "availabilitySummary": {
                    "cabinsAccommodation": [
                        {"description": "4 bed cabin", "count": 0, "status": 0, "price": "&euro;104.00"},
                        {"description": "2 bed cabin", "count": 5, "status": 1, "price": "&euro;114.50"},
                    ]
                },
            }
        ],
    }


def test_cabins_for_leg_flattens_all_offers(sold_out_leg):
    assert len(bsc.cabins_for_leg(sold_out_leg)) == 2


def test_available_offers_empty_when_sold_out(sold_out_leg):
    assert bsc.available_offers({"data": [sold_out_leg]}) == []


def test_available_offers_finds_bookable_cabin(available_leg):
    assert len(bsc.available_offers({"data": [available_leg]})) == 1


def test_available_offers_reports_the_count(available_leg):
    assert bsc.available_offers({"data": [available_leg]})[0]["count"] == 5


def test_available_offers_decodes_price_entity(available_leg):
    assert bsc.available_offers({"data": [available_leg]})[0]["price"] == "€114.50"


@responses.activate
def test_fetch_raises_on_html_response():
    """A stale token makes the engine serve an HTML page instead of JSON."""
    responses.add(
        responses.POST,
        bsc.BASE_URL + bsc.ITINERARIES_PATH.format(api_id="X"),
        body="<!DOCTYPE html><html></html>",
        content_type="text/html",
        status=200,
    )
    with pytest.raises(bsc.TokenExpired):
        bsc.fetch_itineraries("state", "X", TIMETABLES)


@responses.activate
def test_fetch_raises_on_empty_data():
    """A rejected signature returns 200 JSON with no sailings."""
    responses.add(
        responses.POST,
        bsc.BASE_URL + bsc.ITINERARIES_PATH.format(api_id="X"),
        json={"hasError": False, "errorMessages": [], "data": [{"trips": []}]},
        status=200,
    )
    with pytest.raises(bsc.TokenExpired):
        bsc.fetch_itineraries("state", "X", TIMETABLES)


@responses.activate
def test_fetch_returns_data_on_success(available_leg):
    responses.add(
        responses.POST,
        bsc.BASE_URL + bsc.ITINERARIES_PATH.format(api_id="X"),
        json={"hasError": False, "errorMessages": [], "data": [available_leg]},
        status=200,
    )
    result = bsc.fetch_itineraries("state", "X", TIMETABLES)
    assert result["data"][0]["trips"][0]["vessel"]["name"] == "Blue Star 2"


def test_cache_key_is_stable_for_same_search():
    a = bsc._cache_key("Piraeus", "Patmos", "2026-08-08", "2026-08-28", 1)
    b = bsc._cache_key("piraeus", "patmos", "2026-08-08", "2026-08-28", 1)
    assert a == b


def test_cache_key_differs_for_different_search():
    a = bsc._cache_key("Piraeus", "Patmos", "2026-08-08", None, 1)
    b = bsc._cache_key("Piraeus", "Naxos", "2026-08-08", None, 1)
    assert a != b


def test_offers_signature_ignores_order(available_leg):
    offers = bsc.available_offers({"data": [available_leg]})
    assert bsc._offers_signature(offers) == bsc._offers_signature(list(reversed(offers)))


def test_offers_signature_changes_with_count(available_leg):
    offers = bsc.available_offers({"data": [available_leg]})
    bumped = [{**offers[0], "count": offers[0]["count"] + 1}]
    assert bsc._offers_signature(offers) != bsc._offers_signature(bumped)


def test_status_line_sold_out(sold_out_leg):
    assert bsc.status_line({"data": [sold_out_leg]}) == "all cabins sold out; seats n/a"


def test_status_line_reports_available(available_leg):
    assert bsc.status_line({"data": [available_leg]}).startswith("1 bookable cabin offer(s)")


@responses.activate
def test_send_ntfy_posts_to_bare_topic():
    responses.add(responses.POST, "https://ntfy.sh/my-topic", status=200)
    bsc.send_ntfy("my-topic", "title", "body")
    assert responses.calls[0].request.headers["Title"] == "title"


@responses.activate
def test_send_ntfy_accepts_full_url():
    responses.add(responses.POST, "https://ntfy.example.com/t", status=200)
    bsc.send_ntfy("https://ntfy.example.com/t", "title", "body")
    assert responses.calls[0].request.url == "https://ntfy.example.com/t"


@pytest.fixture
def green_seat_leg():
    """A leg whose seats are sold out for cabins but green for a seat type."""
    return {
        "timetable": {"departurePort": "GR:PMS", "arrivalPort": "GR:PIR", "departureDate": "2026-08-28"},
        "trips": [
            {
                "departureDateTime": "2026-08-28 00:15",
                "vessel": {"name": "Blue Star 2"},
                "availabilitySummary": {
                    "cabinsAccommodation": [{"description": "2 bed cabin", "count": 0, "status": 0, "price": "&euro;114.50"}],
                    "seatsAccommodation": [
                        {"description": "Economy", "count": 1044, "status": 2, "price": "&euro;56.00"},
                        {"description": "Airplane Type Seats", "count": 8, "status": 1, "price": "&euro;61.00"},
                    ],
                },
            }
        ],
    }


SAILING_KEY = ("GR:PMS->GR:PIR", "2026-08-28", "2026-08-28 00:15")


def test_seat_status_is_green_when_a_seat_type_is_available(green_seat_leg):
    assert bsc.seat_status_by_sailing({"data": [green_seat_leg]})[SAILING_KEY] == 0


def test_seat_status_is_orange_when_only_limited_seats(green_seat_leg):
    # Drop the green seat type; only the orange (status 2) Economy remains.
    green_seat_leg["trips"][0]["availabilitySummary"]["seatsAccommodation"].pop()
    assert bsc.seat_status_by_sailing({"data": [green_seat_leg]})[SAILING_KEY] == 1


def test_seat_status_skips_sailings_without_seats(sold_out_leg):
    assert bsc.seat_status_by_sailing({"data": [sold_out_leg]}) == {}


def test_seat_degradations_flags_green_to_orange():
    assert bsc.seat_degradations({SAILING_KEY: 0}, {SAILING_KEY: 1}) == [(SAILING_KEY, 0, 1)]


def test_seat_degradations_empty_when_unchanged():
    assert bsc.seat_degradations({SAILING_KEY: 1}, {SAILING_KEY: 1}) == []


def test_seat_degradations_ignores_improvement():
    assert bsc.seat_degradations({SAILING_KEY: 1}, {SAILING_KEY: 0}) == []


def test_seat_degradations_ignores_unseen_sailing():
    assert bsc.seat_degradations({}, {SAILING_KEY: 2}) == []


def test_status_line_reports_seat_status(green_seat_leg):
    assert bsc.status_line({"data": [green_seat_leg]}).endswith("seats available (green)")


def test_decode_timetables_recovers_round_trip_legs():
    # base64( <2 sig bytes> + "APIID|1|0|0|True|GR:PIR|GR:PMS|20260808|GR:PMS|GR:PIR|20260828" )
    import base64

    payload = b"\x00\x01APIID|1|0|0|True|GR:PIR|GR:PMS|20260808|GR:PMS|GR:PIR|20260828"
    state = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert mint._decode_timetables(state) == [
        ("GR:PIR", "GR:PMS", "2026-08-08"),
        ("GR:PMS", "GR:PIR", "2026-08-28"),
    ]
