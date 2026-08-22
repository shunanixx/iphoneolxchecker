"""The price-range step of the subscription wizard.

`_parse_price` is unit-tested directly for the input formats real users
actually type; the handler-level tests drive `enter_price_min` /
`enter_price_max` through a real `FSMContext` backed by aiogram's
in-memory storage (the same storage the bot itself uses), so the state
transitions and the min>max rejection are exercised exactly as they run
in production, not re-implemented as a mock.
"""

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers.filters import _parse_price, enter_price_max, enter_price_min
from bot.middlewares.i18n import Translator
from bot.states.filters import FilterWizard


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20000", 20000),
        ("20 000", 20000),
        ("20.000", 20000),
        ("20,000", 20000),
        ("20000грн", 20000),
        ("20000 грн.", 20000),
        ("  15000  ", 15000),
        ("1", 1),
        ("10000000", 10_000_000),
    ],
)
def test_parse_price_accepts_realistic_input(text, expected):
    assert _parse_price(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "не знаю",
        "0",
        "10000001",  # one over MAX_PRICE
        "договірна",
    ],
)
def test_parse_price_rejects_invalid_input(text):
    assert _parse_price(text) is None


def test_parse_price_strips_a_stray_minus_sign():
    """Extracting digits and discarding everything else (same as it does
    for "20000грн") means a typo'd leading "-" is treated as the number
    typed, not rejected — a price field has no legitimate negative value
    to protect against.
    """
    assert _parse_price("-5000") == 5000


class FakeMessage:
    def __init__(self, text: str):
        self.text = text
        self.answers: list[tuple] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


def _fsm_context() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    return FSMContext(storage=storage, key=key)


async def test_min_then_max_are_both_stored():
    state = _fsm_context()
    i18n = Translator("uk")

    await enter_price_min(FakeMessage("15000"), state, i18n)
    assert (await state.get_data())["price_min"] == 15000
    assert await state.get_state() == FilterWizard.entering_price_max

    await enter_price_max(FakeMessage("25000"), state, i18n)
    data = await state.get_data()
    assert data["price_min"] == 15000
    assert data["price_max"] == 25000


async def test_max_below_min_is_rejected_and_state_is_unchanged():
    state = _fsm_context()
    i18n = Translator("uk")

    await enter_price_min(FakeMessage("20000"), state, i18n)
    message = FakeMessage("15000")
    await enter_price_max(message, state, i18n)

    data = await state.get_data()
    assert "price_max" not in data, "a max below min must not be saved"
    assert await state.get_state() == FilterWizard.entering_price_max, "must stay on this step"
    assert message.answers, "the user must be told why it was rejected"


async def test_max_equal_to_min_is_accepted():
    """An exact single-price search ("only at 20000") is a legitimate filter."""
    state = _fsm_context()
    i18n = Translator("uk")

    await enter_price_min(FakeMessage("20000"), state, i18n)
    await enter_price_max(FakeMessage("20000"), state, i18n)

    data = await state.get_data()
    assert data["price_min"] == 20000
    assert data["price_max"] == 20000


async def test_garbage_input_reprompts_without_advancing_state():
    state = _fsm_context()
    i18n = Translator("uk")
    await state.set_state(FilterWizard.entering_price_min)

    message = FakeMessage("скільки коштує новий?")
    await enter_price_min(message, state, i18n)

    assert await state.get_data() == {}
    assert await state.get_state() == FilterWizard.entering_price_min, "must stay on this step"
    assert message.answers
