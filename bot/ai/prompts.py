"""Prompt templates and the structured-output schema for listing analysis.

The response schema here is load-bearing: `score`, `short_verdict`,
`price_assessment`, `condition_assessment`, `seller_assessment` and
`risk_flags` are persisted in the `analyses` table and rendered by the
listing handlers. Changing a field means changing all three (see
CLAUDE.md, "Things not to break").

Note on language: the analysis text is generated in **Ukrainian**, the
language of the source market, and cached once per
`(listing_id, content_hash)`. Generating it per UI language would
multiply API calls by three and break the shared cache that keeps us
inside the free tier — so the localized part of the card is the labels
around the text, not the text itself.
"""

from typing import Any

#: Fields the model must return. Mirrors the `analyses` table.
ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "Overall attractiveness of the deal, 1 (avoid) to 10 (excellent).",
        },
        "short_verdict": {
            "type": "string",
            "description": "One sentence, max 160 characters, shown on the listing card.",
        },
        "price_assessment": {
            "type": "string",
            "description": "Is the price fair for this model/storage/condition on the UA market?",
        },
        "condition_assessment": {
            "type": "string",
            "description": "Device condition as judged from the photos and the description.",
        },
        "seller_assessment": {
            "type": "string",
            "description": "Seller trustworthiness based on reviews, rating and listing quality.",
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short warning phrases; empty array when nothing is suspicious.",
        },
    },
    "required": [
        "score",
        "short_verdict",
        "price_assessment",
        "condition_assessment",
        "seller_assessment",
        "risk_flags",
    ],
}

SYSTEM_INSTRUCTION = """\
Ти — досвідчений експерт з ринку вживаних iPhone в Україні (OLX).
Твоє завдання — оцінити оголошення з точки зору покупця.

Правила:
- Відповідай ВИКЛЮЧНО українською мовою.
- Будь конкретним і стриманим: без маркетингових формулювань і без паніки.
- Оцінюй лише те, що видно в тексті, на фото та у відгуках. Не вигадуй
  фактів, яких немає в даних.
- Якщо даних бракує (немає фото, немає відгуків, порожній опис) — це сам
  по собі сигнал: зазнач це у відповідному полі та врахуй у балі.

Шкала score:
- 9-10: чудова пропозиція, ціна нижча за ринок, продавець надійний.
- 7-8: хороша пропозиція, дрібні зауваження.
- 5-6: середня, є питання щодо ціни або стану.
- 3-4: сумнівна, кілька тривожних сигналів.
- 1-2: висока ймовірність шахрайства або дуже погана ціна.

Типові ризики, на які варто звертати увагу:
- ціна значно нижча за ринкову (класична приманка шахраїв);
- вимога передоплати, відмова від зустрічі, "надішлю Новою поштою без огляду";
- стокові фото або фото з інтернету замість реального пристрою;
- згадки про заміну екрана/батареї, Face ID, "не оригінал", "не працює";
- Neverlock/R-SIM, iCloud-lock, "чистий iCloud" без підтвердження;
- новий акаунт продавця без відгуків або з негативними відгуками.
"""

_LISTING_TEMPLATE = """\
Оціни це оголошення з OLX.

## Оголошення
Заголовок: {title}
Ціна: {price}
Місто: {city}
Модель (визначена автоматично): {model}
Пам'ять (визначена автоматично): {storage}
Посилання: {url}

## Опис продавця
{description}

## Характеристики
{params}

## Продавець
Ім'я: {seller_name}
{reviews}

## Фото
{photos_note}
"""


def _fmt_price(price: int | None, currency: str | None) -> str:
    if price is None:
        return "не вказана (можливо «Договірна»)"
    return f"{price:,}".replace(",", " ") + f" {currency or 'UAH'}"


def _fmt_reviews(reviews: dict[str, Any] | None) -> str:
    if not reviews:
        return "Відгуки: даних немає."

    lines: list[str] = []
    rating = reviews.get("rating")
    count = reviews.get("reviews_count")
    since = reviews.get("since")

    lines.append(f"Рейтинг: {rating if rating is not None else 'немає'}")
    lines.append(f"Кількість відгуків: {count if count is not None else 'немає'}")
    if since:
        lines.append(f"На OLX з: {since}")

    texts = reviews.get("reviews") or []
    if texts:
        lines.append("Останні відгуки:")
        lines.extend(f"- {text}" for text in texts[:8])
    else:
        lines.append("Текстів відгуків немає.")

    return "\n".join(lines)


def _fmt_params(params: dict[str, str] | None) -> str:
    if not params:
        return "не вказані"
    return "\n".join(f"- {key}: {value}" for key, value in list(params.items())[:12])


def build_listing_prompt(
    *,
    title: str,
    description: str | None,
    price: int | None,
    currency: str | None,
    city: str | None,
    url: str,
    model: str | None,
    storage: str | None,
    seller_name: str | None,
    reviews: dict[str, Any] | None,
    params: dict[str, str] | None = None,
    photo_count: int = 0,
) -> str:
    """Render the text half of the multimodal request.

    The photos themselves are attached as separate inline image parts by
    `gemini_client`; this text tells the model how many to expect so it
    doesn't claim to have seen photos that were not sent.
    """
    if photo_count:
        photos_note = (
            f"До запиту додано {photo_count} фото з оголошення. "
            "Оціни за ними реальний стан пристрою."
        )
    else:
        photos_note = "Фото відсутні — це саме по собі підозріло для оголошення про телефон."

    description_text = (description or "").strip() or "(продавець не залишив опису)"
    if len(description_text) > 4000:
        description_text = description_text[:4000] + "…"

    return _LISTING_TEMPLATE.format(
        title=title,
        price=_fmt_price(price, currency),
        city=city or "не вказане",
        model=model or "не визначено",
        storage=f"{storage} GB" if storage else "не визначено",
        url=url,
        description=description_text,
        params=_fmt_params(params),
        seller_name=seller_name or "не вказане",
        reviews=_fmt_reviews(reviews),
        photos_note=photos_note,
    )


# --------------------------------------------------------------------------
# lightweight classification fallback (scraper/detector.py)
# --------------------------------------------------------------------------

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "description": "Model key such as iphone_13_pro_max, or empty string if unclear.",
        },
        "storage": {
            "type": "string",
            "description": "One of 64, 128, 256, 512, 1024, or empty string if unclear.",
        },
    },
    "required": ["model", "storage"],
}

CLASSIFY_PROMPT = """\
Визнач модель iPhone та обсяг пам'яті з тексту оголошення.

Поверни:
- model: один з ключів у форматі iphone_<покоління>[_pro|_pro_max|_plus|_mini],
  наприклад iphone_13, iphone_14_pro_max, iphone_15_plus.
  Підтримуються покоління 11-17. Якщо визначити неможливо — порожній рядок.
- storage: одне зі значень 64, 128, 256, 512, 1024 (1024 = 1 ТБ).
  Якщо визначити неможливо — порожній рядок.

Не вгадуй. Порожній рядок кращий за неправильну відповідь.

Заголовок: {title}
Опис: {description}
"""
