"""Handler routers, registered in the order they should match."""

from aiogram import Router

from bot.handlers import filters, listings, settings, start


def build_router() -> Router:
    router = Router(name="root")
    router.include_router(start.router)
    router.include_router(settings.router)
    router.include_router(filters.router)
    router.include_router(listings.router)
    return router


__all__ = ["build_router"]
