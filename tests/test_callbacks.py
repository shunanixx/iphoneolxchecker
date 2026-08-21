"""Every keyboard we can build must respect Telegram's 64-byte limit.

Over-long `callback_data` fails when the message is sent, not when the
keyboard is built — so it can ship and only break for the users whose
listing ids or model keys happen to be long.
"""

import pytest

from bot.constants import IPHONE_MODELS, STORAGES
from bot.keyboards.callbacks import CALLBACK_MAX_BYTES, cb, listing_cb
from bot.keyboards.filters import (
    generations_keyboard,
    models_keyboard,
    storages_keyboard,
    subscriptions_keyboard,
)
from bot.keyboards.listings import listing_card, listing_subview
from bot.keyboards.menu import main_menu, settings_menu
from bot.middlewares.i18n import SUPPORTED_LANGUAGES, Translator


def all_callback_data(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def test_cb_rejects_oversized_data():
    with pytest.raises(ValueError, match="callback_data"):
        cb("x" * 100)


def test_cb_counts_bytes_not_characters():
    """Cyrillic is two bytes per character — 40 chars is already 80 bytes."""
    with pytest.raises(ValueError):
        cb("б" * 40)


def test_listing_callbacks_fit_even_for_huge_ids():
    for action in ("card", "details", "photos", "reviews", "back"):
        data = listing_cb(9_999_999_999, action)
        assert len(data.encode()) <= CALLBACK_MAX_BYTES


def test_every_model_key_fits_in_a_callback():
    for model in IPHONE_MODELS:
        assert len(f"flt:m:{model.key}".encode()) <= CALLBACK_MAX_BYTES


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_all_keyboards_are_within_budget(language):
    i18n = Translator(language)

    markups = [
        main_menu(i18n),
        settings_menu(i18n, language),
        generations_keyboard(i18n),
        storages_keyboard(i18n, set(STORAGES)),
        listing_card(i18n, 123456789, "https://olx.ua/x"),
        listing_subview(i18n, 123456789, "https://olx.ua/x"),
    ]
    for generation in {model.generation for model in IPHONE_MODELS}:
        markups.append(models_keyboard(i18n, generation, set()))

    for markup in markups:
        for data in all_callback_data(markup):
            assert len(data.encode()) <= CALLBACK_MAX_BYTES, data


def test_subscription_keyboard_fits(language="uk"):
    from bot.db.models import Subscription

    subs = [
        Subscription(
            id=999999,
            user_id=1,
            models=[m.key for m in IPHONE_MODELS],
            storages=list(STORAGES),
            is_active=True,
        )
    ]
    markup = subscriptions_keyboard(Translator(language), subs)
    for data in all_callback_data(markup):
        assert len(data.encode()) <= CALLBACK_MAX_BYTES, data
