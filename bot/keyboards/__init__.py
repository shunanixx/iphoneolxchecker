"""Inline keyboard builders — pure functions, no I/O, no database.

Every `callback_data` string produced here must stay under Telegram's
64-byte limit. `bot.keyboards.callbacks` owns the encoding so the limit
is checked in one place rather than trusted at each call site.
"""
