# Architecture — OLX iPhone Scout Bot

## 1. Overview

The system has three loosely-coupled subsystems that share a single SQLite
database:

```
┌─────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│   Telegram Bot   │◄────►│     Database      │◄────►│  Background       │
│   (aiogram)      │      │     (SQLite)       │      │  Monitor          │
│   - menus/FSM     │      │                    │      │  (scheduler loop) │
│   - on-demand      │      └──────────────────┘      │   - OLX scraper    │
│     search         │                                  │   - AI analysis    │
└─────────────────┘                                  │   - notifications  │
                                                        └──────────────────┘
```

Both the bot process and the monitor loop run inside the same Python
process (single asyncio event loop) to keep deployment simple — the
monitor is an `asyncio.create_task()` started alongside the
bot's polling/webhook loop. The design keeps clean module boundaries so
the monitor could be split into a separate worker process later without
touching business logic (see §8 Scalability notes).

---

## 2. Repository layout

```
olx-iphone-bot/
├── bot/
│   ├── main.py                # entrypoint: builds Dispatcher, starts monitor task
│   ├── config.py               # pydantic Settings, loaded from .env
│   ├── constants.py            # iPhone model/storage catalogue (stable keys)
│   ├── render.py               # message formatting shared by handlers + monitor
│   ├── handlers/
│   │   ├── start.py            # /start, main menu, on-demand search
│   │   ├── filters.py          # subscription creation wizard (FSM)
│   │   ├── listings.py         # listing card + sub-views (photos/reviews/details/back)
│   │   ├── settings.py         # language switch
│   │   └── common.py           # shared edit/answer helpers
│   ├── keyboards/               # inline keyboard builders (pure functions)
│   │   └── callbacks.py         # callback_data encoding + 64-byte guard
│   ├── states/                  # aiogram FSM state groups
│   ├── middlewares/
│   │   └── i18n.py              # injects translated strings per user
│   ├── locales/
│   │   ├── uk.json / ru.json / en.json
│   ├── db/
│   │   ├── models.py            # SQLAlchemy ORM models
│   │   ├── engine.py            # async engine/session factory
│   │   └── crud.py              # typed query helpers
│   ├── scraper/
│   │   ├── olx_client.py        # HTTP session, search & detail page fetch
│   │   ├── parser.py            # HTML/embedded-JSON extraction
│   │   └── detector.py          # model/storage regex detection (+ AI fallback)
│   ├── ai/
│   │   ├── gemini_client.py     # rate-limited Gemini API wrapper
│   │   ├── prompts.py           # prompt templates, structured-output schema
│   │   └── cache.py             # content-hash based analysis cache
│   ├── scheduler/
│   │   └── monitor.py           # main polling loop, subscription matching
│   └── utils/
│       ├── ratelimit.py         # token-bucket limiter (Gemini + OLX)
│       └── logging.py
├── tests/
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

---

## 3. Data model (SQLite via SQLAlchemy)

```
users
  id PK, tg_id UNIQUE, username, language, created_at

subscriptions
  id PK, user_id FK -> users.id
  models        JSON  -- e.g. ["iphone_13", "iphone_14"]
  storages      JSON  -- e.g. ["128", "256"]
  price_min     INTEGER NULL
  price_max     INTEGER NULL
  city          TEXT NULL
  is_active     BOOLEAN
  created_at

listings
  id PK, olx_id UNIQUE, url, title, price, currency, city
  description   TEXT
  photos        JSON   -- list of image URLs
  seller_name, seller_profile_url
  model, storage        -- detected values
  posted_at, first_seen_at, last_seen_at
  content_hash  TEXT    -- hash(title+description+price+photos), detects edits

analyses
  id PK
  listing_id FK -> listings.id
  content_hash  TEXT     -- must match listings.content_hash to be valid
  score         INTEGER  -- 1-10
  short_verdict TEXT
  price_assessment TEXT
  condition_assessment TEXT
  seller_assessment TEXT
  risk_flags    JSON
  raw_response  JSON
  created_at
  UNIQUE(listing_id, content_hash)

seller_reviews_cache
  seller_profile_url PK, reviews_json, fetched_at   -- TTL ~24h

notifications
  id PK, user_id FK, listing_id FK, subscription_id FK, sent_at
  UNIQUE(user_id, listing_id)
