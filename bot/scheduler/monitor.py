"""Background monitoring loop and subscription matching (ARCHITECTURE.md §4).

Two entry points share one pipeline:

* `run_forever()` — the background loop, polling every
  `POLL_INTERVAL_SEC` for every model any active subscription mentions.
* `search_for_user()` — the "🔍 Search now" button, the same pipeline
  scoped to one user's filters.

They must stay one code path. If manual search and background monitoring
diverge, users get different results from the same filter depending on
which one found the listing first.
"""

import asyncio
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ai.cache import AnalysisService
from bot.config import Settings
from bot.constants import MODELS_BY_KEY
from bot.db import crud
from bot.db.engine import session_scope
from bot.db.models import Listing, Subscription, User
from bot.keyboards.listings import listing_card
from bot.middlewares.i18n import Translator
from bot.render import render_card
from bot.scraper.detector import detect_with_fallback, looks_like_accessory
from bot.scraper.olx_client import BlockedError, FetchError, OLXClient
from bot.scraper.parser import ListingCard, compute_content_hash
from bot.utils.logging import get_logger

log = get_logger(__name__)

#: How many detail pages we pull per cycle. Caps both scrape volume and
#: AI spend on a burst of new listings.
MAX_NEW_PER_CYCLE = 40
#: Edited listings are re-analysed, so they cost API budget too — and
#: they matter less than new ones, hence the tighter cap.
MAX_EDITED_PER_CYCLE = 10
#: Pause between outgoing Telegram messages, to stay under flood limits.
SEND_DELAY = 0.05


def matches_subscription(listing: Listing, sub: Subscription) -> bool:
    """Does this listing satisfy this filter?

    The single definition of "a match", used by both the monitor and the
    on-demand search. An empty `storages` list means "any storage"; a
    `None` price bound means that side is open.
    """
    if not sub.is_active:
        return False

    models = sub.models or []
    if models and listing.model not in models:
        return False

    # A listing whose storage we could not detect only survives when the
    # filter itself is storage-agnostic (an empty list means "any").
    storages = sub.storages or []
    if storages and listing.storage not in storages:
        return False

    if sub.price_min is not None or sub.price_max is not None:
        # "Договірна" listings have no price and cannot be range-matched.
        if listing.price is None:
            return False
        if sub.price_min is not None and listing.price < sub.price_min:
            return False
        if sub.price_max is not None and listing.price > sub.price_max:
            return False

    if sub.city:
        if not listing.city:
            return False
        if sub.city.strip().lower() not in listing.city.strip().lower():
            return False

    return True


