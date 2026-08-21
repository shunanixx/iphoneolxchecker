"""Settings — currently just the interface language switch."""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.db import crud
from bot.db.engine import session_scope
from bot.handlers.common import safe_edit
from bot.keyboards.menu import main_menu, settings_menu
from bot.middlewares.i18n import LOCALES, SUPPORTED_LANGUAGES, Translator

router = Router(name="settings")


@router.callback_query(F.data == "menu:settings")
async def show_settings(callback: CallbackQuery, i18n: Translator, language: str) -> None:
    text = i18n("settings.title", language=LOCALES[language].get("lang.name", language))
    await safe_edit(callback.message, text, settings_menu(i18n, language))
    await callback.answer()


@router.callback_query(F.data.startswith("set:lang:"))
async def change_language(callback: CallbackQuery) -> None:
    language = callback.data.split(":")[2]
    if language not in SUPPORTED_LANGUAGES:
        await callback.answer()
        return

    async with session_scope() as session:
        await crud.set_user_language(session, callback.from_user.id, language)

    # The Translator injected by the middleware still holds the *old*
    # language — this update was resolved before the change — so build a
    # fresh one rather than confirming the switch in the previous locale.
    i18n = Translator(language)
    await callback.answer(i18n("settings.language_changed"))
    await safe_edit(callback.message, i18n("menu.title"), main_menu(i18n))
