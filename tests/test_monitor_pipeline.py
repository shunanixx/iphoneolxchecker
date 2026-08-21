"""End-to-end monitor cycle: scrape → detect → analyse → match → notify.

Nothing here touches the network or the AI provider — the fetcher and the
bot are stubs — but every other layer is the real one, so this is what
catches a break in the seam between them.
"""

import copy
import json

import pytest

from bot.ai.cache import AnalysisService
from bot.config import Settings
from bot.db import crud
from bot.db.engine import session_scope
from bot.scheduler.monitor import Monitor
from bot.scraper.olx_client import OLXClient

SEARCH_ADS = [
    {
        "id": 111,
        "title": "iPhone 13 Pro 256GB, ідеальний стан",
        "url": "https://www.olx.ua/d/uk/obyavlenie/a-ID111.html",
        "price": {"value": 28000, "currency": "UAH"},
        "location": {"city": {"name": "Київ"}},
        "photos": [{"link": "https://cdn/1;s={width}x{height}"}],
        "createdTime": "2026-08-20T09:00:00+03:00",
    },
    {
        "id": 222,
        "title": "Чохол для iPhone 13 Pro",
        "url": "https://www.olx.ua/d/uk/obyavlenie/b-ID222.html",
        "price": {"value": 300, "currency": "UAH"},
        "location": {"city": {"name": "Київ"}},
        "photos": [],
    },
    {
        "id": 333,
        "title": "iPhone 13 Pro 256GB",
        "url": "https://www.olx.ua/d/uk/obyavlenie/c-ID333.html",
        "price": {"value": 90000, "currency": "UAH"},
        "location": {"city": {"name": "Львів"}},
        "photos": [],
    },
]

DETAIL_AD = {
    "id": 111,
    "url": "https://www.olx.ua/d/uk/obyavlenie/a-ID111.html",
    "description": "Продам iPhone 13 Pro 256 ГБ. Стан ідеальний, повний комплект.",
    "photos": [{"link": "https://cdn/1;s={width}x{height}"}, {"link": "https://cdn/2.jpg"}],
    "user": {"name": "Іван", "otherAdsUrl": "https://www.olx.ua/uk/list/user/abc/"},
    "params": [{"name": "Стан", "value": {"label": "Вживані"}}],
}


def _state_html(payload: dict) -> str:
    blob = json.dumps(json.dumps(payload, ensure_ascii=False))
    return f"<html><script>window.__PRERENDERED_STATE__ = {blob};window.x=1;</script></html>"


class FakeFetcher:
    """Serves canned pages and records what was requested."""

    def __init__(self, ads: list[dict]):
        self.ads = ads
        self.requested: list[str] = []
        self.block_streak = 0

    async def get(self, url: str) -> str:
        self.requested.append(url)
        if "ID111" in url:
            price = self.ads[0]["price"]["value"]
            return _state_html({"ad": {**DETAIL_AD, "price": {"value": price}}})
        if "/list/user/" in url:
            return _state_html({"user": {"name": "Іван", "rating": 4.9, "reviewsCount": 12}})
        if "ID333" in url:
            return _state_html({"ad": {**DETAIL_AD, "id": 333, "user": {"name": "Петро"}}})
        return _state_html({"listing": {"listing": {"ads": self.ads}}})

    async def get_bytes(self, url: str) -> bytes:
        return b"\xff\xd8\xffimage"

    async def close(self) -> None:
        return None


class FakeBot:
    """Captures outgoing messages instead of calling Telegram."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return True


class FakeAI:
    def __init__(self):
        self.calls = 0

    async def analyze_listing(self, **kwargs):
        self.calls += 1
        return (
            {
                "score": 9,
                "short_verdict": "Ціна нижча за ринок, продавець надійний",
                "price_assessment": "Вигідна",
                "condition_assessment": "Ідеальний стан",
                "seller_assessment": "12 відгуків, рейтинг 4.9",
                "risk_flags": [],
            },
            {"text": "{}"},
        )

    async def classify_model_storage(self, title, description=""):
        self.calls += 1
        return None, None


@pytest.fixture
def ads() -> list[dict]:
    """A per-test copy, so a test that edits an ad can't leak into others."""
    return copy.deepcopy(SEARCH_ADS)


@pytest.fixture
def pipeline(settings: Settings, ads: list[dict]):
    fetcher = FakeFetcher(ads)
    olx = OLXClient(settings, fetcher=fetcher)
    ai_client = FakeAI()
    bot = FakeBot()
    analysis = AnalysisService(ai_client, image_loader=olx, max_photos=2)
    monitor = Monitor(bot, settings, olx, analysis)
    return monitor, bot, ai_client, fetcher


async def _subscribe(tg_id=555, **overrides) -> int:
    async with session_scope() as session:
        user = await crud.get_or_create_user(session, tg_id, "buyer", "uk")
        params = dict(models=["iphone_13_pro"], storages=["256"], price_max=50000, city=None)
        params.update(overrides)
        await crud.create_subscription(session, user.id, **params)
        return user.id


async def test_full_cycle_delivers_one_matching_listing(db, pipeline):
    monitor, bot, ai_client, _ = pipeline
    await _subscribe()

    sent = await monitor.run_cycle()

    assert sent == 1, "only the matching, non-accessory listing should be delivered"
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 555

    message = bot.sent[0]["text"]
    assert "iPhone 13 Pro" in message
    assert "9/10" in message
    assert "Ціна нижча за ринок" in message
    assert bot.sent[0]["markup"] is not None


