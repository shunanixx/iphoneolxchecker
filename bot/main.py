"""Entrypoint: wires the bot and the background monitor into one process.

The monitor is an `asyncio.create_task()` running alongside aiogram's
polling loop, sharing the event loop and the database (ARCHITECTURE.md
§1). That is a deliberate single-process design for this scope — the
module boundaries are what would let the monitor move to its own worker
later, not a message bus.
"""

import asyncio
import contextlib

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.ai.cache import AnalysisService
from bot.ai.gemini_client import GeminiClient
from bot.config import Settings, get_settings
from bot.db.engine import create_schema, dispose_engine, init_engine
from bot.handlers import build_router
from bot.middlewares.i18n import I18nMiddleware
from bot.scheduler.monitor import Monitor
from bot.scraper.olx_client import OLXClient
from bot.utils.logging import get_logger, setup_logging

log = get_logger(__name__)


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start / main menu"),
            BotCommand(command="menu", description="Main menu"),
            BotCommand(command="help", description="How this bot works"),
        ]
    )


def build_dispatcher(settings: Settings, monitor: Monitor, olx: OLXClient) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Outer middleware so it also runs for updates no handler matches —
    # every handler can then rely on `i18n` and `user_id` being present.
    dp.update.outer_middleware(I18nMiddleware(settings))

    # Injected into handlers by name via aiogram's workflow data.
    dp["monitor"] = monitor
    dp["olx"] = olx
    dp["settings"] = settings

    dp.include_router(build_router())
    return dp


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    init_engine(settings)
    await create_schema()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    olx = OLXClient(settings)
    monitor_task: asyncio.Task | None = None

    # Everything after this point holds an open aiohttp session (`bot`,
    # `olx`) and a DB engine — a failure anywhere below, including an
    # invalid BOT_TOKEN surfacing during `set_commands`, must still reach
    # the cleanup in `finally` rather than leak them.
    try:
        ai_client = GeminiClient(settings) if settings.ai_enabled else None
        if ai_client is None:
            log.warning("GEMINI_API_KEY is not set — listings will be delivered without AI scoring")

        analysis = AnalysisService(
            ai_client, image_loader=olx, max_photos=settings.gemini_max_photos
        )
        monitor = Monitor(bot, settings, olx, analysis)

        dp = build_dispatcher(settings, monitor, olx)
        await set_commands(bot)

        monitor_task = asyncio.create_task(monitor.run_forever(), name="monitor")

        log.info("starting polling")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
        await olx.close()
        await bot.session.close()
        await dispose_engine()
        log.info("shutdown complete")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("interrupted")


if __name__ == "__main__":
    main()
