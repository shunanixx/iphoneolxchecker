"""Main menu and settings keyboards."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.callbacks import menu_cb, settings_cb
from bot.middlewares.i18n import LOCALES, SUPPORTED_LANGUAGES, Translator


def main_menu(i18n: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("menu.search"), callback_data=menu_cb("search"))
    builder.button(text=i18n("menu.new_sub"), callback_data=menu_cb("new_sub"))
    builder.button(text=i18n("menu.subs"), callback_data=menu_cb("subs"))
    builder.button(text=i18n("menu.settings"), callback_data=menu_cb("settings"))
    builder.button(text=i18n("menu.help"), callback_data=menu_cb("help"))
    builder.adjust(1, 2, 2)
    return builder.as_markup()


def back_to_menu(i18n: Translator) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n("btn.menu"), callback_data=menu_cb("main"))]
        ]
    )


def settings_menu(i18n: Translator, current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for language in SUPPORTED_LANGUAGES:
        name = LOCALES[language].get("lang.name", language)
        mark = "✅ " if language == current else ""
        builder.button(text=f"{mark}{name}", callback_data=settings_cb("lang", language))
    builder.button(text=i18n("btn.menu"), callback_data=menu_cb("main"))
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()
