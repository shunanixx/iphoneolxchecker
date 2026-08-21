"""Subscription matching — the rule shared by the monitor and manual search."""

import pytest

from bot.db.models import Listing, Subscription
from bot.scheduler.monitor import matches_subscription


def make_listing(**overrides) -> Listing:
    defaults = dict(
        olx_id="1",
        url="https://olx.ua/x",
        title="iPhone 13 128GB",
        price=20000,
        currency="UAH",
        city="Київ",
        model="iphone_13",
        storage="128",
        content_hash="h",
        photos=[],
    )
    defaults.update(overrides)
    return Listing(**defaults)


def make_sub(**overrides) -> Subscription:
    defaults = dict(
        user_id=1,
        models=["iphone_13"],
        storages=["128"],
        price_min=None,
        price_max=None,
        city=None,
        is_active=True,
    )
    defaults.update(overrides)
    return Subscription(**defaults)


def test_exact_match():
    assert matches_subscription(make_listing(), make_sub())


def test_inactive_subscription_never_matches():
    assert not matches_subscription(make_listing(), make_sub(is_active=False))


def test_wrong_model_is_rejected():
    assert not matches_subscription(make_listing(model="iphone_14"), make_sub())


def test_empty_storages_means_any():
    sub = make_sub(storages=[])
    assert matches_subscription(make_listing(storage="512"), sub)
    assert matches_subscription(make_listing(storage=None), sub)


def test_undetected_storage_is_rejected_by_a_storage_filter():
    assert not matches_subscription(make_listing(storage=None), make_sub(storages=["128"]))


@pytest.mark.parametrize(
    ("price", "pmin", "pmax", "expected"),
    [
        (20000, 15000, 25000, True),
        (20000, 25000, None, False),
        (20000, None, 15000, False),
        (20000, 20000, 20000, True),
        (20000, None, None, True),
    ],
)
def test_price_bounds(price, pmin, pmax, expected):
    sub = make_sub(price_min=pmin, price_max=pmax)
    assert matches_subscription(make_listing(price=price), sub) is expected


def test_priceless_listing_fails_a_price_filter():
    """A «Договірна» listing has no number to compare against."""
    sub = make_sub(price_min=10000)
    assert not matches_subscription(make_listing(price=None), sub)


def test_priceless_listing_passes_when_no_price_filter():
    assert matches_subscription(make_listing(price=None), make_sub())


def test_city_match_is_case_insensitive_and_partial():
    sub = make_sub(city="київ")
    assert matches_subscription(make_listing(city="Київ, Дарницький"), sub)
    assert not matches_subscription(make_listing(city="Львів"), sub)


def test_city_filter_rejects_listing_without_city():
    assert not matches_subscription(make_listing(city=None), make_sub(city="Київ"))


def test_multi_model_subscription():
    sub = make_sub(models=["iphone_13", "iphone_14", "iphone_15"], storages=[])
    assert matches_subscription(make_listing(model="iphone_15"), sub)
    assert not matches_subscription(make_listing(model="iphone_12"), sub)
