# CLAUDE.md

Guidance for Claude Code (or any future contributor/agent) working in this
repository.

## Project summary

Telegram bot that monitors OLX for used iPhones (models 11–17, all storage
variants), scores each listing with the Gemini API (text + photos + seller
reviews), and delivers matches to users based on their personal saved
filters. See `README.md` for features and `ARCHITECTURE.md` for the full
technical design — read both before making structural changes.

## Stack

- Python 3.12+, fully async (`asyncio`/`aiohttp`/`aiogram` 3.x)
- SQLAlchemy (async) + SQLite
- Gemini API (`google-genai` SDK), model: `gemini-3.5-flash-lite` by
  default — the Lite variant of the current generation, which
  historically carries the most generous free-tier RPM
- Docker + docker-compose for deployment

## Commands

```bash
# install runtime deps only
pip install -r requirements.txt

# install runtime + test/lint deps (what you want for development)
pip install -r requirements-dev.txt

# run bot locally (needs a populated .env — copy .env.example)
python -m bot.main

# run tests — no network and no API key required, everything external is stubbed
pytest

# lint / format
ruff check .
ruff format .

# run via docker
docker compose up --build
```

Keep this section in sync with reality. Versions in `requirements.txt`
are pinned to a set that actually resolves together; `aiogram` is the
constraint that pins `aiohttp` and `pydantic`, so bump it first if you
need a newer one of those.

## Repository map

See `ARCHITECTURE.md §2` for the full layout. Quick pointers:

| I need to...                              | Look in...                    |
|--------------------------------------------|--------------------------------|
| Add a new bot menu/command                 | `bot/handlers/`, `bot/keyboards/` |
| Change what data we scrape from OLX        | `bot/scraper/`                 |
| Change the AI prompt or output schema      | `bot/ai/prompts.py`, `bot/ai/gemini_client.py` |
| Add/change a DB table                      | `bot/db/models.py` (+ write a migration) |
| Add a UI string                            | `bot/locales/{uk,ru,en}.json` — **all three**, never just one |
| Change background monitoring logic         | `bot/scheduler/monitor.py`     |
| Change how a listing card/analysis looks   | `bot/render.py` (shared by handlers **and** the monitor) |
| Add an iPhone model or storage tier        | `bot/constants.py`             |
| Add a new `callback_data` scheme           | `bot/keyboards/callbacks.py`   |

## Conventions

- Everything I/O-bound is `async`/`await` — no blocking calls inside
  handlers or the monitor loop (blocks the whole bot).
- All external config (tokens, API keys, poll interval, DB path) goes
  through `bot/config.py` (pydantic `Settings`) — never hardcode secrets
  or read `os.environ` ad-hoc elsewhere.
- Every new UI-facing string must be added to **all three** locale files
  (`uk.json`, `ru.json`, `en.json`), not just the one you're testing with.
- `callback_data` must stay under Telegram's 64-byte limit — follow the
  existing `lst:{id}:{action}` pattern (see `ARCHITECTURE.md §7`) rather
  than inventing a new encoding.
- AI analysis must always go through the cache lookup
  (`(listing_id, content_hash)`) before calling the Gemini API — never
  call `gemini_client` directly from a handler without checking
  `ai/cache.py` first. This is what keeps the bot inside the free tier.
- Respect the rate limiter in `utils/ratelimit.py` for both Gemini and
  OLX requests — do not add new call sites that bypass it.
- `GEMINI_RPM` is meant to track Google's actual free-tier ceiling for
  the configured model, not add an artificially lower throttle on top of
  it. Google no longer publishes exact per-model RPM in public docs —
  the live number is at https://aistudio.google.com/rate-limit
  (per-project, requires login). If you change `GEMINI_MODEL`, check
  that page and update `GEMINI_RPM` to match rather than guessing.

## Things not to break

- The stateless navigation model in `handlers/listings.py` (§7 of
  ARCHITECTURE.md) — don't introduce server-side "current view" session
  state; every sub-view must be reachable purely from `listing_id`.
- The AI response schema (`score`, `short_verdict`, `price_assessment`,
  `condition_assessment`, `seller_assessment`, `risk_flags`) — if it
  changes, update both `ai/prompts.py` and every handler that renders
  an analysis, plus the `analyses` table.
- Scraping etiquette (randomized delay, backoff on 403/429, capped
  concurrency) — this is a scraper without an official API; being
  aggressive here risks getting the source IP blocked entirely.

## Backlog / ideas not yet built

- Price-history tracking per model/storage (trend charts).
- Admin/stats view (active users, notifications sent, API usage).
- Migrate `scraper/` to Playwright automatically on repeated block
  detection instead of requiring a manual flag.
- PostgreSQL migration path if usage grows beyond SQLite's comfort zone.

## Out of scope / explicitly deferred

- No payment/monetization features.
- No multi-server deployment — single-process design is intentional
  at the current scale (see `ARCHITECTURE.md §8`).
