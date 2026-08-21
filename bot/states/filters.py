"""FSM states for the subscription-creation wizard.

FSM state is used *only* here, for the multi-step wizard where the bot
genuinely has to remember partial input between messages. Listing
navigation stays stateless (ARCHITECTURE.md §7) — don't add view state
to this group.
"""

from aiogram.fsm.state import State, StatesGroup


class FilterWizard(StatesGroup):
    choosing_generation = State()
    choosing_models = State()
    choosing_storages = State()
    entering_price_min = State()
    entering_price_max = State()
    entering_city = State()
    confirming = State()
