"""Parsing and the content hash that drives AI cache invalidation."""

import json

import pytest

from bot.scraper.parser import (
    compute_content_hash,
    parse_listing_detail,
    parse_price,
    parse_search_results,
    parse_seller_reviews,
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


def _detail_page(ad: dict, seller_link_html: str = "") -> str:
    blob = json.dumps(json.dumps({"ad": ad}, ensure_ascii=False))
    return (
        f"<html><body>{seller_link_html}</body>"
        f"<script>window.__PRERENDERED_STATE__ = {blob};window.foo=1;</script></html>"
    )


def test_seller_profile_url_comes_from_the_dom_anchor():
    """The real shape: the state's `user` has no URL field at all — OLX
    encodes the profile URL as an opaque slug only present in the page
    markup, behind `data-testid="user-profile-link"`.
    """
    ad = {
        "id": 1,
        "description": "Продам iPhone",
        "photos": [],
        "user": {"id": 958029057, "uuid": "cb320a7b-c192-46a9-80f2-c5746e6bd2e7", "name": "Іван"},
    }
    html = _detail_page(
        ad, '<a data-testid="user-profile-link" href="/uk/list/user/1VqEek/">Іван</a>'
    )

    detail = parse_listing_detail(html)

    assert detail.seller_profile_url == "https://www.olx.ua/uk/list/user/1VqEek/"
    assert detail.seller_name == "Іван"


def test_seller_profile_url_is_not_guessed_from_user_id():
    """Regression: the old code built `/uk/list/user/{user.id}/`, which
    404s on live OLX — a numeric id must never be used as a fallback URL.
    """
    ad = {
        "id": 1,
        "description": "Продам iPhone",
        "photos": [],
        "user": {"id": 958029057, "name": "Іван"},
    }
    html = _detail_page(ad)  # no profile-link anchor at all

    detail = parse_listing_detail(html)

    assert detail.seller_profile_url is None
    assert "958029057" not in (detail.seller_profile_url or "")


def _seller_page_state(seller_data: dict) -> str:
    blob = json.dumps(
        json.dumps({"userListing": {"seller": {"data": seller_data}}}, ensure_ascii=False)
    )
    return (
        "<html><body></body><script>"
        f"window.__PRERENDERED_STATE__ = {blob};window.x=1;"
        "</script></html>"
    )


def test_parse_seller_reviews_matches_the_live_shape():
    """Real seller pages carry no rating/review-count field at all — that
    widget loads over a separate request our HTTP client never makes.
    `created` is the reliable signal that identifies the seller object,
    confirmed against a live `/uk/list/user/...` page.
    """
    html = _seller_page_state(
        {
            "id": 29095517,
            "uuid": "43ed696d-9996-47b3-acdf-0720a2bd155f",
            "created": "2014-12-12T15:21:49+02:00",
            "name": "Олена",
        }
    )

    reviews = parse_seller_reviews(html)

    assert reviews["since"] == "2014-12-12"
    assert reviews["rating"] is None
    assert reviews["reviews_count"] is None


def test_parse_seller_reviews_with_no_matching_object_degrades_gracefully():
    html = "<html><body>no embedded state here</body></html>"

    reviews = parse_seller_reviews(html)

    assert reviews == {"rating": None, "reviews_count": None, "reviews": [], "since": None}
