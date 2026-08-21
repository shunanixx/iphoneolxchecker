
# 📱 OLX iPhone Scout Bot

A Telegram bot that monitors the used iPhone market on OLX (models
iPhone 11–17, all storage variants), analyzes every listing with the
**Gemini API** (listing text, photos, seller reviews) and notifies the
user only about listings worth their attention — with a 1–10 score and a
short verdict.

---

## ✨ Features

- 🔍 **Search by model** — iPhone 11, 12, 13, 14, 15, 16, 17, any storage
  size (detected automatically from the listing text).
- 🎯 **Personal subscriptions** — each user configures their own filters:
  model, storage, price range, city. The bot only sends what matches
  that specific user.
- 📡 **Background monitoring** — the bot watches OLX for new listings on
  its own and sends a notification as soon as a matching one appears.
- 🤖 **AI-powered listing scoring** (Gemini API):
  - listing text analysis (is the price reasonable, is the description
    complete);
  - photo analysis (device condition, signs of mismatch/scam);
  - seller reviews and reputation analysis;
  - final 1–10 score + short verdict on the card, full breakdown behind
    a "Details" button.
- 🧭 **Button-based navigation** — from a listing card you can view all
  photos, seller reviews, the full AI analysis, and go back to the
  listing without losing context.
- 🌍 **Multilingual interface** — Ukrainian / Russian / English,
  switchable anytime in settings.
- ⚡ **AI analysis caching** — the same result isn't recomputed for
  different users, which keeps usage inside the Gemini API free tier.

---

## 🛠 Tech stack

| Component         | Technology                                     |
|--------------------|-------------------------------------------------|
| Language            | Python 3.12+, fully async/await                |
| Telegram bot         | aiogram 3.x                                     |
| AI                    | Gemini API (Flash / Flash-Lite model)           |
| OLX scraping          | aiohttp + BeautifulSoup (fallback: Playwright)  |
| Database               | SQLite (SQLAlchemy, async)                      |
| Background jobs         | APScheduler / asyncio background task           |
| Deployment                | Docker + docker-compose                         |

Full architecture details: [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## 🚀 Setup & run

### 1. Clone the repository

```bash
git clone <repo-url>
cd olx-iphone-bot
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

| Variable            | Description                                    |
|----------------------|--------------------------------------------------|
| `BOT_TOKEN`          | Telegram bot token from @BotFather               |
| `GEMINI_API_KEY`     | Gemini API key (Google AI Studio) — leave empty to run without AI scoring |
| `GEMINI_MODEL`       | e.g. `gemini-3.5-flash-lite`                     |
| `DB_PATH`            | Path to the SQLite file (default `./data/bot.db`) |
| `POLL_INTERVAL_SEC`  | How often to check OLX for new listings          |
| `DEFAULT_LANGUAGE`   | `uk` / `ru` / `en`                               |

### 3. Run with Docker

```bash
docker compose up --build -d
```

### 4. Run locally without Docker (for development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

---

## ⚠️ Disclaimer

Scraping OLX is done respectfully toward the source server
(rate-limited, no CAPTCHA/protection bypass). Review OLX's Terms of
Service before any commercial use.

---

## 📄 License

MIT
