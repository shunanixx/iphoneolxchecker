"""Typed query helpers.

Handlers, the monitor and the AI cache all talk to the database through
this module rather than writing their own selects, so the matching and
cache-key rules live in exactly one place.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Analysis,
    Listing,
    Notification,
    SellerReviewsCache,
    Subscription,
    User,
    utcnow,
)

SELLER_REVIEWS_TTL = timedelta(hours=24)


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None = None,
    default_language: str = "uk",
) -> User:
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        user = User(tg_id=tg_id, username=username, language=default_language)
        session.add(user)
        await session.flush()
        return user

    # Usernames change; keep the row current but never touch language,
    # which the user owns via settings.
    if username and user.username != username:
        user.username = username
    return user


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def set_user_language(session: AsyncSession, tg_id: int, language: str) -> None:
    user = await get_user_by_tg_id(session, tg_id)
    if user is not None:
        user.language = language


# --------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------


async def create_subscription(
    session: AsyncSession,
    user_id: int,
    *,
    models: list[str],
    storages: list[str],
    price_min: int | None = None,
    price_max: int | None = None,
    city: str | None = None,
) -> Subscription:
    sub = Subscription(
        user_id=user_id,
        models=models,
        storages=storages,
        price_min=price_min,
        price_max=price_max,
        city=city,
        is_active=True,
    )
    session.add(sub)
    await session.flush()
    return sub


async def list_subscriptions(session: AsyncSession, user_id: int) -> list[Subscription]:
    result = await session.scalars(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
    )
    return list(result)


async def get_subscription(session: AsyncSession, sub_id: int) -> Subscription | None:
    return await session.get(Subscription, sub_id)


async def delete_subscription(session: AsyncSession, sub_id: int, user_id: int) -> bool:
    """Scoped by user_id so a forged callback can't delete someone else's row."""
    result = await session.execute(
        delete(Subscription).where(Subscription.id == sub_id, Subscription.user_id == user_id)
    )
    return bool(result.rowcount)


async def toggle_subscription(session: AsyncSession, sub_id: int, user_id: int) -> bool | None:
    sub = await session.scalar(
        select(Subscription).where(Subscription.id == sub_id, Subscription.user_id == user_id)
    )
    if sub is None:
        return None
    sub.is_active = not sub.is_active
    return sub.is_active


async def all_active_subscriptions(session: AsyncSession) -> list[Subscription]:
    result = await session.scalars(select(Subscription).where(Subscription.is_active.is_(True)))
    return list(result)


async def distinct_active_models(session: AsyncSession) -> set[str]:
    """Model keys referenced by any active subscription.

    The monitor runs one OLX search per model rather than per
    subscription — this is the fan-out reduction from §4 step 1.
    """
    subs = await all_active_subscriptions(session)
    keys: set[str] = set()
    for sub in subs:
        keys.update(sub.models or [])
    return keys


# --------------------------------------------------------------------------
# listings
# --------------------------------------------------------------------------


async def listings_by_olx_ids(session: AsyncSession, olx_ids: list[str]) -> dict[str, Listing]:
    """Map of the listings we already know, keyed by OLX id.

    The monitor needs the stored rows themselves, not just the ids, so it
    can spot an edited price or title without re-fetching every detail
    page it has already seen.
    """
    if not olx_ids:
        return {}
    result = await session.scalars(select(Listing).where(Listing.olx_id.in_(olx_ids)))
    return {listing.olx_id: listing for listing in result}


async def get_listing_by_olx_id(session: AsyncSession, olx_id: str) -> Listing | None:
    return await session.scalar(select(Listing).where(Listing.olx_id == olx_id))


async def get_listing(session: AsyncSession, listing_id: int) -> Listing | None:
    return await session.get(Listing, listing_id)


async def upsert_listing(session: AsyncSession, data: dict[str, Any]) -> Listing:
    """Insert a new listing or refresh the one we already have.

    Returns the persisted row. `content_hash` is overwritten on update,
    which is what invalidates a stale AI analysis.
    """
    olx_id = data["olx_id"]
    listing = await get_listing_by_olx_id(session, olx_id)

    if listing is None:
        listing = Listing(**data)
        session.add(listing)
        await session.flush()
        return listing

    for key, value in data.items():
        if key != "olx_id":
            setattr(listing, key, value)
    listing.last_seen_at = utcnow()
    await session.flush()
    return listing


async def touch_listing(session: AsyncSession, listing_id: int) -> None:
    listing = await session.get(Listing, listing_id)
    if listing is not None:
        listing.last_seen_at = utcnow()


# --------------------------------------------------------------------------
# analyses (AI cache)
# --------------------------------------------------------------------------


async def get_cached_analysis(
    session: AsyncSession, listing_id: int, content_hash: str
) -> Analysis | None:
    """The cache lookup every AI path must go through first."""
    return await session.scalar(
        select(Analysis).where(
            Analysis.listing_id == listing_id,
            Analysis.content_hash == content_hash,
        )
    )


async def save_analysis(
    session: AsyncSession,
    listing_id: int,
    content_hash: str,
    payload: dict[str, Any],
    raw_response: dict[str, Any] | None = None,
) -> Analysis:
    """Store an analysis, tolerating a concurrent writer for the same key."""
    values = {
        "listing_id": listing_id,
        "content_hash": content_hash,
        "phone_score": int(payload.get("phone_score", 0)),
        "seller_score": int(payload.get("seller_score", 0)),
        "short_verdict": payload.get("short_verdict", ""),
        "price_assessment": payload.get("price_assessment"),
        "condition_assessment": payload.get("condition_assessment"),
        "seller_assessment": payload.get("seller_assessment"),
        "risk_flags": payload.get("risk_flags") or [],
        "raw_response": raw_response,
    }
    stmt = (
        sqlite_insert(Analysis)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["listing_id", "content_hash"])
    )
    await session.execute(stmt)
    await session.flush()

    analysis = await get_cached_analysis(session, listing_id, content_hash)
    assert analysis is not None  # just inserted or already present
    return analysis


async def latest_analysis(session: AsyncSession, listing_id: int) -> Analysis | None:
    """Most recent analysis regardless of hash — for rendering old cards."""
    return await session.scalar(
        select(Analysis)
        .where(Analysis.listing_id == listing_id)
        .order_by(Analysis.created_at.desc())
        .limit(1)
    )


# --------------------------------------------------------------------------
# seller reviews cache
# --------------------------------------------------------------------------


async def get_seller_reviews(session: AsyncSession, profile_url: str) -> dict[str, Any] | None:
    row = await session.get(SellerReviewsCache, profile_url)
    if row is None:
        return None
    fetched_at = row.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - fetched_at > SELLER_REVIEWS_TTL:
        return None
    return row.reviews_json


async def save_seller_reviews(
    session: AsyncSession, profile_url: str, reviews: dict[str, Any]
) -> None:
    stmt = (
        sqlite_insert(SellerReviewsCache)
        .values(seller_profile_url=profile_url, reviews_json=reviews, fetched_at=utcnow())
        .on_conflict_do_update(
            index_elements=["seller_profile_url"],
            set_={"reviews_json": reviews, "fetched_at": utcnow()},
        )
    )
    await session.execute(stmt)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


async def notification_exists(session: AsyncSession, user_id: int, listing_id: int) -> bool:
    found = await session.scalar(
        select(Notification.id).where(
            Notification.user_id == user_id, Notification.listing_id == listing_id
        )
    )
    return found is not None


async def record_notification(
    session: AsyncSession, user_id: int, listing_id: int, subscription_id: int | None
) -> bool:
    """Log a delivery. Returns False if this user already got this listing.

    The UNIQUE(user_id, listing_id) constraint is the real guard — this
    helper just turns the conflict into a boolean instead of an error.
    """
    stmt = (
        sqlite_insert(Notification)
        .values(user_id=user_id, listing_id=listing_id, subscription_id=subscription_id)
        .on_conflict_do_nothing(index_elements=["user_id", "listing_id"])
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def notified_listing_ids(session: AsyncSession, user_id: int) -> set[int]:
    result = await session.scalars(
        select(Notification.listing_id).where(Notification.user_id == user_id)
    )
    return set(result)


async def count_notifications(session: AsyncSession, user_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(Notification.id)).where(Notification.user_id == user_id)
        )
    ) or 0
