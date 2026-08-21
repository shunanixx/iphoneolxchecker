"""Keyboards for the subscription wizard and the saved-filters list."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.constants import GENERATIONS, STORAGES, model_title, models_for_generation, storage_title
from bot.db.models import Subscription
from bot.keyboards.callbacks import filter_cb, menu_cb, sub_cb
from bot.middlewares.i18n import Translator


def generations_keyboard(i18n: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for generation in GENERATIONS:
        builder.button(text=f"iPhone {generation}", callback_data=filter_cb("gen", generation))
    builder.button(text=i18n("btn.cancel"), callback_data=filter_cb("cancel"))
    builder.adjust(3, 3, 3, 1)
    return builder.as_markup()


def models_keyboard(i18n: Translator, generation: int, selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for model in models_for_generation(generation):
        mark = "✅ " if model.key in selected else ""
        builder.button(
            text=f"{mark}{model.title}",
            callback_data=filter_cb("m", model.key),
        )
    builder.adjust(1)

    footer = InlineKeyboardBuilder()
    footer.button(text=i18n("btn.back"), callback_data=filter_cb("gens"))
    footer.button(text=i18n("btn.done"), callback_data=filter_cb("models_done"))
    footer.button(text=i18n("btn.cancel"), callback_data=filter_cb("cancel"))
    footer.adjust(2, 1)

    builder.attach(footer)
    return builder.as_markup()


def storages_keyboard(i18n: Translator, selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for storage in STORAGES:
        mark = "✅ " if storage in selected else ""
        builder.button(
            text=f"{mark}{storage_title(storage)}",
            callback_data=filter_cb("s", storage),
        )
    builder.adjust(3, 2)

    footer = InlineKeyboardBuilder()
    footer.button(text=i18n("btn.any"), callback_data=filter_cb("s_any"))
    footer.button(text=i18n("btn.done"), callback_data=filter_cb("storages_done"))
    footer.button(text=i18n("btn.cancel"), callback_data=filter_cb("cancel"))
    footer.adjust(2, 1)

    builder.attach(footer)
    return builder.as_markup()


def skip_keyboard(i18n: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("btn.skip"), callback_data=filter_cb("skip"))
    builder.button(text=i18n("btn.cancel"), callback_data=filter_cb("cancel"))
    builder.adjust(2)
    return builder.as_markup()


def any_city_keyboard(i18n: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("btn.any"), callback_data=filter_cb("city_any"))
    builder.button(text=i18n("btn.cancel"), callback_data=filter_cb("cancel"))
    builder.adjust(2)
    return builder.as_markup()


def confirm_keyboard(i18n: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("btn.done"), callback_data=filter_cb("save"))
    builder.button(text=i18n("btn.cancel"), callback_data=filter_cb("cancel"))
    builder.adjust(2)
    return builder.as_markup()


def subscriptions_keyboard(
    i18n: Translator, subscriptions: list[Subscription]
) -> InlineKeyboardMarkup:
    """One row per filter: a label, pause/resume, delete."""
    builder = InlineKeyboardBuilder()

    for index, sub in enumerate(subscriptions, start=1):
        models = sub.models or []
        label = model_title(models[0]) if models else "—"
        if len(models) > 1:
            label = f"{label} +{len(models) - 1}"

        builder.row(
            *InlineKeyboardBuilder()
            .button(text=f"{index}. {label}", callback_data=sub_cb(sub.id, "noop"))
            .buttons
        )
        builder.row(
            *InlineKeyboardBuilder()
            .button(text=i18n("btn.sub_toggle"), callback_data=sub_cb(sub.id, "toggle"))
            .button(text=i18n("btn.sub_delete"), callback_data=sub_cb(sub.id, "del"))
            .buttons
        )

    builder.row(
        *InlineKeyboardBuilder()
        .button(text=i18n("menu.new_sub"), callback_data=menu_cb("new_sub"))
        .button(text=i18n("btn.menu"), callback_data=menu_cb("main"))
        .buttons
    )
    return builder.as_markup()
