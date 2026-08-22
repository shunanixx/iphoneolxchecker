"""Listing card and its sub-views.

Navigation here is stateless by design (ARCHITECTURE.md §7): every
callback is `lst:{id}:{action}` and carries everything the handler needs.
There is no server-side "current view" — `back` simply re-renders the
card for that id. Please keep it that way; it is what makes a card from
last week still work, and what stops two cards in one chat from
interfering with each other.
"""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InputMediaPhoto

from bot.db import crud
from bot.db.engine import session_scope
from bot.db.models import Listing
from bot.handlers.common import safe_edit
from bot.keyboards.listings import listing_card, listing_subview
from bot.middlewares.i18n import Translator
from bot.render import render_card, render_details, render_photo_caption, render_reviews
from bot.scraper.olx_client import BlockedError, FetchError, OLXClient
from bot.utils.logging import get_logger

router = Router(name="listings")
log = get_logger(__name__)

#: Telegram's media-group ceiling.
MAX_MEDIA_GROUP = 10


def _parse(callback_data: str) -> tuple[int, str] | None:
    parts = callback_data.split(":")
    if len(parts) != 3 or parts[0] != "lst":
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


async def _load(callback: CallbackQuery, i18n: Translator) -> Listing | None:
    parsed = _parse(callback.data or "")
    if parsed is None:
        await callback.answer()
        return None

    listing_id, _ = parsed
    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)

    if listing is None:
        await callback.answer(i18n("error.listing_gone"), show_alert=True)
        return None
    return listing


@router.callback_query(F.data.startswith("lst:"), F.data.endswith(":card"))
@router.callback_query(F.data.startswith("lst:"), F.data.endswith(":back"))
async def show_card(callback: CallbackQuery, i18n: Translator) -> None:
    """Both `card` and `back` render the same thing — that is the point."""
    listing = await _load(callback, i18n)
    if listing is None:
        return

    async with session_scope() as session:
        analysis = await crud.get_cached_analysis(session, listing.id, listing.content_hash)
        if analysis is None:
            # The listing was edited since we analysed it; showing the
            # previous verdict beats showing nothing.
            analysis = await crud.latest_analysis(session, listing.id)

    await safe_edit(
        callback.message,
        render_card(listing, analysis, i18n),
        listing_card(i18n, listing.id, listing.url),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lst:"), F.data.endswith(":details"))
async def show_details(callback: CallbackQuery, i18n: Translator) -> None:
    listing = await _load(callback, i18n)
    if listing is None:
        return

    async with session_scope() as session:
        analysis = await crud.get_cached_analysis(session, listing.id, listing.content_hash)
        if analysis is None:
            analysis = await crud.latest_analysis(session, listing.id)

    await safe_edit(
        callback.message,
        render_details(listing, analysis, i18n),
        listing_subview(i18n, listing.id, listing.url),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lst:"), F.data.endswith(":photos"))
async def show_photos(callback: CallbackQuery, i18n: Translator) -> None:
    listing = await _load(callback, i18n)
    if listing is None:
        return

    photos = list(listing.photos or [])
    if not photos:
        await callback.answer(i18n("photos.none"), show_alert=True)
        return

    await callback.answer()
    caption = render_photo_caption(listing, i18n)
    keyboard = listing_subview(i18n, listing.id, listing.url)

    try:
        if len(photos) == 1:
            # Telegram's sendMediaGroup rejects groups with fewer than 2
            # items outright — a single-photo listing (common; many OLX
            # sellers upload just one) has to go through answer_photo
            # instead, or the button silently "does nothing" every time.
            # Unlike a media group, a single sendPhoto *can* carry the
            # back button directly, so no follow-up message is needed.
            await callback.message.answer_photo(photos[0], caption=caption, reply_markup=keyboard)
            return
        # InputMediaPhoto is a frozen pydantic model in current aiogram —
        # the caption has to be set at construction time, not assigned
        # afterward (that raises pydantic's ValidationError, which isn't
        # a TelegramBadRequest and was crashing this handler outright for
        # every listing with 2+ photos).
        media = [
            InputMediaPhoto(media=url, caption=caption if index == 0 else None)
            for index, url in enumerate(photos[:MAX_MEDIA_GROUP])
        ]
        await callback.message.answer_media_group(media=media)
    except TelegramBadRequest as exc:
        # OLX CDN links occasionally 403 for Telegram's fetcher.
        log.warning("photo(s) failed for listing %s: %s", listing.id, exc)
        await callback.message.answer(i18n("error.generic"))
        return

    # A media group can't carry an inline keyboard, so the way back is a
    # follow-up message — still just `lst:{id}:back`.
    await callback.message.answer(render_photo_caption(listing, i18n), reply_markup=keyboard)


async def _resolve_seller_reviews(olx: OLXClient, listing: Listing) -> dict | None:
    """Cache lookup, then a live fetch — self-healing a stale profile URL.

    `seller_profile_url` is captured once, at ingestion time. OLX has
    changed how that URL is built before (the old scheme 404s outright —
    see parser.py's `_seller_profile_url_from_dom`), so any listing
    ingested before that fix still carries the broken one forever unless
    something re-resolves it. Rather than a one-off bulk migration, this
    repairs itself the first time a user actually asks to see reviews
    for that listing: on a fetch failure, re-fetch the listing's own
    detail page (which re-derives the URL fresh from the current page
    markup) and retry once with whatever it finds.
    """
    if not listing.seller_profile_url:
        return None

    async with session_scope() as session:
        cached = await crud.get_seller_reviews(session, listing.seller_profile_url)
    if cached is not None:
        return cached

    url = listing.seller_profile_url
    try:
        reviews = await olx.fetch_seller_reviews(url)
    except (BlockedError, FetchError) as exc:
        log.info("seller reviews fetch failed for %s, trying to refresh the URL: %s", url, exc)
        try:
            detail = await olx.fetch_detail(listing.url)
        except (BlockedError, FetchError):
            detail = None

        fresh_url = detail.seller_profile_url if detail else None
        if not fresh_url or fresh_url == url:
            return None

        try:
            reviews = await olx.fetch_seller_reviews(fresh_url)
        except (BlockedError, FetchError) as exc2:
            log.debug("refreshed seller URL still failed for %s: %s", fresh_url, exc2)
            return None

        async with session_scope() as session:
            stored = await crud.get_listing(session, listing.id)
            if stored is not None:
                stored.seller_profile_url = fresh_url
        url = fresh_url

    async with session_scope() as session:
        await crud.save_seller_reviews(session, url, reviews)
    return reviews


@router.callback_query(F.data.startswith("lst:"), F.data.endswith(":reviews"))
async def show_reviews(callback: CallbackQuery, i18n: Translator, olx: OLXClient) -> None:
    listing = await _load(callback, i18n)
    if listing is None:
        return

    reviews = None
    answered = False

    if listing.seller_profile_url:
        async with session_scope() as session:
            cached = await crud.get_seller_reviews(session, listing.seller_profile_url)

        if cached is not None:
            reviews = cached
        else:
            # Cache miss (or a stale/broken URL needing a self-heal
            # fetch) — acknowledge first, since either can outlast
            # Telegram's callback timeout.
            await callback.answer()
            answered = True
            reviews = await _resolve_seller_reviews(olx, listing)

    await safe_edit(
        callback.message,
        render_reviews(listing, reviews, i18n),
        listing_subview(i18n, listing.id, listing.url),
    )
    if not answered:
        await callback.answer()
