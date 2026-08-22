"""The listing prompt's handling of seller review data.

Rating and review-count are unavailable for every seller on OLX by
construction (loaded by a separate widget our lightweight scraper never
sees) — the wording here exists specifically so Gemini doesn't read
"unavailable" as "confirmed zero" and label every seller a suspicious
new account. See ai/prompts.py's SYSTEM_INSTRUCTION for the full
reasoning.
"""

from bot.ai.prompts import build_listing_prompt


def _prompt(reviews):
    return build_listing_prompt(
        title="iPhone 13",
        description="Стан ідеальний",
        price=20000,
        currency="UAH",
        city="Київ",
        url="https://olx.ua/x",
        model="iphone_13",
        storage="128",
        seller_name="Продавець",
        reviews=reviews,
    )


def test_missing_rating_and_count_are_not_phrased_as_confirmed_zero():
    text = _prompt({"rating": None, "reviews_count": None, "reviews": [], "since": None})

    assert "недоступні" in text.lower()
    assert "не використовуй це як" in text.lower() or "не є" in text.lower()
    # The old wording that could read as "this seller has none" must be gone.
    assert "рейтинг: немає" not in text.lower()
    assert "кількість відгуків: немає" not in text.lower()


def test_real_registration_date_is_surfaced_as_a_reliable_signal():
    text = _prompt({"rating": None, "reviews_count": None, "reviews": [], "since": "2010-06-21"})

    assert "2010-06-21" in text
    assert "надійний сигнал" in text.lower()


def test_actual_rating_is_shown_when_present():
    """If some future account type ever does expose it, don't hide it."""
    text = _prompt({"rating": 4.8, "reviews_count": 12, "reviews": [], "since": None})

    assert "4.8" in text
    assert "12" in text


def test_review_texts_are_included_when_present():
    reviews = {
        "rating": None,
        "reviews_count": None,
        "reviews": ["Все супер, рекомендую!"],
        "since": None,
    }
    text = _prompt(reviews)

    assert "Все супер, рекомендую!" in text


def test_no_reviews_dict_at_all_does_not_imply_a_new_or_suspicious_seller():
    text = _prompt(None)

    assert "новим" not in text.lower()
    assert "підозрілим" not in text.lower() or "не означає" in text.lower()
