"""Static catalogue of the iPhone models and storage tiers we track.

Model keys (`iphone_13_pro_max`) are what gets persisted in
`subscriptions.models` and `listings.model`, so they must stay stable —
renaming a key orphans existing subscriptions.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IPhoneModel:
    key: str
    title: str
    generation: int
    #: OLX search query used by the monitor (one search per model).
    query: str
    #: Lowercase phrases that identify this model in a listing title.
    aliases: tuple[str, ...] = field(default=())


def _m(key: str, title: str, gen: int, *aliases: str) -> IPhoneModel:
    return IPhoneModel(key=key, title=title, generation=gen, query=title, aliases=aliases)


#: Ordered newest-last; the bot renders them grouped by generation.
IPHONE_MODELS: tuple[IPhoneModel, ...] = (
    _m("iphone_11", "iPhone 11", 11, "iphone 11", "айфон 11"),
    _m("iphone_11_pro", "iPhone 11 Pro", 11, "iphone 11 pro", "айфон 11 про"),
    _m("iphone_11_pro_max", "iPhone 11 Pro Max", 11, "iphone 11 pro max"),
    _m("iphone_12_mini", "iPhone 12 mini", 12, "iphone 12 mini"),
    _m("iphone_12", "iPhone 12", 12, "iphone 12", "айфон 12"),
    _m("iphone_12_pro", "iPhone 12 Pro", 12, "iphone 12 pro"),
    _m("iphone_12_pro_max", "iPhone 12 Pro Max", 12, "iphone 12 pro max"),
    _m("iphone_13_mini", "iPhone 13 mini", 13, "iphone 13 mini"),
    _m("iphone_13", "iPhone 13", 13, "iphone 13", "айфон 13"),
    _m("iphone_13_pro", "iPhone 13 Pro", 13, "iphone 13 pro"),
    _m("iphone_13_pro_max", "iPhone 13 Pro Max", 13, "iphone 13 pro max"),
    _m("iphone_14", "iPhone 14", 14, "iphone 14", "айфон 14"),
    _m("iphone_14_plus", "iPhone 14 Plus", 14, "iphone 14 plus"),
    _m("iphone_14_pro", "iPhone 14 Pro", 14, "iphone 14 pro"),
    _m("iphone_14_pro_max", "iPhone 14 Pro Max", 14, "iphone 14 pro max"),
    _m("iphone_15", "iPhone 15", 15, "iphone 15", "айфон 15"),
    _m("iphone_15_plus", "iPhone 15 Plus", 15, "iphone 15 plus"),
    _m("iphone_15_pro", "iPhone 15 Pro", 15, "iphone 15 pro"),
    _m("iphone_15_pro_max", "iPhone 15 Pro Max", 15, "iphone 15 pro max"),
    _m("iphone_16", "iPhone 16", 16, "iphone 16", "айфон 16"),
    _m("iphone_16_plus", "iPhone 16 Plus", 16, "iphone 16 plus"),
    _m("iphone_16_pro", "iPhone 16 Pro", 16, "iphone 16 pro"),
    _m("iphone_16_pro_max", "iPhone 16 Pro Max", 16, "iphone 16 pro max"),
    _m("iphone_17", "iPhone 17", 17, "iphone 17", "айфон 17"),
    _m("iphone_17_pro", "iPhone 17 Pro", 17, "iphone 17 pro"),
    _m("iphone_17_pro_max", "iPhone 17 Pro Max", 17, "iphone 17 pro max"),
)

MODELS_BY_KEY: dict[str, IPhoneModel] = {m.key: m for m in IPHONE_MODELS}

GENERATIONS: tuple[int, ...] = tuple(sorted({m.generation for m in IPHONE_MODELS}))

#: Storage tiers, as strings — they are stored in JSON columns.
STORAGES: tuple[str, ...] = ("64", "128", "256", "512", "1024")

STORAGE_TITLES: dict[str, str] = {
    "64": "64 GB",
    "128": "128 GB",
    "256": "256 GB",
    "512": "512 GB",
    "1024": "1 TB",
}


def models_for_generation(generation: int) -> tuple[IPhoneModel, ...]:
    return tuple(m for m in IPHONE_MODELS if m.generation == generation)


def model_title(key: str) -> str:
    model = MODELS_BY_KEY.get(key)
    return model.title if model else key


def storage_title(key: str) -> str:
    return STORAGE_TITLES.get(key, f"{key} GB")
