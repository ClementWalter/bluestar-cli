#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "requests", "click", "responses"]
# ///
"""Unit tests for the cabin-availability parsing and the token-expiry guard.

The network call itself is stubbed (via `responses`) so the tests exercise the
real branching — JSON-vs-HTML detection, empty-data detection, count filtering —
without depending on the live booking engine.
"""

import importlib.util
from pathlib import Path

import pytest
import responses

# Load the hyphen-free module under test by path (it is a script, not a package).
_spec = importlib.util.spec_from_file_location(
    "bluestar_cabins", Path(__file__).with_name("bluestar_cabins.py")
)
bsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsc)


@pytest.fixture
def sold_out_leg():
    """One leg whose only sailing has every cabin at count 0."""
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
    """One leg with a single bookable 2-bed cabin (count 5)."""
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
        bsc.fetch_itineraries("state", "X", bsc.DEFAULT_TIMETABLES)


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
        bsc.fetch_itineraries("state", "X", bsc.DEFAULT_TIMETABLES)


@responses.activate
def test_fetch_returns_data_on_success(available_leg):
    responses.add(
        responses.POST,
        bsc.BASE_URL + bsc.ITINERARIES_PATH.format(api_id="X"),
        json={"hasError": False, "errorMessages": [], "data": [available_leg]},
        status=200,
    )
    result = bsc.fetch_itineraries("state", "X", bsc.DEFAULT_TIMETABLES)
    assert result["data"][0]["trips"][0]["vessel"]["name"] == "Blue Star 2"
