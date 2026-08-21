"""Subscription wizard (FSM) and the saved-filters list.

This is the only place with real conversational state: the wizard has to
remember partial answers across several messages. Everything else in the
bot — listing navigation especially — stays stateless.
"""

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.constants import MODELS_BY_KEY, STORAGES
from bot.db import crud
from bot.db.engine import session_scope
from bot.handlers.common import safe_edit
from bot.keyboards.filters import (
    any_city_keyboard,
    confirm_keyboard,
    generations_keyboard,
    models_keyboard,
    skip_keyboard,
    storages_keyboard,
    subscriptions_keyboard,
)
from bot.keyboards.menu import main_menu
from bot.middlewares.i18n import Translator
from bot.render import render_filter_summary, render_subscriptions
from bot.states.filters import FilterWizard
from bot.utils.logging import get_logger

router = Router(name="filters")
log = get_logger(__name__)

#: Each filter costs one OLX search per model per cycle, so this is a
#: politeness limit as much as a UX one.
MAX_SUBSCRIPTIONS = 5
MAX_PRICE = 10_000_000
MAX_CITY_LENGTH = 64


def _parse_price(text: str) -> int | None:
    """Accept "12 500", "12500грн", "12.500" — reject anything else."""
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    value = int(digits)
    return value if 0 < value <= MAX_PRICE else None


# --------------------------------------------------------------------------
# wizard
# --------------------------------------------------------------------------


@router.callback_query(F.data == "menu:new_sub")
async def start_wizard(
    callback: CallbackQuery, state: FSMContext, i18n: Translator, user_id: int
) -> None:
    async with session_scope() as session:
        existing = await crud.list_subscriptions(session, user_id)

    if len(existing) >= MAX_SUBSCRIPTIONS:
        await callback.answer(
            i18n("filters.limit_reached", limit=MAX_SUBSCRIPTIONS), show_alert=True
        )
        return

    await state.clear()
    await state.set_state(FilterWizard.choosing_generation)
    await safe_edit(callback.message, i18n("filters.pick_generation"), generations_keyboard(i18n))
    await callback.answer()