class Monitor:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        olx: OLXClient,
        analysis: AnalysisService,
    ) -> None:
        self._bot = bot
        self._settings = settings
        self._olx = olx
        self._analysis = analysis
        self._cycle_lock = asyncio.Lock()
        self._user_searches: set[int] = set()

    # ------------------------------------------------------------------
    # background loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Poll until cancelled. One bad cycle must never kill the loop."""
        log.info("monitor started (interval %ss)", self._settings.poll_interval_sec)
        while True:
            try:
                sent = await self.run_cycle()
                if sent:
                    log.info("cycle finished, %s notification(s) sent", sent)
            except asyncio.CancelledError:
                log.info("monitor stopped")
                raise
            except Exception as exc:
                log.exception("monitor cycle failed: %s", exc)

            await asyncio.sleep(self._settings.poll_interval_sec)

    async def run_cycle(self) -> int:
        """One full pass: scrape → ingest → analyse → notify."""
        if self._cycle_lock.locked():
            log.warning("previous cycle still running, skipping this tick")
            return 0

        async with self._cycle_lock:
            async with session_scope() as session:
                subs = await crud.all_active_subscriptions(session)

            model_keys: set[str] = set()
            for sub in subs:
                model_keys.update(sub.models or [])

            if not model_keys:
                log.debug("no active subscriptions, nothing to poll")
                return 0

            listing_ids = await self._collect(model_keys, subs)
            if not listing_ids:
                return 0

            return await self._dispatch(listing_ids, subs)

    # ------------------------------------------------------------------
    # steps 1-9: scrape and ingest
    # ------------------------------------------------------------------

    async def _collect(
        self,
        model_keys: set[str],
        subs: list[Subscription],
        city: str | None = None,
    ) -> list[int]:
        """Search each model, ingest whatever is new, return listing ids."""
        cards: dict[str, ListingCard] = {}

        for key in sorted(model_keys):
            model = MODELS_BY_KEY.get(key)
            if model is None:
                continue
            try:
                found = await self._olx.search_many(
                    model.query, pages=self._settings.olx_pages_per_model, city=city
                )
            except (BlockedError, FetchError) as exc:
                log.warning("search for %s failed: %s", key, exc)
                continue

            for card in found:
                cards.setdefault(card.olx_id, card)

        if not cards:
            return []

        if self._olx.looks_blocked:
            log.error(
                "OLX is consistently blocking the lightweight client; "
                "consider switching to the Playwright fetcher"
            )

        # Step 4: diff against what we already have.
        async with session_scope() as session:
            known = await crud.listings_by_olx_ids(session, list(cards))

        fresh: list[ListingCard] = []
        edited: list[ListingCard] = []
        for olx_id, card in cards.items():
            stored = known.get(olx_id)
            if stored is None:
                fresh.append(card)
            elif card.price != stored.price or card.title != stored.title:
                # The seller changed something we can see from the search
                # card. Re-ingesting recomputes `content_hash`, which is
                # what invalidates the cached analysis (§3) — without
                # this, an edited listing would keep its stale verdict
                # forever, because we never look at it again.
                edited.append(card)

        log.info("found %s listings, %s new, %s edited", len(cards), len(fresh), len(edited))

        ingested: list[int] = []
        for card in fresh[:MAX_NEW_PER_CYCLE] + edited[:MAX_EDITED_PER_CYCLE]:
            listing_id = await self._ingest(card, subs)
            if listing_id is not None:
                ingested.append(listing_id)

        return ingested

    async def _ingest(self, card: ListingCard, subs: list[Subscription]) -> int | None:
        """Steps 5-9 for a single listing: detail, detect, hash, store, analyse."""
        if looks_like_accessory(card.title):
            log.debug("skipping accessory/parts listing: %s", card.title[:60])
            return None

        try:
            detail = await self._olx.fetch_detail(card.url)
        except (BlockedError, FetchError) as exc:
            log.warning("detail fetch failed for %s: %s", card.url, exc)
            return None

        description = detail.description or ""
        if looks_like_accessory(description[:400]):
            return None

        detection = await detect_with_fallback(
            card.title,
            description,
            classifier=self._analysis.classify if self._analysis.enabled else None,
        )
        if detection.model is None:
            log.debug("no model detected, skipping: %s", card.title[:60])
            return None

        photos = detail.photos or ([card.thumbnail] if card.thumbnail else [])
        content_hash = compute_content_hash(card.title, description, card.price, photos)

        async with session_scope() as session:
            listing = await crud.upsert_listing(
                session,
                {
                    "olx_id": card.olx_id,
                    "url": card.url,
                    "title": card.title,
                    "price": card.price,
                    "currency": card.currency,
                    "city": card.city,
                    "description": description or None,
                    "photos": photos,
                    "seller_name": detail.seller_name,
                    "seller_profile_url": detail.seller_profile_url,
                    "model": detection.model,
                    "storage": detection.storage,
                    "posted_at": card.posted_at,
                    "content_hash": content_hash,
                },
            )
            listing_id = listing.id
            # Analysis is the only expensive step, so it is gated on the
            # listing actually being wanted by someone. Scoring a phone
            # no active filter matches would burn free-tier quota on a
            # card nobody will ever be sent.
            wanted = any(matches_subscription(listing, sub) for sub in subs)

        if not wanted:
            log.debug("stored %s without analysis: no active filter matches", card.olx_id)
            return listing_id

        # Step 7: seller reputation, cached separately for 24h.
        reviews = await self._seller_reviews(detail.seller_profile_url)

        # Step 9: analyse, but only ever through the cache.
        async with session_scope() as session:
            stored = await crud.get_listing(session, listing_id)
            if stored is not None:
                await self._analysis.get_or_create(
                    session, stored, reviews=reviews, params=detail.params
                )

        return listing_id

    async def _seller_reviews(self, profile_url: str | None) -> dict[str, Any] | None:
        """Step 7 — cached for 24h, since reputation barely moves day to day."""
        if not profile_url:
            return None

        async with session_scope() as session:
            cached = await crud.get_seller_reviews(session, profile_url)
        if cached is not None:
            return cached

        try:
            reviews = await self._olx.fetch_seller_reviews(profile_url)
        except (BlockedError, FetchError) as exc:
            log.debug("seller reviews fetch failed for %s: %s", profile_url, exc)
            return None

        async with session_scope() as session:
            await crud.save_seller_reviews(session, profile_url, reviews)
        return reviews

    # ------------------------------------------------------------------
    # step 10: match and notify
    # ------------------------------------------------------------------

    async def _dispatch(self, listing_ids: list[int], subs: list[Subscription]) -> int:
        sent = 0
        if not subs:
            return 0

        for listing_id in listing_ids:
            async with session_scope() as session:
                listing = await crud.get_listing(session, listing_id)
                if listing is None:
                    continue

                # First matching filter per user wins — one message per
                # listing per person, no matter how many filters overlap.
                targets: dict[int, Subscription] = {}
                for sub in subs:
                    if sub.user_id not in targets and matches_subscription(listing, sub):
                        targets[sub.user_id] = sub

                if not targets:
                    continue

                analysis = await self._analysis.get_cached(session, listing)

                for user_id, sub in targets.items():
                    user = await session.get(User, user_id)
                    if user is None:
                        continue
                    if await crud.notification_exists(session, user_id, listing.id):
                        continue

                    delivered = await self._send_card(user, listing, analysis)
                    if delivered:
                        await crud.record_notification(session, user_id, listing.id, sub.id)
                        sent += 1
                    await asyncio.sleep(SEND_DELAY)

        return sent

    async def _send_card(
        self, user: User, listing: Listing, analysis: Any, header: bool = True
    ) -> bool:
        i18n = Translator(user.language, fallback=self._settings.default_language)
        text = render_card(listing, analysis, i18n)
        if header:
            text = f"{i18n('notify.header')}\n\n{text}"

        try:
            await self._bot.send_message(
                chat_id=user.tg_id,
                text=text,
                reply_markup=listing_card(i18n, listing.id, listing.url),
                disable_web_page_preview=False,
            )
            return True
        except TelegramRetryAfter as exc:
            log.warning("flood limit, sleeping %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            return await self._send_card(user, listing, analysis, header=header)
        except TelegramForbiddenError:
            # User blocked the bot. Their filters stay, but there is no
            # point retrying every cycle.
            log.info("user %s blocked the bot, pausing their filters", user.tg_id)
            await self._pause_user_filters(user.id)
            return False
        except Exception as exc:
            log.warning("failed to send listing %s to %s: %s", listing.id, user.tg_id, exc)
            return False

    async def _pause_user_filters(self, user_id: int) -> None:
        async with session_scope() as session:
            for sub in await crud.list_subscriptions(session, user_id):
                sub.is_active = False

    # ------------------------------------------------------------------
    # on-demand search ("🔍 Search now")
    # ------------------------------------------------------------------

    def is_searching(self, user_id: int) -> bool:
        return user_id in self._user_searches

    async def search_for_user(self, user_id: int) -> list[int]:
        """Run the pipeline for one user's filters and return matching ids.

        Returns listings this user has not been notified about yet —
        including ones already in the database from a previous cycle, so
        a fresh subscriber immediately sees the current market rather
        than waiting for the next genuinely-new listing.
        """
        if user_id in self._user_searches:
            return []

        self._user_searches.add(user_id)
        try:
            async with session_scope() as session:
                subs = [
                    sub for sub in await crud.list_subscriptions(session, user_id) if sub.is_active
                ]

            if not subs:
                return []

            model_keys: set[str] = set()
            for sub in subs:
                model_keys.update(sub.models or [])
            if not model_keys:
                return []

            await self._collect(model_keys, subs)

            async with session_scope() as session:
                already = await crud.notified_listing_ids(session, user_id)
                candidates = await self._recent_matches(session, subs, already)
            return candidates
        finally:
            self._user_searches.discard(user_id)

    async def _recent_matches(
        self, session: AsyncSession, subs: list[Subscription], exclude: set[int]
    ) -> list[int]:
        model_keys: set[str] = set()
        for sub in subs:
            model_keys.update(sub.models or [])

        rows = await session.scalars(
            select(Listing)
            .where(Listing.model.in_(model_keys))
            .order_by(Listing.first_seen_at.desc())
            .limit(200)
        )

        matched: list[int] = []
        for listing in rows:
            if listing.id in exclude:
                continue
            if any(matches_subscription(listing, sub) for sub in subs):
                matched.append(listing.id)
        return matched

    async def deliver_to_user(self, user_id: int, listing_ids: list[int]) -> int:
        """Send specific listings to one user and log the notifications."""
        sent = 0
        async with session_scope() as session:
            user = await session.get(User, user_id)
            if user is None:
                return 0

            subs = [s for s in await crud.list_subscriptions(session, user_id) if s.is_active]

            for listing_id in listing_ids:
                listing = await crud.get_listing(session, listing_id)
                if listing is None:
                    continue

                sub = next((s for s in subs if matches_subscription(listing, s)), None)

                # An older listing may predate this user's filter and so
                # never have been analysed. Score it now rather than
                # sending them a blank card — still cache-first, and
                # bounded by the caller's result limit.
                reviews = None
                if listing.seller_profile_url:
                    reviews = await crud.get_seller_reviews(session, listing.seller_profile_url)
                analysis = await self._analysis.get_or_create(session, listing, reviews=reviews)

                if await self._send_card(user, listing, analysis, header=False):
                    await crud.record_notification(
                        session, user_id, listing.id, sub.id if sub else None
                    )
                    sent += 1
                await asyncio.sleep(SEND_DELAY)

        return sent
