"""Database helpers: users, subscriptions, notification de-duplication."""

from bot.db import crud
from bot.db.engine import session_scope


async def _user(tg_id=111, language="uk") -> int:
    async with session_scope() as session:
        user = await crud.get_or_create_user(session, tg_id, "tester", language)
        return user.id


async def test_get_or_create_user_is_idempotent(db):
    first = await _user()
    second = await _user()
    assert first == second


async def test_username_refreshes_but_language_is_not_overwritten(db):
    async with session_scope() as session:
        await crud.get_or_create_user(session, 222, "old", "uk")
    async with session_scope() as session:
        await crud.set_user_language(session, 222, "en")
    async with session_scope() as session:
        user = await crud.get_or_create_user(session, 222, "new", "uk")
        assert user.username == "new"
        assert user.language == "en", "a later update must not reset the user's choice"


async def test_distinct_active_models_deduplicates(db):
    user_id = await _user()
    async with session_scope() as session:
        await crud.create_subscription(
            session, user_id, models=["iphone_13", "iphone_14"], storages=[]
        )
        await crud.create_subscription(
            session, user_id, models=["iphone_14", "iphone_15"], storages=[]
        )

    async with session_scope() as session:
        models = await crud.distinct_active_models(session)

    assert models == {"iphone_13", "iphone_14", "iphone_15"}


async def test_paused_subscriptions_are_excluded_from_polling(db):
    user_id = await _user()
    async with session_scope() as session:
        sub = await crud.create_subscription(session, user_id, models=["iphone_13"], storages=[])
        sub_id = sub.id

    async with session_scope() as session:
        await crud.toggle_subscription(session, sub_id, user_id)

    async with session_scope() as session:
        assert await crud.distinct_active_models(session) == set()


async def test_delete_subscription_is_scoped_to_its_owner(db):
    owner = await _user(tg_id=1)
    intruder = await _user(tg_id=2)

    async with session_scope() as session:
        sub = await crud.create_subscription(session, owner, models=["iphone_13"], storages=[])
        sub_id = sub.id

    async with session_scope() as session:
        assert await crud.delete_subscription(session, sub_id, intruder) is False
        assert await crud.delete_subscription(session, sub_id, owner) is True


async def test_notification_is_recorded_only_once(db):
    user_id = await _user()
    async with session_scope() as session:
        listing = await crud.upsert_listing(
            session,
            {
                "olx_id": "abc",
                "url": "u",
                "title": "iPhone 13",
                "price": 1,
                "currency": "UAH",
                "city": None,
                "description": None,
                "photos": [],
                "seller_name": None,
                "seller_profile_url": None,
                "model": "iphone_13",
                "storage": "128",
                "posted_at": None,
                "content_hash": "h",
            },
        )
        listing_id = listing.id

    async with session_scope() as session:
        assert await crud.record_notification(session, user_id, listing_id, None) is True

    async with session_scope() as session:
        assert await crud.record_notification(session, user_id, listing_id, None) is False
        assert await crud.notification_exists(session, user_id, listing_id) is True


async def test_upsert_listing_updates_instead_of_duplicating(db):
    payload = {
        "olx_id": "same",
        "url": "u",
        "title": "iPhone 13",
        "price": 20000,
        "currency": "UAH",
        "city": None,
        "description": None,
        "photos": [],
        "seller_name": None,
        "seller_profile_url": None,
        "model": "iphone_13",
        "storage": "128",
        "posted_at": None,
        "content_hash": "h1",
    }

    async with session_scope() as session:
        first = await crud.upsert_listing(session, payload)
        first_id = first.id

    async with session_scope() as session:
        second = await crud.upsert_listing(
            session, {**payload, "price": 18000, "content_hash": "h2"}
        )
        assert second.id == first_id
        assert second.price == 18000
        assert second.content_hash == "h2"


async def test_seller_reviews_cache_roundtrip(db):
    async with session_scope() as session:
        await crud.save_seller_reviews(session, "https://olx.ua/user/1", {"rating": 4.8})

    async with session_scope() as session:
        cached = await crud.get_seller_reviews(session, "https://olx.ua/user/1")

    assert cached == {"rating": 4.8}


async def test_seller_reviews_cache_miss_returns_none(db):
    async with session_scope() as session:
        assert await crud.get_seller_reviews(session, "https://olx.ua/user/none") is None
