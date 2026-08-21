"""HTTP access to OLX.

Fetching is split from parsing on purpose: `Fetcher` is the pluggable
strategy from ARCHITECTURE.md §6. `AiohttpFetcher` is the primary — cheap
enough to run continuously in the background — and `PlaywrightFetcher` is
the fallback for when the lightweight path starts getting blocked.
`OLXClient` itself only knows about the protocol, so swapping strategies
does not touch any calling code.

Politeness is not optional here: this is a scraper against a site with no
official API. Every request goes through `PoliteLimiter` (jittered delay,
capped concurrency, token bucket) and any 403/429 triggers exponential
global backoff.
"""

import asyncio
import random
from typing import Protocol
from urllib.parse import quote_plus, urlencode

import aiohttp

from bot.config import Settings
from bot.scraper.parser import (
    ListingCard,
    ListingDetail,
    parse_listing_detail,
    parse_search_results,
    parse_seller_reviews,
)
from bot.utils.logging import get_logger
from bot.utils.ratelimit import PoliteLimiter

log = get_logger(__name__)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
)

#: Consecutive block responses before we recommend the Playwright path.
BLOCK_STREAK_THRESHOLD = 5


class BlockedError(RuntimeError):
    """OLX answered with a block/CAPTCHA rather than content."""


class FetchError(RuntimeError):
    """The page could not be retrieved after retries."""


class Fetcher(Protocol):
    async def get(self, url: str) -> str: ...
    async def get_bytes(self, url: str) -> bytes: ...
    async def close(self) -> None: ...


class AiohttpFetcher:
    """Primary strategy: plain HTTP, no browser."""

    def __init__(self, settings: Settings, limiter: PoliteLimiter) -> None:
        self._settings = settings
        self._limiter = limiter
        self._session: aiohttp.ClientSession | None = None
        self.block_streak = 0

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
                    "Cache-Control": "no-cache",
                },
            )
        return self._session

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": random.choice(USER_AGENTS)}

    async def get(self, url: str, *, attempts: int = 3) -> str:
        session = await self._ensure_session()

        for attempt in range(1, attempts + 1):
            async with self._limiter:
                try:
                    async with session.get(url, headers=self._headers()) as response:
                        if response.status in (403, 429):
                            # Back off for everyone, not just this call —
                            # the block is against the IP, not the request.
                            backoff = min(300.0, 5.0 * (2 ** (attempt - 1)))
                            self.block_streak += 1
                            await self._limiter.penalize(backoff)
                            log.warning(
                                "OLX returned %s for %s (attempt %s/%s), backing off %.0fs",
                                response.status,
                                url,
                                attempt,
                                attempts,
                                backoff,
                            )
                            if attempt == attempts:
                                raise BlockedError(f"HTTP {response.status} for {url}")
                            continue

                        if response.status >= 500:
                            if attempt == attempts:
                                raise FetchError(f"HTTP {response.status} for {url}")
                            await asyncio.sleep(2.0 * attempt)
                            continue

                        if response.status == 404:
                            raise FetchError(f"HTTP 404 for {url}")

                        response.raise_for_status()
                        html = await response.text()
                        self.block_streak = 0
                        return html

                except (TimeoutError, aiohttp.ClientError) as exc:
                    if attempt == attempts:
                        raise FetchError(f"{type(exc).__name__} for {url}: {exc}") from exc
                    await asyncio.sleep(2.0 * attempt)

        raise FetchError(f"exhausted retries for {url}")

    async def get_bytes(self, url: str) -> bytes:
        session = await self._ensure_session()
        async with self._limiter, session.get(url, headers=self._headers()) as response:
            response.raise_for_status()
            return await response.read()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


