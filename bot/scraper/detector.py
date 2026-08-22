"""Detect which iPhone model and storage tier a listing is about.

Regex first, AI second (ARCHITECTURE.md §4 step 6). Most OLX titles state
the model and storage plainly, so the cheap path handles the large
majority; the Gemini text-only fallback exists for the ambiguous tail and
is only reached when the regex genuinely can't decide.

Order matters: "iPhone 13 Pro Max" must be tested before "iPhone 13 Pro"
before "iPhone 13", otherwise every Pro Max is misfiled as a base model.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bot.constants import IPHONE_MODELS, MODELS_BY_KEY, STORAGES
from bot.utils.logging import get_logger

log = get_logger(__name__)

#: Async `(title, description) -> (model_key | None, storage | None)`.
Classifier = Callable[[str, str], Awaitable[tuple[str | None, str | None]]]

_STORAGE_RE = re.compile(r"\b(64|128|256|512)\s*(?:gb|gib|гб|гбайт|г6)\b", re.IGNORECASE)
_TERABYTE_RE = re.compile(r"\b1\s*(?:tb|тб|т6)\b", re.IGNORECASE)
#: 2 TB exists only on the 17 Pro Max, but detecting it generically costs
#: nothing and avoids a one-model special case.
_TWO_TERABYTE_RE = re.compile(r"\b2\s*(?:tb|тб|т6)\b", re.IGNORECASE)

#: Words that mean this listing is not a phone we can score.
_ACCESSORY_MARKERS = (
    "чехол",
    "чохол",
    "стекло",
    "скло",
    "чашка",
    "запчаст",
    "на запчасти",
    "на запчастини",
    "дисплей",
    "экран для",
    "акумулятор для",
    "аккумулятор для",
    "камера для",
    "плата",
    "корпус",
    "муляж",
    "копия",
    "копія",
    "реплика",
    "репліка",
)


@dataclass(frozen=True)
class Detection:
    model: str | None
    storage: str | None
    #: "regex" | "ai" | "none" — useful when debugging bad matches.
    source: str = "none"

    @property
    def is_complete(self) -> bool:
        return self.model is not None and self.storage is not None


def _normalize(text: str) -> str:
    """Fold the spellings sellers actually use into one comparable form."""
    lowered = text.lower()
    lowered = lowered.replace("ё", "е")
    # Cyrillic spellings of the brand, and the "i-phone"/"ip" shorthands.
    lowered = re.sub(r"\b(айфон|аифон|iphone|i-phone|iphon|ифон)\b", "iphone", lowered)
    lowered = re.sub(r"\bip\s*(\d{2})\b", r"iphone \1", lowered)
    lowered = lowered.replace("promax", "pro max").replace("pro-max", "pro max")
    lowered = lowered.replace("+", " plus ")
    # Cyrillic variant suffixes — Ukrainian and Russian sellers write
    # "Айфон 12 міні" as often as "iPhone 12 mini". "про макс" must be
    # folded before the bare "про", or Pro Max collapses to Pro.
    lowered = re.sub(r"\bпро\s*макс\b", "pro max", lowered)
    lowered = re.sub(r"\bмакс\b", "max", lowered)
    lowered = re.sub(r"\bпро\b", "pro", lowered)
    lowered = re.sub(r"\b(міні|мини)\b", "mini", lowered)
    lowered = re.sub(r"\bплюс\b", "plus", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def looks_like_accessory(text: str) -> bool:
    """Filter out cases, spare parts and replicas before we spend AI budget."""
    lowered = _normalize(text)
    return any(marker in lowered for marker in _ACCESSORY_MARKERS)


def detect_storage(text: str) -> str | None:
    normalized = _normalize(text)
    if _TWO_TERABYTE_RE.search(normalized):
        return "2048"
    if _TERABYTE_RE.search(normalized):
        return "1024"
    match = _STORAGE_RE.search(normalized)
    if match:
        return match.group(1)

    # Bare "iPhone 13 128" with no unit — common, and unambiguous enough
    # because the number can only be a storage tier.
    bare = re.search(r"\b(?:iphone)\s+\d{2}[a-zа-я ]*?\b(64|128|256|512)\b", normalized)
    return bare.group(1) if bare else None


def _alias_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Model keys paired with a matcher, longest phrase first."""
    entries: list[tuple[str, str]] = []
    for model in IPHONE_MODELS:
        phrases = {model.title.lower(), *model.aliases}
        for phrase in phrases:
            entries.append((model.key, _normalize(phrase)))

    entries.sort(key=lambda item: len(item[1]), reverse=True)
    return [
        (key, re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"))
        for key, phrase in entries
    ]


_ALIAS_PATTERNS = _alias_patterns()


def detect_model(text: str) -> str | None:
    normalized = _normalize(text)
    for key, pattern in _ALIAS_PATTERNS:
        if pattern.search(normalized):
            return key

    # "iPhone 14 ProMax"-style titles where a suffix follows the number
    # without the exact alias spacing we indexed.
    match = re.search(r"\biphone\s*(1[1-7])\b(.*)", normalized)
    if not match:
        return None

    generation, tail = match.group(1), match.group(2)[:24]
    if "pro max" in tail:
        suffix = "_pro_max"
    elif "pro" in tail:
        suffix = "_pro"
    elif "plus" in tail:
        suffix = "_plus"
    elif "mini" in tail:
        suffix = "_mini"
    else:
        suffix = ""

    candidate = f"iphone_{generation}{suffix}"
    if candidate in MODELS_BY_KEY:
        return candidate
    # A Plus/mini variant that does not exist for this generation still
    # tells us the generation, which is better than nothing.
    return f"iphone_{generation}" if f"iphone_{generation}" in MODELS_BY_KEY else None


def detect(title: str, description: str = "") -> Detection:
    """Pure-regex detection over the title, then the description."""
    model = detect_model(title)
    storage = detect_storage(title)

    if description and (model is None or storage is None):
        head = description[:1500]
        model = model or detect_model(head)
        storage = storage or detect_storage(head)

    source = "regex" if (model or storage) else "none"
    return Detection(model=model, storage=storage, source=source)


async def detect_with_fallback(
    title: str,
    description: str = "",
    classifier: Classifier | None = None,
) -> Detection:
    """Regex detection, escalating to a single cheap AI call if needed.

    The AI call only fires when the regex left the model unknown (or left
    storage unknown on an otherwise-matched phone) — never as the first
    attempt, since that would burn free-tier quota on titles that say
    "iPhone 13 Pro 256GB" in plain text.
    """
    result = detect(title, description)
    if result.is_complete or classifier is None:
        return result

    if looks_like_accessory(f"{title}\n{description[:500]}"):
        return result

    try:
        ai_model, ai_storage = await classifier(title, description[:1500])
    except Exception as exc:
        log.warning("AI model/storage fallback failed: %s", exc)
        return result

    model = result.model or (ai_model if ai_model in MODELS_BY_KEY else None)
    storage = result.storage or (ai_storage if ai_storage in STORAGES else None)

    if model == result.model and storage == result.storage:
        return result
    return Detection(model=model, storage=storage, source="ai")
