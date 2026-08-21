"""Small helpers shared by the handler modules."""

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from bot.utils.logging import get_logger

log = get_logger(__name__)


async def safe_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit a message, tolerating Telegram's two routine complaints.

    "message is not modified" happens whenever a user taps the button
    that re-renders the view they are already on — normal with stateless
    navigation, not an error. Photo messages can't be edited into text at
    all, so those get a fresh message instead.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if "message is not modified" in detail:
            return
        if "no text in the message" in detail or "message can't be edited" in detail:
            await message.answer(text, reply_markup=reply_markup)
            return
        log.warning("edit failed: %s", exc)
        await message.answer(text, reply_markup=reply_markup)
