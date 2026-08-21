"""Parsing and the content hash that drives AI cache invalidation."""

import json

import pytest

from bot.scraper.parser import (
    compute_content_hash,
    parse_price,
    parse_search_results,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12500, (12500, None)),
        ("12 500 грн", (12500, "UAH")),
        ("12 500 грн.", (12500, "UAH")),
        ("450 $", (450, "USD")),
        ("399 €", (399, "EUR")),
        ({"value": 9999, "currency": "UAH"}, (9999, "UAH")),
        ({"displayValue": "31 000 грн"}, (31000, "UAH")),
        # Real OLX responses can carry `regularPrice: None` — present,
        # not just absent — which crashed the old `.get(key, {})` code.
        ({"value": None, "regularPrice": None, "displayValue": "20 000 грн"}, (20000, "UAH")),
        # The actual shape live OLX search results use: both the numeric
        # value and the currency code live under `regularPrice`, not at
        # the top level.
        (
            {
                "displayValue": "19 000 грн.",
                "regularPrice": {"value": 19000, "currencyCode": "UAH"},
            },
            (19000, "UAH"),
        ),
        ("Договірна", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_content_hash_is_stable():
    args = ("iPhone 13", "як новий", 20000, ["a.jpg", "b.jpg"])
    assert compute_content_hash(*args) == compute_content_hash(*args)


def test_content_hash_ignores_photo_order():
    """Photo order is not meaningful; re-ordering must not cost an API call."""
    a = compute_content_hash("t", "d", 100, ["a.jpg", "b.jpg"])
    b = compute_content_hash("t", "d", 100, ["b.jpg", "a.jpg"])
    assert a == b


@pytest.mark.parametrize(
    "changed",
    [
        ("iPhone 13 Pro", "як новий", 20000, ["a.jpg"]),
        ("iPhone 13", "терміново!", 20000, ["a.jpg"]),
        ("iPhone 13", "як новий", 18000, ["a.jpg"]),
        ("iPhone 13", "як новий", 20000, ["a.jpg", "c.jpg"]),
    ],
)
def test_content_hash_changes_on_any_edit(changed):
    """Each of these is an edit that should trigger a fresh analysis."""
    base = compute_content_hash("iPhone 13", "як новий", 20000, ["a.jpg"])
    assert compute_content_hash(*changed) != base


def _state_page(ads: list[dict]) -> str:
    blob = json.dumps(json.dumps({"listing": {"listing": {"ads": ads}}}, ensure_ascii=False))
    return f"<html><script>window.__PRERENDERED_STATE__ = {blob};window.foo=1;</script></html>"


def test_parse_search_results_from_embedded_state():
    html = _state_page(
        [
            {
                "id": 123456,
                "title": "iPhone 13 128GB",
                "url": "https://www.olx.ua/d/uk/obyavlenie/iphone-ID123456.html",
                "price": {"value": 20000, "currency": "UAH"},
                "location": {"city": {"name": "Київ"}},
                "photos": [{"link": "https://cdn/x;s={width}x{height}"}],
                "createdTime": "2026-08-20T10:00:00+03:00",
            }
        ]
    )

    cards = parse_search_results(html)

    assert len(cards) == 1
    card = cards[0]
    assert card.olx_id == "123456"
    assert card.title == "iPhone 13 128GB"
    assert card.price == 20000
    assert card.currency == "UAH"
    assert card.city == "Київ"
    assert "{width}" not in card.thumbnail
    assert card.posted_at is not None


def test_parse_search_results_matches_the_live_olx_shape():
    """The exact ad shape confirmed against real OLX search results.

    Price nests both the value and the currency code under
    `regularPrice`, and location uses a flat `cityName` string — neither
    matches the older/simpler shape in the test above, and both silently
    produced `currency=None`/`city=None` until this was caught against
    the live site.
    """
    html = _state_page(
        [
            {
                "id": 932390793,
                "title": "Iphone 13 Pro Max 128Gb Gold",
                "url": "https://www.olx.ua/d/uk/obyavlenie/iphone-ID932390793.html",
                "price": {
                    "displayValue": "19 000 грн.",
                    "regularPrice": {"value": 19000, "currencyCode": "UAH"},
                },
                "location": {"cityName": "Хуст", "cityId": 253},
                "photos": [],
            }
        ]
    )

    cards = parse_search_results(html)

    assert len(cards) == 1
    assert cards[0].price == 19000
    assert cards[0].currency == "UAH"
    assert cards[0].city == "Хуст"


def test_parse_search_results_falls_back_to_dom():
    """When the embedded state disappears, the DOM path must still work."""
    html = """
    <html><body>
      <div data-cy="l-card" id="987654">
        <a href="/d/uk/obyavlenie/iphone-14-ID987654.html">
          <h4>iPhone 14 256GB</h4>
        </a>
        <p data-testid="ad-price">25 000 грн</p>
        <p data-testid="location-date">Львів - Сьогодні</p>
        <img src="https://cdn/thumb.jpg"/>
      </div>
    </body></html>
    """

    cards = parse_search_results(html)

    assert len(cards) == 1
    assert cards[0].olx_id == "987654"
    assert cards[0].price == 25000
    assert cards[0].city == "Львів"
    assert cards[0].url.startswith("https://www.olx.ua/")


def test_parse_search_results_on_garbage_returns_empty():
    assert parse_search_results("<html><body>blocked</body></html>") == []