class PlaywrightFetcher:
    """Fallback strategy for when the lightweight client gets blocked.

    Deliberately opt-in (`USE_PLAYWRIGHT`-style flag / manual switch)
    rather than automatic — automating the escalation is a backlog item,
    not current behaviour. Playwright is an optional dependency, so the
    import happens on first use.
    """

    def __init__(self, settings: Settings, limiter: PoliteLimiter) -> None:
        self._settings = settings
        self._limiter = limiter
        self._browser = None
        self._playwright = None

    async def _ensure_browser(self):  # type: ignore[no-untyped-def]
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise FetchError(
                "Playwright fallback requested but playwright is not installed; "
                "run `pip install playwright && playwright install chromium`"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def get(self, url: str) -> str:
        browser = await self._ensure_browser()
        async with self._limiter:
            context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
            try:
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return await page.content()
            finally:
                await context.close()

    async def get_bytes(self, url: str) -> bytes:
        browser = await self._ensure_browser()
        async with self._limiter:
            context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
            try:
                response = await context.request.get(url)
                return await response.body()
            finally:
                await context.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


class OLXClient:
    """Search and detail-page access, independent of the fetch strategy."""

    def __init__(self, settings: Settings, fetcher: Fetcher | None = None) -> None:
        self._settings = settings
        self._limiter = PoliteLimiter(
            rate=30,
            period=60.0,
            concurrency=settings.olx_max_concurrency,
            min_delay=settings.olx_min_delay_sec,
            max_delay=settings.olx_max_delay_sec,
        )
        self._fetcher: Fetcher = fetcher or AiohttpFetcher(settings, self._limiter)

    @property
    def base_url(self) -> str:
        return self._settings.olx_base_url.rstrip("/")

    @property
    def looks_blocked(self) -> bool:
        """True once blocks are persistent enough to justify Playwright."""
        streak = getattr(self._fetcher, "block_streak", 0)
        return streak >= BLOCK_STREAK_THRESHOLD

    def build_search_url(self, query: str, page: int = 1, city: str | None = None) -> str:
        """Newest-first search within the phones category.

        The category slug is `mobilnye-telefony-smartfony` — OLX renamed
        it from the older `mobilnye-telefony` at some point; the old slug
        now 404s outright. This is exactly the kind of markup/URL drift
        ARCHITECTURE.md §9 calls out as the parser's ongoing maintenance
        burden — if search starts silently 404ing again, check the
        current subcategory slug at olx.ua/uk/elektronika/telefony-i-aksesuary/
        before assuming anything else broke.
        """
        path = f"{self.base_url}/uk/elektronika/telefony-i-aksesuary/mobilnye-telefony-smartfony/"
        if city:
            path = f"{path}{quote_plus(city.lower())}/"

        params: dict[str, str] = {"q": query, "search[order]": "created_at:desc"}
        if page > 1:
            params["page"] = str(page)
        return f"{path}?{urlencode(params)}"

    async def search(
        self, query: str, *, page: int = 1, city: str | None = None
    ) -> list[ListingCard]:
        url = self.build_search_url(query, page=page, city=city)
        log.debug("OLX search: %s", url)
        html = await self._fetcher.get(url)
        cards = parse_search_results(html, self.base_url)
        log.debug("OLX search %r page %s -> %s cards", query, page, len(cards))
        return cards

    async def search_many(
        self, query: str, *, pages: int = 1, city: str | None = None
    ) -> list[ListingCard]:
        """Paginate a single query, de-duplicated by `olx_id`."""
        seen: set[str] = set()
        collected: list[ListingCard] = []
        for page in range(1, max(1, pages) + 1):
            try:
                cards = await self.search(query, page=page, city=city)
            except (BlockedError, FetchError) as exc:
                log.warning("search %r page %s failed: %s", query, page, exc)
                break
            if not cards:
                break
            for card in cards:
                if card.olx_id not in seen:
                    seen.add(card.olx_id)
                    collected.append(card)
        return collected

    async def fetch_detail(self, url: str) -> ListingDetail:
        html = await self._fetcher.get(url)
        return parse_listing_detail(html, self.base_url)

    async def fetch_seller_reviews(self, profile_url: str) -> dict:
        html = await self._fetcher.get(profile_url)
        return parse_seller_reviews(html)

    async def fetch_image(self, url: str) -> bytes | None:
        """Download a listing photo for multimodal analysis; never fatal."""
        try:
            return await self._fetcher.get_bytes(url)
        except Exception as exc:
            log.debug("image download failed for %s: %s", url, exc)
            return None

    async def close(self) -> None:
        await self._fetcher.close()

    async def __aenter__(self) -> "OLXClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()
