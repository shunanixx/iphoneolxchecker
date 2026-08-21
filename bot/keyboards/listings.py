"""Listing card navigation — the stateless model from ARCHITECTURE.md §7.

Every button carries the `listing_id` it operates on, so any sub-view is
reachable and returnable from the callback alone. There is deliberately
no "current listing" stored per user: a card sent an hour ago still works
after a restart, and two cards in the same chat never fight over whose
view is current.
"""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.callbacks import listing_cb, menu_cb
from bot.middlewares.i18n import Translator


def listing_card(i18n: Translator, listing_id: int, url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("btn.details"), callback_data=listing_cb(listing_id, "details"))
    builder.button(text=i18n("btn.photos"), callback_data=listing_cb(listing_id, "photos"))
    builder.button(text=i18n("btn.reviews"), callback_data=listing_cb(listing_id, "reviews"))
    builder.button(text=i18n("btn.open"), url=url)
    builder.adjust(2, 2)
    return builder.as_markup()


def listing_subview(i18n: Translator, listing_id: int, url: str) -> InlineKeyboardMarkup:
    """Shared footer for details/photos/reviews — `back` re-renders the card."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("btn.back"), callback_data=listing_cb(listing_id, "back"))
    builder.button(text=i18n("btn.open"), url=url)
    builder.adjust(2)
    return builder.as_markup()


def search_results_footer(i18n: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("btn.menu"), callback_data=menu_cb("main"))
    return builder.as_markup()