@router.callback_query(F.data == "flt:gens")
async def back_to_generations(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    await state.set_state(FilterWizard.choosing_generation)
    await safe_edit(callback.message, i18n("filters.pick_generation"), generations_keyboard(i18n))
    await callback.answer()


@router.callback_query(F.data.startswith("flt:gen:"))
async def pick_generation(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    generation = int(callback.data.split(":")[2])
    data = await state.get_data()
    selected = set(data.get("models") or [])

    await state.update_data(generation=generation)
    await state.set_state(FilterWizard.choosing_models)
    await safe_edit(
        callback.message,
        i18n("filters.pick_model"),
        models_keyboard(i18n, generation, selected),
    )
    await callback.answer()


@router.callback_query(FilterWizard.choosing_models, F.data.startswith("flt:m:"))
async def toggle_model(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in MODELS_BY_KEY:
        await callback.answer()
        return

    data = await state.get_data()
    selected = set(data.get("models") or [])
    selected.symmetric_difference_update({key})

    await state.update_data(models=sorted(selected))
    generation = data.get("generation") or MODELS_BY_KEY[key].generation
    await safe_edit(
        callback.message,
        i18n("filters.pick_model"),
        models_keyboard(i18n, generation, selected),
    )
    await callback.answer()


@router.callback_query(FilterWizard.choosing_models, F.data == "flt:models_done")
async def models_done(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    data = await state.get_data()
    if not data.get("models"):
        await callback.answer(i18n("filters.need_model"), show_alert=True)
        return

    await state.set_state(FilterWizard.choosing_storages)
    await safe_edit(
        callback.message,
        i18n("filters.pick_storage"),
        storages_keyboard(i18n, set(data.get("storages") or [])),
    )
    await callback.answer()


@router.callback_query(FilterWizard.choosing_storages, F.data.startswith("flt:s:"))
async def toggle_storage(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    storage = callback.data.split(":", 2)[2]
    if storage not in STORAGES:
        await callback.answer()
        return

    data = await state.get_data()
    selected = set(data.get("storages") or [])
    selected.symmetric_difference_update({storage})

    await state.update_data(storages=sorted(selected))
    await safe_edit(
        callback.message, i18n("filters.pick_storage"), storages_keyboard(i18n, selected)
    )
    await callback.answer()


@router.callback_query(
    FilterWizard.choosing_storages, F.data.in_({"flt:s_any", "flt:storages_done"})
)
async def storages_done(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    if callback.data == "flt:s_any":
        # Empty list is how "any storage" is stored — see matches_subscription.
        await state.update_data(storages=[])

    await state.set_state(FilterWizard.entering_price_min)
    await safe_edit(callback.message, i18n("filters.price_min"), skip_keyboard(i18n))
    await callback.answer()


@router.message(FilterWizard.entering_price_min, F.text)
async def enter_price_min(message: Message, state: FSMContext, i18n: Translator) -> None:
    price = _parse_price(message.text)
    if price is None:
        await message.answer(i18n("filters.bad_number"), reply_markup=skip_keyboard(i18n))
        return

    await state.update_data(price_min=price)
    await state.set_state(FilterWizard.entering_price_max)
    await message.answer(i18n("filters.price_max"), reply_markup=skip_keyboard(i18n))


@router.message(FilterWizard.entering_price_max, F.text)
async def enter_price_max(message: Message, state: FSMContext, i18n: Translator) -> None:
    price = _parse_price(message.text)
    if price is None:
        await message.answer(i18n("filters.bad_number"), reply_markup=skip_keyboard(i18n))
        return

    data = await state.get_data()
    price_min = data.get("price_min")
    if price_min is not None and price < price_min:
        await message.answer(i18n("filters.bad_range"), reply_markup=skip_keyboard(i18n))
        return

    await state.update_data(price_max=price)
    await state.set_state(FilterWizard.entering_city)
    await message.answer(i18n("filters.city"), reply_markup=any_city_keyboard(i18n))


@router.callback_query(FilterWizard.entering_price_min, F.data == "flt:skip")
async def skip_price_min(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    await state.update_data(price_min=None)
    await state.set_state(FilterWizard.entering_price_max)
    await safe_edit(callback.message, i18n("filters.price_max"), skip_keyboard(i18n))
    await callback.answer()


@router.callback_query(FilterWizard.entering_price_max, F.data == "flt:skip")
async def skip_price_max(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    await state.update_data(price_max=None)
    await state.set_state(FilterWizard.entering_city)
    await safe_edit(callback.message, i18n("filters.city"), any_city_keyboard(i18n))
    await callback.answer()


@router.message(FilterWizard.entering_city, F.text)
async def enter_city(message: Message, state: FSMContext, i18n: Translator) -> None:
    city = (message.text or "").strip()[:MAX_CITY_LENGTH]
    await state.update_data(city=city or None)
    await _show_summary(message, state, i18n)


@router.callback_query(FilterWizard.entering_city, F.data == "flt:city_any")
async def skip_city(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    await state.update_data(city=None)
    await state.set_state(FilterWizard.confirming)
    data = await state.get_data()
    await safe_edit(callback.message, render_filter_summary(data, i18n), confirm_keyboard(i18n))
    await callback.answer()


async def _show_summary(message: Message, state: FSMContext, i18n: Translator) -> None:
    await state.set_state(FilterWizard.confirming)
    data = await state.get_data()
    await message.answer(render_filter_summary(data, i18n), reply_markup=confirm_keyboard(i18n))


@router.callback_query(FilterWizard.confirming, F.data == "flt:save")
async def save_filter(
    callback: CallbackQuery, state: FSMContext, i18n: Translator, user_id: int
) -> None:
    data = await state.get_data()
    models = data.get("models") or []
    if not models:
        await callback.answer(i18n("filters.need_model"), show_alert=True)
        return

    async with session_scope() as session:
        existing = await crud.list_subscriptions(session, user_id)
        if len(existing) >= MAX_SUBSCRIPTIONS:
            await callback.answer(
                i18n("filters.limit_reached", limit=MAX_SUBSCRIPTIONS), show_alert=True
            )
            return

        await crud.create_subscription(
            session,
            user_id,
            models=models,
            storages=data.get("storages") or [],
            price_min=data.get("price_min"),
            price_max=data.get("price_max"),
            city=data.get("city"),
        )

    await state.clear()
    await safe_edit(callback.message, i18n("filters.saved"), main_menu(i18n))
    await callback.answer()


@router.callback_query(F.data == "flt:cancel")
async def cancel_wizard(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    await state.clear()
    await safe_edit(callback.message, i18n("filters.cancelled"), main_menu(i18n))
    await callback.answer()


# --------------------------------------------------------------------------
# saved filters
# --------------------------------------------------------------------------


@router.callback_query(F.data == "menu:subs")
async def show_subscriptions(
    callback: CallbackQuery, state: FSMContext, i18n: Translator, user_id: int
) -> None:
    await state.clear()
    await _render_subscriptions(callback, i18n, user_id)
    await callback.answer()


async def _render_subscriptions(callback: CallbackQuery, i18n: Translator, user_id: int) -> None:
    async with session_scope() as session:
        subs = await crud.list_subscriptions(session, user_id)

    if not subs:
        await safe_edit(
            callback.message,
            f"{i18n('subs.title')}\n\n{i18n('subs.empty')}",
            main_menu(i18n),
        )
        return

    await safe_edit(
        callback.message, render_subscriptions(subs, i18n), subscriptions_keyboard(i18n, subs)
    )


@router.callback_query(F.data.endswith(":toggle"), F.data.startswith("sub:"))
async def toggle_sub(callback: CallbackQuery, i18n: Translator, user_id: int) -> None:
    sub_id = int(callback.data.split(":")[1])

    async with session_scope() as session:
        # Scoped by user_id inside crud, so a replayed callback from
        # another chat cannot touch someone else's filter.
        active = await crud.toggle_subscription(session, sub_id, user_id)

    if active is None:
        await callback.answer(i18n("error.generic"), show_alert=True)
        return

    await callback.answer(i18n("subs.resumed") if active else i18n("subs.stopped"))
    await _render_subscriptions(callback, i18n, user_id)


@router.callback_query(F.data.endswith(":del"), F.data.startswith("sub:"))
async def delete_sub(callback: CallbackQuery, i18n: Translator, user_id: int) -> None:
    sub_id = int(callback.data.split(":")[1])

    async with session_scope() as session:
        deleted = await crud.delete_subscription(session, sub_id, user_id)

    if not deleted:
        await callback.answer(i18n("error.generic"), show_alert=True)
        return

    await callback.answer(i18n("subs.deleted"))
    await _render_subscriptions(callback, i18n, user_id)


@router.callback_query(F.data.endswith(":noop"))
async def noop(callback: CallbackQuery) -> None:
    """Label rows are buttons too; acknowledge so they don't spin."""
    await callback.answer()
