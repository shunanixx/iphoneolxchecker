"""SQLAlchemy ORM models — the schema described in ARCHITECTURE.md §3.

The important invariant lives on `analyses`: `UNIQUE(listing_id,
content_hash)`. That pair is the AI cache key. When a seller edits the
price or description the listing's `content_hash` changes and a fresh
analysis is computed; otherwise every user matching the same listing
reuses one cached result, which is what keeps us inside the Gemini free
tier.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(2), default="uk")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    #: e.g. ["iphone_13", "iphone_14"] — keys from bot.constants.
    models: Mapped[list] = mapped_column(JSON, default=list)
    #: e.g. ["128", "256"]; empty list means "any storage".
    storages: Mapped[list] = mapped_column(JSON, default=list)
    price_min: Mapped[int | None] = mapped_column(Integer)
    price_max: Mapped[int | None] = mapped_column(Integer)
    city: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="subscriptions", lazy="selectin")


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    olx_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[int | None] = mapped_column(Integer, index=True)
    currency: Mapped[str | None] = mapped_column(String(8))
    city: Mapped[str | None] = mapped_column(String(128))

    description: Mapped[str | None] = mapped_column(Text)
    #: List of image URLs.
    photos: Mapped[list] = mapped_column(JSON, default=list)

    seller_name: Mapped[str | None] = mapped_column(String(128))
    seller_profile_url: Mapped[str | None] = mapped_column(Text)

    #: Detected by scraper/detector.py (regex first, AI fallback).
    model: Mapped[str | None] = mapped_column(String(32), index=True)
    storage: Mapped[str | None] = mapped_column(String(8), index=True)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    #: hash(title + description + price + photos) — changes when edited.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", lazy="selectin"
    )


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("listing_id", "content_hash", name="uq_analysis_cache_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    #: Must equal listings.content_hash for this row to be a cache hit.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    score: Mapped[int] = mapped_column(Integer)
    short_verdict: Mapped[str] = mapped_column(Text)
    price_assessment: Mapped[str | None] = mapped_column(Text)
    condition_assessment: Mapped[str | None] = mapped_column(Text)
    seller_assessment: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)
    raw_response: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="analyses", lazy="selectin")


class SellerReviewsCache(Base):
    __tablename__ = "seller_reviews_cache"

    seller_profile_url: Mapped[str] = mapped_column(Text, primary_key=True)
    reviews_json: Mapped[dict] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "listing_id", name="uq_notification_once_per_user"),
        Index("ix_notifications_user_sent", "user_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
