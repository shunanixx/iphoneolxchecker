"""The AI cache — the mechanism that keeps the bot inside the free tier."""

import asyncio

from bot.ai.cache import AnalysisService
from bot.ai.gemini_client import GeminiError, normalize_analysis
from bot.db import crud
from bot.db.engine import session_scope
from bot.db.models import Listing


class FakeAI:
    """Counts calls so tests can assert the cache actually prevented them."""

    def __init__(self, payload=None, fail=False):
        self.calls = 0
        self.fail = fail
        self.payload = payload or {
            "score": 8,
            "short_verdict": "Хороша пропозиція",
            "price_assessment": "Ціна нижча за ринок",
            "condition_assessment": "Стан добрий",
            "seller_assessment": "Продавець з відгуками",
            "risk_flags": [],
        }

    async def analyze_listing(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise GeminiError("boom")
        return self.payload, {"text": "{}"}

    async def classify_model_storage(self, title, description=""):
        self.calls += 1
        return "iphone_13", "128"


async def _make_listing(olx_id="1", content_hash="hash-a") -> int:
    async with session_scope() as session:
        listing = await crud.upsert_listing(
            session,
            {
                "olx_id": olx_id,
                "url": "https://olx.ua/x",
                "title": "iPhone 13 128GB",
                "price": 20000,
                "currency": "UAH",
                "city": "Київ",
                "description": "Стан ідеальний",
                "photos": [],
                "seller_name": "Іван",
                "seller_profile_url": None,
                "model": "iphone_13",
                "storage": "128",
                "posted_at": None,
                "content_hash": content_hash,
            },
        )
        return listing.id


async def test_first_call_hits_the_api_and_is_stored(db):
    ai_client = FakeAI()
    service = AnalysisService(ai_client)
    listing_id = await _make_listing()

    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)
        analysis = await service.get_or_create(session, listing)

    assert ai_client.calls == 1
    assert analysis is not None
    assert analysis.score == 8


async def test_second_call_is_served_from_cache(db):
    """Forty subscribers on one listing must cost one API call, not forty."""
    ai_client = FakeAI()
    service = AnalysisService(ai_client)
    listing_id = await _make_listing()

    for _ in range(5):
        async with session_scope() as session:
            listing = await crud.get_listing(session, listing_id)
            await service.get_or_create(session, listing)

    assert ai_client.calls == 1


async def test_edited_listing_invalidates_the_cache(db):
    """A changed content_hash is what forces a re-analysis."""
    ai_client = FakeAI()
    service = AnalysisService(ai_client)
    listing_id = await _make_listing(content_hash="hash-a")

    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)
        await service.get_or_create(session, listing)

    # Seller drops the price: new hash.
    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)
        listing.content_hash = "hash-b"

    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)
        await service.get_or_create(session, listing)

    assert ai_client.calls == 2


async def test_concurrent_requests_collapse_into_one_call(db):
    """Monitor and manual search hitting the same listing must not double-spend."""
    ai_client = FakeAI()
    service = AnalysisService(ai_client)
    listing_id = await _make_listing()

    async def run():
        async with session_scope() as session:
            listing = await crud.get_listing(session, listing_id)
            return await service.get_or_create(session, listing)

    results = await asyncio.gather(*(run() for _ in range(4)))

    assert ai_client.calls == 1
    assert all(result is not None for result in results)


async def test_api_failure_degrades_instead_of_raising(db):
    """A dead API must still let the listing be delivered, just unscored."""
    ai_client = FakeAI(fail=True)
    service = AnalysisService(ai_client)
    listing_id = await _make_listing()

    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)
        analysis = await service.get_or_create(session, listing)

    assert analysis is None


async def test_disabled_ai_returns_none(db):
    service = AnalysisService(None)
    listing_id = await _make_listing()

    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)
        assert await service.get_or_create(session, listing) is None
    assert not service.enabled


async def test_images_are_capped_and_failures_tolerated(db):
    class Loader:
        def __init__(self):
            self.requested = []

        async def fetch_image(self, url):
            self.requested.append(url)
            return None if url.endswith("2.jpg") else b"\xff\xd8\xffdata"

    loader = Loader()
    ai_client = FakeAI()
    service = AnalysisService(ai_client, image_loader=loader, max_photos=3)

    listing = Listing(
        id=1,
        olx_id="x",
        url="u",
        title="t",
        content_hash="h",
        photos=[f"{i}.jpg" for i in range(10)],
    )
    images = await service._load_images(listing)

    assert len(loader.requested) == 3
    assert all(isinstance(image, bytes) for image in images)
    assert len(images) == 2  # the failed download is simply absent


def test_normalize_clamps_a_wild_score():
    result = normalize_analysis({"score": 42, "short_verdict": "x", "risk_flags": "one flag"})
    assert result["score"] == 10
    assert result["risk_flags"] == ["one flag"]


def test_normalize_survives_a_malformed_response():
    result = normalize_analysis({})
    assert result["score"] == 0
    assert result["short_verdict"] == ""
    assert result["risk_flags"] == []
