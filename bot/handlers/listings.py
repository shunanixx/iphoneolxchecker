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

    media = [InputMediaPhoto(media=url) for url in photos[:MAX_MEDIA_GROUP]]
    media[0].caption = render_photo_caption(listing, i18n)

    try:
        await callback.message.answer_media_group(media=media)
    except TelegramBadRequest as exc:
        # OLX CDN links occasionally 403 for Telegram's fetcher.
        log.warning("media group failed for listing %s: %s", listing.id, exc)
        await callback.message.answer(i18n("error.generic"))
        return

    # A media group can't carry an inline keyboard, so the way back is a
    # follow-up message — still just `lst:{id}:back`.
    await callback.message.answer(
        render_photo_caption(listing, i18n),
        reply_markup=listing_subview(i18n, listing.id, listing.url),
    )


@router.callback_query(F.data.startswith("lst:"), F.data.endswith(":reviews"))
async def show_reviews(callback: CallbackQuery, i18n: Translator, olx: OLXClient) -> None:
    listing = await _load(callback, i18n)
    if listing is None:
        return

    reviews = None
    answered = False

    if listing.seller_profile_url:
        async with session_scope() as session:
            reviews = await crud.get_seller_reviews(session, listing.seller_profile_url)

        if reviews is None:
            # Cache miss (or expired TTL) — fetch once and store, so the
            # next user viewing this seller pays nothing. Acknowledge the
            # callback first: the fetch can outlast Telegram's timeout.
            await callback.answer()
            answered = True
            try:
                reviews = await olx.fetch_seller_reviews(listing.seller_profile_url)
            except (BlockedError, FetchError) as exc:
                log.debug("live reviews fetch failed: %s", exc)
                reviews = None
            else:
                async with session_scope() as session:
                    await crud.save_seller_reviews(session, listing.seller_profile_url, reviews)

    await safe_edit(
        callback.message,
        render_reviews(listing, reviews, i18n),
        listing_subview(i18n, listing.id, listing.url),
    )
    if not answered:
        await callback.answer()
