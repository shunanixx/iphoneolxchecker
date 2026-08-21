"""The AI cache — the only sanctioned door to Gemini.

Nothing should call `GeminiClient.analyze_listing` directly. Everything
goes through `AnalysisService.get_or_create`, which checks the
`(listing_id, content_hash)` cache first. That single rule is what makes
the bot viable on the free tier: one listing seen by forty subscribers
costs one API call, not forty.

On top of the database cache there is an in-process lock per cache key,
so if the monitor and an on-demand search hit the same fresh listing at
the same moment, only one of them actually calls the API and the other
waits for the result.
"""

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.ai.gemini_client import GeminiClient, GeminiError
from bot.db import crud
from bot.db.engine import session_scope
from bot.db.models import Analysis, Listing
from bot.utils.logging import get_logger

log = get_logger(__name__)


class AnalysisService:
    def __init__(
        self,
        ai_client: GeminiClient | None,
        image_loader: Any | None = None,
        max_photos: int = 4,
    ) -> None:
        """`image_loader` is any object with `async fetch_image(url) -> bytes|None`.

        In practice it is the `OLXClient`; injecting it keeps this module
        free of scraper imports and easy to test with a stub.
        """
        self._ai_client = ai_client
        self._image_loader = image_loader
        self._max_photos = max_photos
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._ai_client is not None

    async def _lock_for(self, key: tuple[int, str]) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def _release_lock(self, key: tuple[int, str]) -> None:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                self._locks.pop(key, None)

    async def get_cached(self, session: AsyncSession, listing: Listing) -> Analysis | None:
        return await crud.get_cached_analysis(session, listing.id, listing.content_hash)

    async def get_or_create(
        self,
        session: AsyncSession,
        listing: Listing,
        reviews: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Analysis | None:
        """Return a valid analysis, computing one only if the cache misses.

        Returns `None` when AI is disabled or the API call failed — the
        caller is expected to still deliver the listing, just unscored.
        """
        cached = await self.get_cached(session, listing)
        if cached is not None:
            log.debug("analysis cache hit for listing %s", listing.id)
            return cached

        if self._ai_client is None:
            return None

        key = (listing.id, listing.content_hash)
        lock = await self._lock_for(key)

        async with lock:
            # Another coroutine may have filled the cache while we waited.
            cached = await self.get_cached(session, listing)
            if cached is not None:
                return cached

            try:
                images = await self._load_images(listing)
                payload, raw = await self._ai_client.analyze_listing(
                    title=listing.title,
                    description=listing.description,
                    price=listing.price,
                    currency=listing.currency,
                    city=listing.city,
                    url=listing.url,
                    model=listing.model,
                    storage=listing.storage,
                    seller_name=listing.seller_name,
                    reviews=reviews,
                    params=params,
                    images=images,
                )
            except GeminiError as exc:
                log.warning("analysis failed for listing %s: %s", listing.id, exc)
                return None
            except Exception as exc:
                log.exception("unexpected analysis error for listing %s: %s", listing.id, exc)
                return None

            try:
                # Persist in a transaction of our own rather than the
                # caller's. The caller commits some time after we return,
                # but the lock is released the moment this block ends —
                # so writing into their session would let the next waiter
                # re-check the cache before the row was visible and pay
                # for a second identical API call.
                async with session_scope() as own_session:
                    analysis = await crud.save_analysis(
                        own_session, listing.id, listing.content_hash, payload, raw
                    )
                log.info(
                    "analysed listing %s (%s) -> score %s",
                    listing.id,
                    listing.olx_id,
                    analysis.score,
                )
                return analysis
            finally:
                await self._release_lock(key)

    async def _load_images(self, listing: Listing) -> list[bytes]:
        """Download the first N photos; failures just mean fewer images."""
        if self._image_loader is None or not listing.photos:
            return []

        urls = list(listing.photos)[: self._max_photos]
        results = await asyncio.gather(
            *(self._image_loader.fetch_image(url) for url in urls),
            return_exceptions=True,
        )
        return [item for item in results if isinstance(item, bytes) and item]

    async def classify(self, title: str, description: str = "") -> tuple[str | None, str | None]:
        """Detector fallback hook, passed to `detect_with_fallback`."""
        if self._ai_client is None:
            return None, None
        return await self._ai_client.classify_model_storage(title, description)
