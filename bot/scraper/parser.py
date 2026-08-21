"""Extraction of listing data from OLX pages.

Strategy (ARCHITECTURE.md §6): OLX server-renders its pages and ships the
same data as JSON inside a `<script>` tag. Reading that embedded state is
far cheaper than a headless browser and much less brittle than scraping
rendered markup — so we try JSON first and only fall back to CSS
selectors when the page shape has changed.

This is the module most likely to break when OLX changes their markup.
Every extractor here is written to degrade rather than raise: a missing
field comes back as `None` and the listing still flows through the
pipeline.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from bot.utils.logging import get_logger

log = get_logger(__name__)

#: The embedded-state globals OLX has used; tried in order.
_STATE_PATTERNS = (
    re.compile(r"window\.__PRERENDERED_STATE__\s*=\s*(?P<json>\"(?:\\.|[^\"\\])*\")\s*;", re.S),
    re.compile(r"window\.__PRERENDERED_STATE__\s*=\s*(?P<json>\{.*?\})\s*;\s*window\.", re.S),
)

_PRICE_RE = re.compile(r"(\d[\d\s\u00a0.,]*)")


@dataclass
class ListingCard:
    """A listing as it appears in search results — cheap, no detail fetch."""

    olx_id: str
    title: str
    url: str
    price: int | None = None
    currency: str | None = None
    city: str | None = None
    thumbnail: str | None = None
    posted_at: datetime | None = None


@dataclass
class ListingDetail:
    """The extra fields that only exist on the listing's own page."""

    description: str | None = None
    photos: list[str] = field(default_factory=list)
    seller_name: str | None = None
    seller_profile_url: str | None = None
    #: Structured attributes OLX shows as chips ("Стан: Вживані").
    params: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def parse_price(raw: Any) -> tuple[int | None, str | None]:
    """Normalise OLX's many price shapes into `(amount, currency)`.

    Handles the numeric JSON form and the display string form
    ("12 500 грн", "450 $"). Returns `(None, None)` for "Обмін"/"Договірна".
    """
    if raw is None:
        return None, None

    if isinstance(raw, int | float):
        return int(raw), None

    if isinstance(raw, dict):
        # `regularPrice` is a real OLX field that can be present but
        # `None` (not just absent) — a plain `.get(key, {})` default only
        # covers the missing-key case, so an explicit `or {}` is needed
        # to avoid `NoneType has no attribute 'get'` on live data. Live
        # search results nest both the numeric value and the currency
        # code under `regularPrice` (`{"regularPrice": {"value": 19000,
        # "currencyCode": "UAH"}}`) rather than at the top level.
        regular = raw.get("regularPrice") or {}
        value = raw.get("value") or regular.get("value")
        currency = raw.get("currency") or raw.get("currencyCode") or regular.get("currencyCode")
        if isinstance(currency, dict):
            currency = currency.get("code")
        if isinstance(value, int | float):
            return int(value), currency
        raw = raw.get("displayValue") or raw.get("label") or ""

    text = str(raw)
    match = _PRICE_RE.search(text)
    if not match:
        return None, None

    digits = re.sub(r"[^\d]", "", match.group(1))
    if not digits:
        return None, None

    currency = None
    lowered = text.lower()
    if "грн" in lowered or "uah" in lowered:
        currency = "UAH"
    elif "$" in text or "usd" in lowered:
        currency = "USD"
    elif "€" in text or "eur" in lowered:
        currency = "EUR"

    return int(digits), currency