async def test_accessory_listing_is_never_ingested(db, pipeline):
    monitor, _, _, fetcher = pipeline
    await _subscribe()

    await monitor.run_cycle()

    async with session_scope() as session:
        assert await crud.get_listing_by_olx_id(session, "222") is None
    assert not any("ID222" in url for url in fetcher.requested), (
        "we should not even fetch the detail page of an obvious accessory"
    )


async def test_listing_outside_the_price_filter_is_stored_but_not_sent(db, pipeline):
    monitor, bot, _, _ = pipeline
    await _subscribe()

    await monitor.run_cycle()

    # 90 000 UAH exceeds the filter's price_max, but the listing is still
    # in the database — a different user's filter may want it.
    async with session_scope() as session:
        assert await crud.get_listing_by_olx_id(session, "333") is not None
    assert all("90" not in entry["text"] for entry in bot.sent)


async def test_unmatched_listing_is_not_analysed(db, pipeline):
    """AI budget is only spent on listings someone will actually receive."""
    monitor, _, ai_client, _ = pipeline
    await _subscribe()

    await monitor.run_cycle()

    assert ai_client.calls == 1, "only the matching listing should reach the API"

    async with session_scope() as session:
        unmatched = await crud.get_listing_by_olx_id(session, "333")
        assert await crud.latest_analysis(session, unmatched.id) is None


async def test_detection_and_analysis_are_persisted(db, pipeline):
    monitor, _, ai_client, _ = pipeline
    await _subscribe()

    await monitor.run_cycle()

    async with session_scope() as session:
        listing = await crud.get_listing_by_olx_id(session, "111")
        assert listing.model == "iphone_13_pro"
        assert listing.storage == "256"
        assert listing.city == "Київ"
        assert listing.price == 28000
        assert listing.seller_name == "Іван"
        assert len(listing.photos) == 2

        analysis = await crud.get_cached_analysis(session, listing.id, listing.content_hash)
        assert analysis is not None
        assert analysis.score == 9


async def test_second_cycle_sends_nothing_new(db, pipeline):
    """Re-running must not re-notify or re-spend AI budget."""
    monitor, bot, ai_client, _ = pipeline
    await _subscribe()

    first = await monitor.run_cycle()
    calls_after_first = ai_client.calls
    second = await monitor.run_cycle()

    assert first == 1
    assert second == 0
    assert len(bot.sent) == 1
    assert ai_client.calls == calls_after_first, "known listings must not be re-analysed"


async def test_two_users_share_one_analysis(db, pipeline):
    """The whole point of the cache: one listing, one API call, many users."""
    monitor, bot, ai_client, _ = pipeline
    await _subscribe(tg_id=555)
    await _subscribe(tg_id=666)

    sent = await monitor.run_cycle()

    assert sent == 2
    assert {entry["chat_id"] for entry in bot.sent} == {555, 666}
    assert ai_client.calls == 1


async def test_seller_reviews_are_fetched_once_and_cached(db, pipeline):
    monitor, _, _, fetcher = pipeline
    await _subscribe()

    await monitor.run_cycle()

    profile_hits = [url for url in fetcher.requested if "/list/user/" in url]
    assert len(profile_hits) == 1

    async with session_scope() as session:
        cached = await crud.get_seller_reviews(session, "https://www.olx.ua/uk/list/user/abc/")
    assert cached["rating"] == 4.9


async def test_cycle_with_no_subscriptions_makes_no_requests(db, pipeline):
    monitor, bot, ai_client, fetcher = pipeline

    sent = await monitor.run_cycle()

    assert sent == 0
    assert fetcher.requested == []
    assert ai_client.calls == 0
    assert bot.sent == []


async def test_paused_subscription_receives_nothing(db, pipeline):
    monitor, bot, _, _ = pipeline
    user_id = await _subscribe()

    async with session_scope() as session:
        subs = await crud.list_subscriptions(session, user_id)
        await crud.toggle_subscription(session, subs[0].id, user_id)

    assert await monitor.run_cycle() == 0
    assert bot.sent == []


async def test_on_demand_search_uses_the_same_matching(db, pipeline):
    monitor, bot, _, _ = pipeline
    user_id = await _subscribe()

    listing_ids = await monitor.search_for_user(user_id)
    assert len(listing_ids) == 1

    delivered = await monitor.deliver_to_user(user_id, listing_ids)
    assert delivered == 1
    assert len(bot.sent) == 1

    # Having been delivered, it must not come back on the next search.
    assert await monitor.search_for_user(user_id) == []


async def test_edited_listing_is_re_analysed(db, pipeline, ads):
    """A price drop must invalidate the cached verdict, not keep a stale one."""
    monitor, _, ai_client, _ = pipeline
    await _subscribe()

    await monitor.run_cycle()
    assert ai_client.calls == 1

    async with session_scope() as session:
        listing = await crud.get_listing_by_olx_id(session, "111")
        original_hash = listing.content_hash

    # The seller drops the price; the next scrape sees the new value.
    ads[0]["price"]["value"] = 25000
    await monitor.run_cycle()

    assert ai_client.calls == 2, "an edited listing must be re-analysed"

    async with session_scope() as session:
        listing = await crud.get_listing_by_olx_id(session, "111")
        assert listing.price == 25000
        assert listing.content_hash != original_hash


async def test_unchanged_listing_is_not_re_fetched(db, pipeline):
    """Only genuinely changed listings may cost a detail fetch."""
    monitor, _, _, fetcher = pipeline
    await _subscribe()

    await monitor.run_cycle()
    detail_hits = len([url for url in fetcher.requested if "ID111" in url])

    await monitor.run_cycle()

    assert len([url for url in fetcher.requested if "ID111" in url]) == detail_hits
