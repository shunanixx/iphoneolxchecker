"""Single source of truth for external configuration.

Every token, key, path and tuning knob is read here and nowhere else —
modules take a `Settings` instance (or import `settings`) rather than
touching `os.environ` on their own.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Language = Literal["uk", "ru", "en"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str = Field(alias="BOT_TOKEN")

    # --- Gemini ---
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    # The Lite variant of the current Gemini generation — Flash-Lite
    # models have historically carried the most generous free-tier RPM
    # of any Gemini tier, since Pro moved behind billing. See
    # ARCHITECTURE.md §5 for why this, specifically, is the free-tier
    # model to default to.
    gemini_model: str = Field(default="gemini-3.5-flash-lite", alias="GEMINI_MODEL")
    # Set to Google's actual free-tier ceiling for GEMINI_MODEL, not
    # something artificially lower: our own limiter should be a safety
    # net that almost never fires, not a second, tighter throttle on top
    # of the real one. Google no longer publishes exact per-model RPM in
    # its scrapable docs — confirm the live number for your own project
    # at https://aistudio.google.com/rate-limit and adjust this to match
    # if it differs; 15 here is a conservative placeholder based on the
    # pattern of recent Flash-Lite free tiers, not a confirmed figure for
    # this exact model.
    gemini_rpm: int = Field(default=15, alias="GEMINI_RPM")
    gemini_max_photos: int = Field(default=4, alias="GEMINI_MAX_PHOTOS")

    # --- Storage ---
    db_path: str = Field(default="./data/bot.db", alias="DB_PATH")

    # --- Monitor ---
    poll_interval_sec: int = Field(default=600, alias="POLL_INTERVAL_SEC")

    # --- i18n ---
    default_language: Language = Field(default="uk", alias="DEFAULT_LANGUAGE")

    # --- Scraper politeness ---
    olx_base_url: str = Field(default="https://www.olx.ua", alias="OLX_BASE_URL")
    olx_max_concurrency: int = Field(default=3, alias="OLX_MAX_CONCURRENCY")
    olx_min_delay_sec: float = Field(default=1.0, alias="OLX_MIN_DELAY_SEC")
    olx_max_delay_sec: float = Field(default=3.0, alias="OLX_MAX_DELAY_SEC")
    olx_pages_per_model: int = Field(default=1, alias="OLX_PAGES_PER_MODEL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def ai_enabled(self) -> bool:
        """Without a key the bot still scrapes and notifies, just unscored."""
        return bool(self.gemini_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