```

The `(listing_id, content_hash)` pair is the cache key for AI analysis:
if a seller edits the price or description, the hash changes and a fresh
analysis is triggered — otherwise every user who matches the listing
reuses the same cached result, which is what makes the free Gemini tier
viable at ~40 users/day.

---

## 4. Background monitor cycle (`scheduler/monitor.py`)

Runs every `POLL_INTERVAL_SEC` (configurable, e.g. 5–15 minutes):

1. Collect the **distinct set of models** referenced by all active
   subscriptions (query fan-out reduction — one OLX search per model,
   not per subscription).
2. For each model, fetch the latest OLX search results (paginated,
   newest first) via `scraper/olx_client.py`.
3. Parse listing cards → `(olx_id, title, price, url, city, thumbnail)`.
4. Diff against the `listings` rows already in the DB → listings that are
   **new**, plus the few whose price or title changed since we last saw
   them (an edit we can spot from the search card alone, without paying
   for a detail fetch). Everything else is skipped entirely.
5. For each of those: fetch full detail page (description, all
   photos, seller profile link).
6. `detector.py` extracts `model` + `storage` via regex first
   (`\b(64|128|256|512)\s?(gb|гб)\b`, model keywords); if ambiguous,
   falls back to a single lightweight Gemini text-only classification
   call.
7. Compute `content_hash`; store/update the `listings` row.
8. Check the listing against the active subscriptions. **If no filter
   matches it, stop here** — the row stays in the database (a future
   subscription may want it) but it is never analysed. Analysis is the
   only expensive step in the cycle, so spending it on a phone nobody
   asked for is the easiest way to blow the free tier.
9. For a wanted listing: fetch/refresh seller reviews (from
   `seller_reviews_cache`, TTL 24h), then, if no valid cached `analyses`
   row exists for this `(listing_id, content_hash)`, run AI analysis
   (§5) and store it.
10. For each matching subscription without an existing `notifications`
    row → render the listing card and send it to that user, then log the
    notification. `UNIQUE(user_id, listing_id)` means each person gets a
    given listing at most once, however many of their filters match it.

An on-demand **"🔍 Найти сейчас"** button in the bot re-uses the exact
same matching function scoped to a single user's filters, so manual
search and background monitoring share one code path.

---

## 5. AI analysis (`ai/gemini_client.py`)

- Model: `gemini-3.5-flash-lite` by default — the Lite variant of the
  current Gemini generation, since Flash-Lite tiers have historically
  carried the most generous free RPM ceiling (Pro is paid-only). Google
  no longer publishes exact per-model RPM/TPM/RPD in its public,
  scrapable docs — the authoritative number for your own project is at
  https://aistudio.google.com/rate-limit (requires login). Check it
  before changing `GEMINI_MODEL`.
- `GEMINI_RPM` is meant to match that live ceiling rather than being
  artificially lower — the token-bucket limiter in `utils/ratelimit.py`
  exists to stop us from exceeding Google's quota and getting 429'd, not
  to add a second, tighter throttle on top of it. The shipped default
  (15) is a conservative placeholder based on the pattern of recent
  Flash-Lite free tiers, not a confirmed figure for this exact model —
  raise it once you've checked the real number.
- Input to the model: title, description, price, city, seller name,
  a summarized/trimmed seller-reviews text, plus up to `GEMINI_MAX_PHOTOS`
  listing photos (as inline image parts).
- Output: requested as **structured JSON** (via `response_mime_type:
  application/json` and a `response_schema`) with fixed fields:
  `score (1-10)`, `short_verdict`, `price_assessment`,
  `condition_assessment`, `seller_assessment`, `risk_flags[]`. This
  avoids fragile string-parsing of free-form model output.
- Retries: each call retries up to 3 times on the same model, with
  exponential backoff on quota/429 errors specifically (longer wait) and
  a shorter backoff on other transient failures.
- Caching: see §3 — analysis is looked up by `(listing_id, content_hash)`
  before any API call is made, and only for listings that actually match
  an active subscription (§4 step 8) — scoring a listing nobody asked
  for is pure waste of the daily request quota.
- Can be switched off entirely by leaving `GEMINI_API_KEY` empty; the
  bot still scrapes and delivers listings, just unscored.

---

## 6. OLX scraping strategy (`scraper/`)

- Primary approach: `aiohttp` + `BeautifulSoup`, extracting listing data
  from the embedded JSON state OLX ships inside `<script>` tags on
  server-rendered pages — faster and lighter than a headless browser,
  important since this runs continuously in the background.
- Fallback: Playwright, only invoked if the lightweight client starts
  getting blocked/CAPTCHA'd (detected via response status/heuristics).
  Kept as a pluggable strategy behind the same `olx_client` interface.
- Politeness: randomized delay between requests, rotating User-Agent,
  exponential backoff on 403/429, capped concurrency.

---

## 7. Bot navigation model (`handlers/listings.py`)

Navigation is **stateless by design** — every callback encodes the
`listing_id` it needs, so "back" always re-renders the listing card for
that ID rather than requiring a session history stack:

```
lst:{id}:card       -> renders the listing card (short verdict + score)
lst:{id}:details     -> full AI breakdown (price/condition/seller/risks)
lst:{id}:photos      -> media group of all listing photos
lst:{id}:reviews     -> seller reviews
lst:{id}:back        -> re-renders lst:{id}:card
```

Keeping `callback_data` short (`lst:<id>:<action>`) respects Telegram's
64-byte limit and keeps every sub-view independently reachable/returnable
without server-side session state.

---

## 8. Scalability notes

- SQLite is sufficient at the current scale (~40 users/day, single
  process). The ORM layer (SQLAlchemy async) makes swapping to
  PostgreSQL a connection-string change, not a rewrite.
- If load grows, the monitor loop can be extracted into a separate
  worker process/container (already isolated in `scheduler/`), with the
  bot and worker communicating only through the shared database.

## 9. Known limitations

- OLX has no official API for this use case; scraping is inherently
  fragile to markup changes — the parser should be treated as the
  highest-maintenance module.
- Gemini free tier is rate- and quota-limited; under sustained high
  load, switching to the paid tier is a config change (see README).
- Storage/model detection via regex will miss unusual phrasings; the
  AI fallback mitigates but doesn't eliminate this.
