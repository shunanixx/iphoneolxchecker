"""Model/storage detection — the cheap path that keeps AI usage down."""

import pytest

from bot.scraper.detector import (
    Detection,
    detect,
    detect_model,
    detect_storage,
    detect_with_fallback,
    looks_like_accessory,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("iPhone 13 128GB", "iphone_13"),
        ("Apple iPhone 13 Pro Max 256 Gb", "iphone_13_pro_max"),
        ("iphone 13 pro 256гб", "iphone_13_pro"),
        ("Айфон 12 міні 64 ГБ", "iphone_12_mini"),
        ("iPhone 14 Plus 128", "iphone_14_plus"),
        ("iPhone 15 ProMax 512GB", "iphone_15_pro_max"),
        ("Продам iPhone 11 в ідеалі", "iphone_11"),
        ("iPhone 17 Pro 1TB", "iphone_17_pro"),
    ],
)
def test_detect_model(title, expected):
    assert detect_model(title) == expected


def test_pro_max_wins_over_pro_and_base():
    """Longest alias first — otherwise every Pro Max is filed as a base model."""
    assert detect_model("iPhone 14 Pro Max") == "iphone_14_pro_max"
    assert detect_model("iPhone 14 Pro") == "iphone_14_pro"
    assert detect_model("iPhone 14") == "iphone_14"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("iPhone 13 128GB", "128"),
        ("iPhone 13 256 гб", "256"),
        ("iPhone 15 Pro 1TB", "1024"),
        ("iPhone 15 Pro 1 ТБ", "1024"),
        ("iPhone 12 64gb", "64"),
        ("iPhone 12 512", "512"),
    ],
)
def test_detect_storage(text, expected):
    assert detect_storage(text) == expected


def test_detect_storage_ignores_unrelated_numbers():
    assert detect_storage("iPhone 13 ідеальний стан, 100% батарея") is None


def test_detect_reads_description_when_title_is_thin():
    result = detect("Продам телефон Apple", "iPhone 13 Pro, память 256 ГБ, стан ідеальний")
    assert result.model == "iphone_13_pro"
    assert result.storage == "256"
    assert result.source == "regex"


@pytest.mark.parametrize(
    "title",
    [
        "Чехол для iPhone 13 Pro",
        "Скло на айфон 12",
        "iPhone 11 на запчасти",
        "Дисплей iPhone 13",
        "Копия iPhone 15 Pro Max",
    ],
)
def test_accessories_are_flagged(title):
    assert looks_like_accessory(title)


def test_real_listing_is_not_an_accessory():
    assert not looks_like_accessory("iPhone 13 Pro 256GB, ідеальний стан, повний комплект")


async def test_fallback_not_called_when_regex_is_confident():
    calls = []

    async def classifier(title, description):
        calls.append(title)
        return "iphone_13", "128"

    result = await detect_with_fallback("iPhone 13 Pro Max 256GB", "", classifier)

    assert calls == [], "AI fallback must not fire on an unambiguous title"
    assert result == Detection("iphone_13_pro_max", "256", "regex")


async def test_fallback_fills_in_what_regex_missed():
    async def classifier(title, description):
        return "iphone_14_pro", "256"

    result = await detect_with_fallback("Продам яблуко-телефон, як новий", "", classifier)

    assert result.model == "iphone_14_pro"
    assert result.storage == "256"
    assert result.source == "ai"


async def test_fallback_output_is_validated():
    """A model that invents a key must not reach the database."""

    async def classifier(title, description):
        return "iphone_99_ultra", "999"

    result = await detect_with_fallback("Телефон Apple вживаний", "", classifier)

    assert result.model is None
    assert result.storage is None


async def test_fallback_failure_is_not_fatal():
    async def classifier(title, description):
        raise RuntimeError("API down")

    result = await detect_with_fallback("Телефон Apple", "", classifier)
    assert result.model is None
