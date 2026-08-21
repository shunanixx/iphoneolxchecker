"""/start, /help, the main menu, and the on-demand search button."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.common import safe_edit
from bot.keyboards.menu import back_to_menu, main_menu
from bot.middlewares.i18n import Translator
from bot.scheduler.monitor import Monitor
from bot.utils.logging import get_logger

router = Router(name="start")
log = get_logger(__name__)

#: How many matches an on-demand search sends at once, so a first-time
#: user with a broad filter doesn't get a hundred messages.
SEARCH_RESULT_LIMIT = 10


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, i18n: Translator) -> None:
    await state.clear()
    name = message.from_user.first_name if message.from_user else ""
    await message.answer(i18n("start.greeting", name=name))
    await message.answer(i18n("menu.title"), reply_markup=main_menu(i18n))


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, i18n: Translator) -> None:
    await state.clear()
    await message.answer(i18n("menu.title"), reply_markup=main_menu(i18n))


@router.message(Command("help"))
async def cmd_help(message: Message, i18n: Translator) -> None:
    await message.answer(i18n("help.text"), reply_markup=back_to_menu(i18n))


@router.callback_query(F.data == "menu:main")
async def show_menu(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    await state.clear()
    await safe_edit(callback.message, i18n("menu.title"), main_menu(i18n))
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def show_help(callback: CallbackQuery, i18n: Translator) -> None:
    await safe_edit(callback.message, i18n("help.text"), back_to_menu(i18n))
    await callback.answer()


@router.callback_query(F.data == "menu:search")
async def search_now(
    callback: CallbackQuery,
    i18n: Translator,
    user_id: int,
    monitor: Monitor,
) -> None:
    """Run the monitor pipeline against just this user's filters.

    Same matching code as the background loop (ARCHITECTURE.md §4) — the
    only difference is the scope and that results are sent immediately.
    """
    if monitor.is_searching(user_id):
        await callback.answer(i18n("search.busy"), show_alert=True)
        return

    # The scrape takes a while; acknowledge before it starts so the
    # user's button doesn't spin until Telegram times the callback out.
    await callback.answer()
    await safe_edit(callback.message, i18n("search.started"))

    try:
        listing_ids = await monitor.search_for_user(user_id)
    except Exception as exc:
        log.exception("on-demand search failed for user %s: %s", user_id, exc)
        await callback.message.answer(i18n("error.generic"), reply_markup=main_menu(i18n))
        return

    if not listing_ids:
        await safe_edit(callback.message, i18n("search.none"), main_menu(i18n))
        return

    batch = listing_ids[:SEARCH_RESULT_LIMIT]
    await safe_edit(callback.message, i18n("search.found", count=len(batch)))
    await monitor.deliver_to_user(user_id, batch)
    await callback.message.answer(i18n("menu.title"), reply_markup=main_menu(i18n))
