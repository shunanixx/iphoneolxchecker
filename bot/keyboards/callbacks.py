"""Callback-data encoding.

Telegram caps `callback_data` at 64 **bytes** (not characters), and
exceeding it fails at send time rather than build time — an easy way to
ship a keyboard that crashes only for some users. So every callback
string is built through `cb()`, which asserts the budget.

The scheme is the one fixed in ARCHITECTURE.md §7: short colon-separated
segments, `lst:{id}:{action}` for listings. Do not invent a new encoding
without updating that section.
"""

CALLBACK_MAX_BYTES = 64

# --- namespaces ---
NS_MENU = "menu"
NS_FILTER = "flt"
NS_SUB = "sub"
NS_LISTING = "lst"
NS_SETTINGS = "set"
NS_NOOP = "noop"


def cb(*parts: str | int) -> str:
    """Join callback segments and enforce the 64-byte limit."""
    data = ":".join(str(part) for part in parts)
    size = len(data.encode("utf-8"))
    if size > CALLBACK_MAX_BYTES:
        raise ValueError(f"callback_data is {size} bytes (max {CALLBACK_MAX_BYTES}): {data!r}")
    return data


def listing_cb(listing_id: int, action: str) -> str:
    """`lst:{id}:{action}` — the stateless navigation key."""
    return cb(NS_LISTING, listing_id, action)


def menu_cb(action: str) -> str:
    return cb(NS_MENU, action)


def filter_cb(*parts: str | int) -> str:
    return cb(NS_FILTER, *parts)


def sub_cb(sub_id: int, action: str) -> str:
    return cb(NS_SUB, sub_id, action)


def settings_cb(*parts: str | int) -> str:
    return cb(NS_SETTINGS, *parts)


NOOP = cb(NS_NOOP)
