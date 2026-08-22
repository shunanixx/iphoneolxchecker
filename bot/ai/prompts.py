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

from bot.constants import MODELS_BY_KEY, STORAGES

#: Fields the model must return. Mirrors the `analyses` table.
#:
#: `phone_score` and `seller_score` are deliberately separate — the
#: device/price side of the deal and the seller's trustworthiness are
#: independent questions, and blending them into one number hid which
#: one was actually the problem (e.g. a great phone from a sketchy
#: seller, or the reverse, both used to collapse to the same "6/10").
ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phone_score": {
            "type": "integer",
            "description": (
                "Deal quality for the device itself — price vs. market, condition, "
                "completeness — 1 (avoid) to 10 (excellent). Independent of the seller."
            ),
        },
        "seller_score": {
            "type": "integer",
            "description": (
                "Seller trustworthiness — reviews, rating, account age, listing "
                "quality, scam red flags — 1 (avoid) to 10 (excellent). Independent "
                "of the device itself."
            ),
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
        "phone_score",
        "seller_score",
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
- Якщо даних бракує (немає фото, порожній опис) — це сам по собі сигнал:
  зазнач це у відповідному полі та врахуй у балі.

ВАЖЛИВО про рейтинг і відгуки продавця: рейтинг та кількість відгуків
технічно недоступні для БУДЬ-ЯКОГО продавця на цій платформі — не через
те, що в конкретного продавця їх немає, а тому, що ці дані завантажує
окремий віджет, якого наш інструмент не бачить. Тому позначка
"рейтинг/відгуки: недоступні" стосується взагалі ВСІХ оголошень
однаково і сама по собі НЕ Є сигналом ненадійності — не пиши "новий
акаунт без відгуків" чи подібне лише на основі цієї відсутності даних.
Про надійність продавця роби висновок на основі:
- реальної дати реєстрації на OLX (якщо вказана — це надійний сигнал);
- поведінки в самому оголошенні (вимога передоплати, ухилення від
  зустрічі, підозрілі формулювання);
- будь-яких текстів відгуків, якщо вони все ж таки надані нижче.
Якщо жодного з цих сигналів немає — чесно напиши, що даних для оцінки
продавця замало, а не вигадуй висновок про "новий" чи "підозрілий"
акаунт.

Оцінюй телефон і продавця ОКРЕМО — це два різних питання, і "хороший
телефон від сумнівного продавця" чи навпаки не повинні зливатись в одне
розмите число.

Шкала phone_score (ціна/стан/комплектність пристрою):
- 9-10: чудова пропозиція, ціна нижча за ринок, стан і опис відмінні.
- 7-8: хороша пропозиція, дрібні зауваження щодо ціни або стану.
- 5-6: середня, є питання щодо ціни або стану.
- 3-4: сумнівна пропозиція, ціна завищена або стан викликає сумніви.
- 1-2: дуже погана пропозиція (явно завищена ціна, ознаки підміни/несправності).

Шкала seller_score (надійність продавця; рейтинг/відгуки НЕ використовуй
як підставу — вони недоступні завжди й для всіх, див. вище):
- 9-10: тривала історія на OLX (роки), у самому оголошенні немає жодних
  тривожних сигналів.
- 7-8: помірна історія на OLX, поведінка в оголошенні виглядає нормально.
- 5-6: даних для впевненого висновку недостатньо (типовий випадок —
  чесно признач цю оцінку, а не вигадуй причину для нижчої).
- 3-4: акаунт справді новий (за реальною датою реєстрації, якщо вона
  відома) АБО є конкретні тривожні сигнали з поведінки в оголошенні.
- 1-2: явні ознаки шахрайства в самому оголошенні (не просто відсутність
  рейтингу).

Типові ризики, на які варто звертати увагу:
- ціна значно нижча за ринкову (класична приманка шахраїв);
- вимога передоплати, відмова від зустрічі, "надішлю Новою поштою без огляду";
- стокові фото або фото з інтернету замість реального пристрою;
- згадки про заміну екрана/батареї, Face ID, "не оригінал", "не працює";
- Neverlock/R-SIM, iCloud-lock, "чистий iCloud" без підтвердження;
- акаунт, зареєстрований на OLX за реальною датою нещодавно (не плутати
  з відсутністю рейтингу — це різні речі, див. вище).
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
        return (
            "Дані про продавця не завантажені. Це не означає, що продавець "
            "новий чи підозрілий — просто немає інформації для аналізу."
        )

    lines: list[str] = []
    rating = reviews.get("rating")
    count = reviews.get("reviews_count")
    since = reviews.get("since")

    # Rating/review-count are unavailable for every seller on this
    # platform, by construction (a separate widget our scraper never
    # sees loads them) — phrased so the model can't mistake "we didn't
    # get this" for "this seller confirmed has none".
    if rating is not None:
        lines.append(f"Рейтинг: {rating}")
    if count is not None:
        lines.append(f"Кількість відгуків: {count}")
    if rating is None and count is None:
        lines.append(
            "Рейтинг і кількість відгуків: недоступні технічно (стосується "
            "всіх продавців на платформі однаково — НЕ використовуй це як "
            "ознаку ненадійності)."
        )
    if since:
        lines.append(f"На OLX з: {since} (реальна дата реєстрації — надійний сигнал)")

    texts = reviews.get("reviews") or []
    if texts:
        lines.append("Тексти відгуків, знайдені на сторінці:")
        lines.extend(f"- {text}" for text in texts[:8])

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
            "description": "One of 64, 128, 256, 512, 1024, 2048, or empty string if unclear.",
        },
    },
    "required": ["model", "storage"],
}

#: Built from the live catalogue rather than hardcoded, so a model added
#: to `constants.py` (e.g. iphone_16e, iphone_air — neither fits the
#: generic iphone_<gen>[_suffix] pattern) is something the AI fallback
#: can actually name, not just something the regex detector knows about.
_MODEL_KEY_LIST = ", ".join(MODELS_BY_KEY)
_STORAGE_LIST = ", ".join(STORAGES)

CLASSIFY_PROMPT = f"""\
Визнач модель iPhone та обсяг пам'яті з тексту оголошення.

Поверни:
- model: ОДИН з цих ключів (більше жодних інших значень не існує):
  {_MODEL_KEY_LIST}
  Якщо жоден не підходить або дані неоднозначні — порожній рядок.
- storage: одне зі значень {_STORAGE_LIST} (1024 = 1 ТБ, 2048 = 2 ТБ).
  Якщо визначити неможливо — порожній рядок.

Не вгадуй. Порожній рядок кращий за неправильну відповідь.

Заголовок: {{title}}
Опис: {{description}}
"""
