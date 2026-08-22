"""Message rendering shared by the handlers and the background monitor.

This lives outside `handlers/` on purpose: the monitor sends the very
same listing card that the on-demand search does, and duplicating the
formatting in two places is how the two paths would silently drift apart.

All output is Telegram HTML, so anything coming from OLX or from the
model is escaped before it reaches a message.
"""

from html import escape
from typing import Any

from bot.constants import model_title, storage_title
from bot.db.models import Analysis, Listing, Subscription
from bot.middlewares.i18n import Translator

#: Telegram's hard cap is 4096; leave room for the header we prepend.
MESSAGE_LIMIT = 3800
#: Captions on photos are much tighter than plain messages.
CAPTION_LIMIT = 1000


def esc(value: Any) -> str:
    return escape(str(value), quote=False)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def score_emoji(score: int) -> str:
    """Traffic-light prefix so the verdict reads at a glance in a feed."""
    if score >= 8:
        return "🟢"
    if score >= 6:
        return "🟡"
    if score >= 4:
        return "🟠"
    return "🔴"


def format_price(price: int | None, currency: str | None, i18n: Translator) -> str:
    if price is None:
        return i18n("value.not_set")
    amount = f"{price:,}".replace(",", " ")
    return f"{amount} {currency or 'UAH'}"


def format_price_range(price_min: int | None, price_max: int | None, i18n: Translator) -> str:
    if price_min is None and price_max is None:
        return i18n("value.any")
    if price_min is not None and price_max is not None:
        return i18n(
            "value.price_range",
            min=f"{price_min:,}".replace(",", " "),
            max=f"{price_max:,}".replace(",", " "),
        )
    if price_min is not None:
        return i18n("value.price_from", min=f"{price_min:,}".replace(",", " "))
    return i18n("value.price_to", max=f"{price_max:,}".replace(",", " "))


# --------------------------------------------------------------------------
# listing card + sub-views
# --------------------------------------------------------------------------


def render_card(listing: Listing, analysis: Analysis | None, i18n: Translator) -> str:
    """The short card: what it is, what it costs, and the AI's one-liner."""
    lines = [i18n("card.title", title=esc(truncate(listing.title, 180)))]

    lines.append(i18n("card.price", price=esc(format_price(listing.price, listing.currency, i18n))))

    if listing.model or listing.storage:
        lines.append(
            i18n(
                "card.specs",
                model=esc(model_title(listing.model)) if listing.model else i18n("value.unknown"),
                storage=esc(storage_title(listing.storage))
                if listing.storage
                else i18n("value.unknown"),
            )
        )

    if listing.city:
        lines.append(i18n("card.location", city=esc(listing.city)))

    lines.append("")

    if analysis is None:
        lines.append(i18n("card.no_analysis"))
        return "\n".join(lines)

    lines.append(
        f"{score_emoji(analysis.phone_score)} "
        + i18n("card.phone_score", score=analysis.phone_score)
    )
    lines.append(
        f"{score_emoji(analysis.seller_score)} "
        + i18n("card.seller_score", score=analysis.seller_score)
    )
    if analysis.short_verdict:
        lines.append(i18n("card.verdict", verdict=esc(analysis.short_verdict)))

    if analysis.risk_flags:
        flags = ", ".join(esc(flag) for flag in analysis.risk_flags[:3])
        lines.append(i18n("card.risks", flags=flags))

    return truncate("\n".join(lines), MESSAGE_LIMIT)


