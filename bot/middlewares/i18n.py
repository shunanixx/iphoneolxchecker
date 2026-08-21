"""Per-user translation, injected into every handler.

Handlers never import a locale file directly — they receive a
`Translator` as the `i18n` keyword argument and call `i18n("some.key")`.
That keeps the "every string exists in all three locales" rule
enforceable in one place (and testable: see tests/test_locales.py).

The middleware also resolves the database `User` row once per update, so
handlers get `user` for free instead of each one re-querying.
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser

from bot.config import Settings
from bot.db import crud
from bot.db.engine import session_scope
from bot.utils.logging import get_logger

log = get_logger(__name__)

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
SUPPORTED_LANGUAGES: tuple[str, ...] = ("uk", "ru", "en")


def load_locales() -> dict[str, dict[str, str]]:
    locales: dict[str, dict[str, str]] = {}
    for language in SUPPORTED_LANGUAGES:
        path = LOCALES_DIR / f"{language}.json"
        with path.open(encoding="utf-8") as handle:
            locales[language] = json.load(handle)
    return locales


LOCALES = load_locales()


class Translator:
    """Bound to one language; falls back to the default, then to the key."""

    __slots__ = ("language", "_strings", "_fallback")

    def __init__(self, language: str, fallback: str = "uk") -> None:
        self.language = language if language in LOCALES else fallback
        self._strings = LOCALES[self.language]
        self._fallback = LOCALES.get(fallback, {})

    def get(self, key: str) -> str:
        value = self._strings.get(key)
        if value is None:
            value = self._fallback.get(key)
        if value is None:
            # Showing the key beats showing nothing, and it makes the
            # missing translation obvious in a screenshot.
            log.warning("missing translation for %r (%s)", key, self.language)
            return key
        return value

    def __call__(self, key: str, **kwargs: Any) -> str:
        text = self.get(key)
        if not kwargs:
            return text
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError) as exc:
            log.warning("bad placeholder in %r (%s): %s", key, self.language, exc)
            return text


def guess_language(tg_user: TgUser | None, default: str) -> str:
    """Best first guess from Telegram's own locale, before the user picks."""
    code = (getattr(tg_user, "language_code", None) or "").lower()
    if code.startswith("uk"):
        return "uk"
    if code.startswith("ru") or code.startswith("be"):
        return "ru"
    if code.startswith("en"):
        return "en"
    return default


class I18nMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")

        if tg_user is None or tg_user.is_bot:
            data["i18n"] = Translator(self._settings.default_language)
            return await handler(event, data)

        async with session_scope() as session:
            user = await crud.get_or_create_user(
                session,
                tg_id=tg_user.id,
                username=tg_user.username,
                default_language=guess_language(tg_user, self._settings.default_language),
            )
            language = user.language
            user_id = user.id

        data["user_id"] = user_id
        data["language"] = language
        data["i18n"] = Translator(language, fallback=self._settings.default_language)
        return await handler(event, data)