def compute_content_hash(
    title: str | None,
    description: str | None,
    price: int | None,
    photos: list[str] | None,
) -> str:
    """Fingerprint of everything the AI actually looks at.

    If a seller edits the price, the text, or the photo set, this changes
    and the cached analysis for the old hash stops being a hit — which is
    exactly the invalidation rule in ARCHITECTURE.md §3.
    """
    payload = json.dumps(
        {
            "title": (title or "").strip(),
            "description": (description or "").strip(),
            "price": price,
            "photos": sorted(photos or []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_state(html: str) -> dict[str, Any] | None:
    """Pull and decode the embedded JSON state, if the page still has one."""
    for pattern in _STATE_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        blob = match.group("json")
        try:
            # The value is usually a JSON *string* containing JSON.
            decoded = json.loads(blob)
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
            if isinstance(decoded, dict):
                return decoded
        except (json.JSONDecodeError, ValueError):
            continue

    # Next.js-style payload, used on some OLX surfaces.
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            data = json.loads(tag.string)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _walk_for_ads(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find the ad array inside the state blob without hardcoding its path.

    OLX has moved this between `listing.listing.ads`, `listing.ads` and
    `pageProps` over time; searching for the shape is more durable than
    tracking the path.
    """
    if depth > 8:
        return []

    if isinstance(node, list):
        looks_like_ads = (
            len(node) > 0
            and all(isinstance(item, dict) for item in node[:3])
            and all("id" in item and ("title" in item or "url" in item) for item in node[:3])
        )
        if looks_like_ads:
            return [item for item in node if isinstance(item, dict)]
        for item in node:
            found = _walk_for_ads(item, depth + 1)
            if found:
                return found
        return []

    if isinstance(node, dict):
        for key in ("ads", "items", "listings"):
            if key in node:
                found = _walk_for_ads(node[key], depth + 1)
                if found:
                    return found
        for value in node.values():
            found = _walk_for_ads(value, depth + 1)
            if found:
                return found
    return []


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _city_from_ad(ad: dict[str, Any]) -> str | None:
    location = ad.get("location")
    if isinstance(location, dict):
        # Live search results carry the city as the flat `cityName`
        # string (`{"cityName": "Хуст", "cityId": 253, ...}`); older/other
        # shapes nested it as `city: {name: ...}` or a bare string — kept
        # as fallbacks in case a different surface still sends those.
        city_name = location.get("cityName")
        if isinstance(city_name, str) and city_name:
            return city_name

        city = location.get("city")
        if isinstance(city, dict):
            return city.get("name")
        if isinstance(city, str):
            return city
    return ad.get("city") if isinstance(ad.get("city"), str) else None


def _photos_from_ad(ad: dict[str, Any], limit: int | None = None) -> list[str]:
    photos: list[str] = []
    for photo in ad.get("photos") or []:
        url: str | None = None
        if isinstance(photo, str):
            url = photo
        elif isinstance(photo, dict):
            url = photo.get("link") or photo.get("url") or photo.get("filename")
            if url and "{width}" in url:
                # OLX templates the CDN URL; ask for a size the model can read.
                url = url.replace("{width}", "1000").replace("{height}", "1000")
        if url:
            photos.append(url)
        if limit and len(photos) >= limit:
            break
    return photos


# --------------------------------------------------------------------------
# search results
# --------------------------------------------------------------------------


def parse_search_results(html: str, base_url: str = "https://www.olx.ua") -> list[ListingCard]:
    """Parse a search page into listing cards, newest first as OLX returns them."""
    state = _extract_state(html)
    if state:
        cards = _cards_from_state(state, base_url)
        if cards:
            return cards
        log.debug("embedded state present but held no ads; falling back to DOM")

    return _cards_from_dom(html, base_url)


def _cards_from_state(state: dict[str, Any], base_url: str) -> list[ListingCard]:
    cards: list[ListingCard] = []
    for ad in _walk_for_ads(state):
        olx_id = ad.get("id")
        title = ad.get("title")
        if olx_id is None or not title:
            continue

        url = ad.get("url") or ""
        if url and not url.startswith("http"):
            url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"

        price, currency = parse_price(ad.get("price"))
        photos = _photos_from_ad(ad, limit=1)

        cards.append(
            ListingCard(
                olx_id=str(olx_id),
                title=str(title).strip(),
                url=url,
                price=price,
                currency=currency,
                city=_city_from_ad(ad),
                thumbnail=photos[0] if photos else None,
                posted_at=_parse_iso(ad.get("createdTime") or ad.get("lastRefreshTime")),
            )
        )
    return cards


def _cards_from_dom(html: str, base_url: str) -> list[ListingCard]:
    """CSS-selector fallback for when the embedded state disappears."""
    soup = BeautifulSoup(html, "lxml")
    cards: list[ListingCard] = []

    for node in soup.select('[data-cy="l-card"]'):
        link = node.find("a", href=True)
        if not link:
            continue

        href = link["href"]
        url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"

        olx_id = node.get("id") or _olx_id_from_url(url)
        if not olx_id:
            continue

        title_node = node.select_one("h4, h6, [data-cy='ad-card-title']")
        title = title_node.get_text(strip=True) if title_node else ""
        if not title:
            continue

        price_node = node.select_one('[data-testid="ad-price"]')
        price, currency = parse_price(price_node.get_text(strip=True) if price_node else None)

        location_node = node.select_one('[data-testid="location-date"]')
        city = None
        if location_node:
            city = location_node.get_text(strip=True).split(" - ")[0].strip() or None

        img = node.find("img")
        thumbnail = img.get("src") if img else None

        cards.append(
            ListingCard(
                olx_id=str(olx_id),
                title=title,
                url=url,
                price=price,
                currency=currency,
                city=city,
                thumbnail=thumbnail,
            )
        )
    return cards


def _olx_id_from_url(url: str) -> str | None:
    """OLX slugs end in `-ID<digits>.html`."""
    match = re.search(r"-ID([0-9A-Za-z]+)\.html", url)
    if match:
        return match.group(1)
    match = re.search(r"/obyavlenie/.*?(\d{6,})", url)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# detail page
# --------------------------------------------------------------------------


def parse_listing_detail(html: str, base_url: str = "https://www.olx.ua") -> ListingDetail:
    state = _extract_state(html)
    if state:
        detail = _detail_from_state(state, base_url)
        if detail and (detail.description or detail.photos):
            return detail
    return _detail_from_dom(html, base_url)


def _find_ad_object(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """Locate the single-ad object in the detail page's state blob."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        if "description" in node and ("id" in node or "url" in node):
            return node
        for value in node.values():
            found = _find_ad_object(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_ad_object(item, depth + 1)
            if found:
                return found
    return None


def _detail_from_state(state: dict[str, Any], base_url: str) -> ListingDetail | None:
    ad = _find_ad_object(state)
    if ad is None:
        return None

    description = ad.get("description")
    if isinstance(description, str):
        # OLX stores the description with HTML line breaks.
        description = BeautifulSoup(description, "lxml").get_text("\n", strip=True)

    seller_name = None
    seller_url = None
    user = ad.get("user") or ad.get("seller") or {}
    if isinstance(user, dict):
        seller_name = user.get("name")
        seller_url = user.get("otherAdsUrl") or user.get("url") or user.get("profileUrl")
        if not seller_url and user.get("id"):
            seller_url = f"{base_url.rstrip('/')}/uk/list/user/{user['id']}/"

    params: dict[str, str] = {}
    for param in ad.get("params") or []:
        if not isinstance(param, dict):
            continue
        name = param.get("name") or param.get("key")
        value = param.get("value")
        if isinstance(value, dict):
            value = value.get("label") or value.get("key")
        if name and isinstance(value, str):
            params[str(name)] = value

    return ListingDetail(
        description=description,
        photos=_photos_from_ad(ad),
        seller_name=seller_name,
        seller_profile_url=seller_url,
        params=params,
    )


def _detail_from_dom(html: str, base_url: str) -> ListingDetail:
    soup = BeautifulSoup(html, "lxml")

    desc_node = soup.select_one('[data-cy="ad_description"]')
    description = desc_node.get_text("\n", strip=True) if desc_node else None

    photos: list[str] = []
    for img in soup.select('[data-testid="ad-photo"] img, [data-cy="adPhotos-swiperSlide"] img'):
        src = img.get("src") or img.get("data-src")
        if src and src not in photos:
            photos.append(src)

    seller_node = soup.select_one('[data-cy="seller_card"]')
    seller_name = None
    seller_url = None
    if seller_node:
        name_node = seller_node.select_one("h4, h3, a")
        seller_name = name_node.get_text(strip=True) if name_node else None
        link = seller_node.find("a", href=True)
        if link:
            href = link["href"]
            seller_url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"

    params: dict[str, str] = {}
    for chip in soup.select('[data-testid="ad-parameters-container"] p, ul.css-sfcl1s li'):
        text = chip.get_text(":", strip=True)
        if ":" in text:
            key, _, value = text.partition(":")
            params[key.strip()] = value.strip()

    return ListingDetail(
        description=description,
        photos=photos,
        seller_name=seller_name,
        seller_profile_url=seller_url,
        params=params,
    )


# --------------------------------------------------------------------------
# seller reviews
# --------------------------------------------------------------------------


def parse_seller_reviews(html: str) -> dict[str, Any]:
    """Best-effort seller reputation summary.

    OLX exposes seller reputation inconsistently between regions and
    account types, so this returns whatever it can find and an empty
    review list is a normal, expected result — the AI prompt is written
    to cope with "no reviews available".
    """
    result: dict[str, Any] = {"rating": None, "reviews_count": None, "reviews": [], "since": None}

    state = _extract_state(html)
    if state:
        user = _find_user_object(state)
        if user:
            result["rating"] = user.get("rating") or user.get("score")
            result["reviews_count"] = user.get("reviewsCount") or user.get("opinionsCount")
            result["since"] = user.get("created") or user.get("registeredSince")

    soup = BeautifulSoup(html, "lxml")
    for node in soup.select('[data-testid="user-review"], [data-cy="user-review"]')[:10]:
        text = node.get_text(" ", strip=True)
        if text:
            result["reviews"].append(text[:400])

    if result["since"] is None:
        since_node = soup.select_one('[data-cy="user-since"], [data-testid="member-since"]')
        if since_node:
            result["since"] = since_node.get_text(strip=True)

    return result


def _find_user_object(node: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 8:
        return None
    if isinstance(node, dict):
        if "name" in node and any(k in node for k in ("rating", "reviewsCount", "opinionsCount")):
            return node
        for value in node.values():
            found = _find_user_object(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_user_object(item, depth + 1)
            if found:
                return found
    return None