def render_details(listing: Listing, analysis: Analysis | None, i18n: Translator) -> str:
    """The full AI breakdown behind the Details button."""
    header = i18n("details.title", title=esc(truncate(listing.title, 120)))

    if analysis is None:
        return f"{header}\n\n{i18n('details.missing')}"

    blocks = [
        header,
        "",
        f"{score_emoji(analysis.phone_score)} "
        + i18n("details.phone_score", score=analysis.phone_score),
        f"{score_emoji(analysis.seller_score)} "
        + i18n("details.seller_score", score=analysis.seller_score),
    ]

    for key, text in (
        ("details.price", analysis.price_assessment),
        ("details.condition", analysis.condition_assessment),
        ("details.seller", analysis.seller_assessment),
    ):
        if text:
            blocks.append("")
            blocks.append(i18n(key, text=esc(text)))

    blocks.append("")
    if analysis.risk_flags:
        risks = "\n".join(f"• {esc(flag)}" for flag in analysis.risk_flags)
    else:
        risks = i18n("details.no_risks")
    blocks.append(i18n("details.risks", text=risks))

    return truncate("\n".join(blocks), MESSAGE_LIMIT)


def render_reviews(listing: Listing, reviews: dict[str, Any] | None, i18n: Translator) -> str:
    """Seller reputation, tolerant of OLX exposing very little of it."""
    lines = [i18n("reviews.title", name=esc(listing.seller_name or i18n("value.unknown")))]

    if not reviews:
        lines.extend(["", i18n("reviews.none")])
        return "\n".join(lines)

    lines.append("")
    has_data = False

    if reviews.get("rating") is not None:
        lines.append(i18n("reviews.rating", rating=esc(reviews["rating"])))
        has_data = True
    if reviews.get("reviews_count") is not None:
        lines.append(i18n("reviews.count", count=esc(reviews["reviews_count"])))
        has_data = True
    if reviews.get("since"):
        lines.append(i18n("reviews.since", since=esc(reviews["since"])))
        has_data = True

    texts = reviews.get("reviews") or []
    if texts:
        has_data = True
        lines.extend(["", i18n("reviews.list")])
        lines.extend(f"• {esc(truncate(text, 300))}" for text in texts[:5])

    if not has_data:
        lines.append(i18n("reviews.none"))

    return truncate("\n".join(lines), MESSAGE_LIMIT)


def render_photo_caption(listing: Listing, i18n: Translator) -> str:
    return truncate(i18n("photos.caption", title=esc(truncate(listing.title, 120))), CAPTION_LIMIT)


# --------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------


def render_subscription(sub: Subscription, index: int, i18n: Translator) -> str:
    models = sub.models or []
    if models:
        shown = ", ".join(esc(model_title(key)) for key in models[:3])
        if len(models) > 3:
            shown = f"{shown} +{len(models) - 3}"
    else:
        shown = i18n("value.any")

    storages = sub.storages or []
    storages_text = (
        ", ".join(esc(storage_title(item)) for item in storages) if storages else i18n("value.any")
    )

    return i18n(
        "subs.item",
        index=index,
        status=i18n("subs.active") if sub.is_active else i18n("subs.paused"),
        models=shown,
        storages=storages_text,
        price=esc(format_price_range(sub.price_min, sub.price_max, i18n)),
        city=esc(sub.city) if sub.city else i18n("value.any"),
    )


def render_subscriptions(subs: list[Subscription], i18n: Translator) -> str:
    if not subs:
        return f"{i18n('subs.title')}\n\n{i18n('subs.empty')}"
    body = "\n".join(render_subscription(sub, index, i18n) for index, sub in enumerate(subs, 1))
    return f"{i18n('subs.title')}\n\n{body}"


def render_filter_summary(data: dict[str, Any], i18n: Translator) -> str:
    """Preview of the wizard's collected answers before saving."""
    models = data.get("models") or []
    storages = data.get("storages") or []

    return i18n(
        "filters.summary",
        models=", ".join(esc(model_title(key)) for key in models) or i18n("value.any"),
        storages=", ".join(esc(storage_title(item)) for item in storages) or i18n("value.any"),
        price=esc(format_price_range(data.get("price_min"), data.get("price_max"), i18n)),
        city=esc(data["city"]) if data.get("city") else i18n("value.any"),
    )
