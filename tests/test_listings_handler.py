"""The listing card's sub-view handlers — the photos button split and the
seller-reviews self-heal for a stale `seller_profile_url`.

aiogram's `@router.callback_query(...)` decorator registers the handler
but returns the original function unchanged, so these are called
directly as plain coroutines against fake `CallbackQuery`/`Message`
stand-ins — no live Bot or Dispatcher needed.
"""

from bot.db import crud
from bot.db.engine import session_scope
from bot.handlers.listings import show_photos, show_reviews
from bot.middlewares.i18n import Translator
from bot.scraper.olx_client import FetchError
from bot.scraper.parser import ListingDetail


class FakeMessage:
    def __init__(self):
        self.media_group_calls: list[list] = []
        self.photo_calls: list[tuple] = []
        self.text_calls: list[tuple] = []
        self.edit_calls: list[tuple] = []

    async def answer_media_group(self, media):
        self.media_group_calls.append(media)

    async def answer_photo(self, photo, caption=None, reply_markup=None):
        self.photo_calls.append((photo, caption, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.text_calls.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None):
        self.edit_calls.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def _make_listing(photos: list[str], suffix: str) -> int:
    async with session_scope() as session:
        listing = await crud.upsert_listing(
            session,
            {
                "olx_id": f"photos-{suffix}",
                "url": "https://olx.ua/x",
                "title": "iPhone 13",
                "price": 1,
                "currency": "UAH",
                "city": None,
                "description": None,
                "photos": photos,
                "seller_name": None,
                "seller_profile_url": None,
                "model": "iphone_13",
                "storage": "128",
                "posted_at": None,
                "content_hash": f"h-{suffix}",
            },
        )
        return listing.id


async def test_single_photo_uses_answer_photo_not_media_group(db):
    """Telegram's sendMediaGroup rejects fewer than 2 items outright — a
    listing with exactly one photo (common; many sellers upload just
    one) must fall back to a plain photo message, or the button silently
    "does nothing" every time it's pressed.
    """
    listing_id = await _make_listing(["https://cdn/1.jpg"], "one")
    message = FakeMessage()
    callback = FakeCallback(f"lst:{listing_id}:photos", message)

    await show_photos(callback, Translator("uk"))

    assert len(message.photo_calls) == 1
    photo, _caption, reply_markup = message.photo_calls[0]
    assert photo == "https://cdn/1.jpg"
    assert reply_markup is not None, "single photo can carry the back button directly"
    assert not message.media_group_calls
    assert not message.text_calls, "no follow-up message needed when sendPhoto carries the keyboard"


async def test_multiple_photos_use_media_group(db):
    listing_id = await _make_listing(["https://cdn/1.jpg", "https://cdn/2.jpg"], "two")
    message = FakeMessage()
    callback = FakeCallback(f"lst:{listing_id}:photos", message)

    await show_photos(callback, Translator("uk"))

    assert len(message.media_group_calls) == 1
    assert len(message.media_group_calls[0]) == 2
    assert not message.photo_calls
    assert len(message.text_calls) == 1, "media group needs the follow-up message for the keyboard"


async def test_no_photos_shows_alert_and_sends_nothing(db):
    listing_id = await _make_listing([], "none")
    message = FakeMessage()
    callback = FakeCallback(f"lst:{listing_id}:photos", message)

    await show_photos(callback, Translator("uk"))

    assert not message.photo_calls
    assert not message.media_group_calls
    assert not message.text_calls
    assert callback.answers


async def _make_listing_with_seller(seller_profile_url: str | None, suffix: str) -> int:
    async with session_scope() as session:
        listing = await crud.upsert_listing(
            session,
            {
                "olx_id": f"seller-{suffix}",
                "url": "https://olx.ua/x",
                "title": "iPhone 13",
                "price": 1,
                "currency": "UAH",
                "city": None,
                "description": None,
                "photos": [],
                "seller_name": "Продавець",
                "seller_profile_url": seller_profile_url,
                "model": "iphone_13",
                "storage": "128",
                "posted_at": None,
                "content_hash": f"h-seller-{suffix}",
            },
        )
        return listing.id


class FakeOLXForReviews:
    """Simulates a stale stored URL that 404s, with a working current one."""

    def __init__(self, reviews_by_url: dict[str, dict], fresh_seller_url: str | None = None):
        self.reviews_by_url = reviews_by_url
        self.fresh_seller_url = fresh_seller_url
        self.review_calls: list[str] = []
        self.detail_calls: list[str] = []

    async def fetch_seller_reviews(self, url: str) -> dict:
        self.review_calls.append(url)
        if url not in self.reviews_by_url:
            raise FetchError(f"HTTP 404 for {url}")
        return self.reviews_by_url[url]

    async def fetch_detail(self, listing_url: str) -> ListingDetail:
        self.detail_calls.append(listing_url)
        return ListingDetail(seller_profile_url=self.fresh_seller_url)


async def test_reviews_self_heal_on_a_stale_seller_url(db):
    """A URL captured before OLX changed its profile-link scheme (or any
    other reason a stored URL stops resolving) must not just show "no
    data" forever — the first view should recover it from the listing's
    own current page and persist the fix for next time.
    """
    stale_url = "https://www.olx.ua/uk/list/user/958029057/"
    fresh_url = "https://www.olx.ua/uk/list/user/1VqEek/"
    listing_id = await _make_listing_with_seller(stale_url, "stale")

    olx = FakeOLXForReviews(
        reviews_by_url={
            fresh_url: {"rating": None, "reviews_count": None, "reviews": [], "since": "2020-01-01"}
        },
        fresh_seller_url=fresh_url,
    )
    message = FakeMessage()
    callback = FakeCallback(f"lst:{listing_id}:reviews", message)

    await show_reviews(callback, Translator("uk"), olx)

    assert olx.review_calls == [stale_url, fresh_url], (
        "must try the stored URL, then the refreshed one"
    )
    assert olx.detail_calls == ["https://olx.ua/x"]

    async with session_scope() as session:
        listing = await crud.get_listing(session, listing_id)
        assert listing.seller_profile_url == fresh_url, "the corrected URL must be persisted"
        cached = await crud.get_seller_reviews(session, fresh_url)
        assert cached is not None

    assert message.edit_calls, "the view must render successfully after the self-heal"
    rendered_text = message.edit_calls[-1][0]
    assert "2020-01-01" in rendered_text


async def test_reviews_gives_up_gracefully_when_self_heal_also_fails(db):
    stale_url = "https://www.olx.ua/uk/list/user/958029057/"
    listing_id = await _make_listing_with_seller(stale_url, "unrecoverable")

    # fresh_seller_url=None: even the re-fetched detail page has no link.
    olx = FakeOLXForReviews(reviews_by_url={}, fresh_seller_url=None)
    message = FakeMessage()
    callback = FakeCallback(f"lst:{listing_id}:reviews", message)

    await show_reviews(callback, Translator("uk"), olx)

    assert olx.review_calls == [stale_url]
    assert olx.detail_calls == ["https://olx.ua/x"]
    assert message.edit_calls, "must still render a card, just without review data"


async def test_reviews_served_from_cache_never_touches_the_network(db):
    url = "https://www.olx.ua/uk/list/user/1VqEek/"
    listing_id = await _make_listing_with_seller(url, "cached")

    async with session_scope() as session:
        await crud.save_seller_reviews(
            session, url, {"rating": 4.8, "reviews_count": 10, "reviews": [], "since": None}
        )

    olx = FakeOLXForReviews(reviews_by_url={})
    message = FakeMessage()
    callback = FakeCallback(f"lst:{listing_id}:reviews", message)

    await show_reviews(callback, Translator("uk"), olx)

    assert olx.review_calls == []
    assert olx.detail_calls == []
