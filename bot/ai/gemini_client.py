"""Rate-limited wrapper around the Gemini API.

Two things this module guarantees for every caller:

1. No request leaves without passing the token bucket sized to the
   free-tier RPM ceiling (`GEMINI_RPM`) — deliberately set to Google's
   actual ceiling for the configured model rather than something
   artificially lower, so the limiter is a safety net that rarely fires
   rather than a second throttle stacked on top of Google's own.
2. Responses come back as validated dicts matching the schema in
   `prompts.py`, requested via Gemini's structured-output
   (`response_schema`) so there is no fragile string-parsing of
   free-form model output.

It deliberately does **not** know about caching — that is `ai/cache.py`,
and callers are expected to go through it rather than calling this class
directly from a handler.
"""

import asyncio
import json
from typing import Any

from bot.ai.prompts import (
    ANALYSIS_SCHEMA,
    CLASSIFY_PROMPT,
    CLASSIFY_SCHEMA,
    SYSTEM_INSTRUCTION,
    build_listing_prompt,
)
from bot.config import Settings
from bot.constants import MODELS_BY_KEY, STORAGES
from bot.utils.logging import get_logger
from bot.utils.ratelimit import TokenBucket

log = get_logger(__name__)

MAX_ATTEMPTS = 3
#: Rough cap on bytes per inline image; larger photos are skipped rather
#: than resized, since we have no image library in the dependency set.
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class GeminiError(RuntimeError):
    """The model could not be reached or returned something unusable."""


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.gemini_model
        self._bucket = TokenBucket(rate=settings.gemini_rpm, period=60.0)
        self._client: Any = None

    def _ensure_client(self) -> Any:
        """Import and construct lazily so tests don't need the SDK or a key."""
        if self._client is not None:
            return self._client
        if not self._settings.gemini_api_key:
            raise GeminiError("GEMINI_API_KEY is not configured")

        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise GeminiError("google-genai is not installed") from exc

        self._client = genai.Client(api_key=self._settings.gemini_api_key)
        return self._client

    # ------------------------------------------------------------------
    # low-level call
    # ------------------------------------------------------------------

    async def _generate_json(
        self,
        parts: list[Any],
        schema: dict[str, Any],
        *,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One structured-output call. Returns `(parsed, raw)`."""
        from google.genai import types

        client = self._ensure_client()
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
            max_output_tokens=2048,
        )

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._bucket.acquire()
            try:
                response = await client.aio.models.generate_content(
                    model=self._model,
                    contents=parts,
                    config=config,
                )
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                # Quota/rate errors deserve a real wait; anything else is
                # probably transient and a short retry is enough.
                if "429" in message or "quota" in message or "resource_exhausted" in message:
                    wait = min(60.0, 5.0 * (2 ** (attempt - 1)))
                    log.warning("Gemini quota hit (attempt %s), sleeping %.0fs", attempt, wait)
                else:
                    wait = 2.0 * attempt
                    log.warning("Gemini call failed (attempt %s): %s", attempt, exc)
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(wait)
                continue

            text = (getattr(response, "text", None) or "").strip()
            if not text:
                last_error = GeminiError("empty response")
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(1.0 * attempt)
                continue

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                # Structured output should make this impossible, but a
                # truncated response can still land here.
                last_error = GeminiError(f"invalid JSON: {exc}")
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(1.0 * attempt)
                continue

            if not isinstance(parsed, dict):
                last_error = GeminiError("response was not a JSON object")
                break

            return parsed, {"text": text, "model": self._model}

        raise GeminiError(f"Gemini request failed after {MAX_ATTEMPTS} attempts: {last_error}")

    # ------------------------------------------------------------------
    # listing analysis
    # ------------------------------------------------------------------

    async def analyze_listing(
        self,
        *,
        title: str,
        description: str | None,
        price: int | None,
        currency: str | None,
        city: str | None,
        url: str,
        model: str | None,
        storage: str | None,
        seller_name: str | None,
        reviews: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        images: list[bytes] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Score one listing. Returns `(analysis_payload, raw_response)`."""
        from google.genai import types

        usable_images = [
            image for image in (images or []) if image and len(image) <= MAX_IMAGE_BYTES
        ][: self._settings.gemini_max_photos]

        prompt = build_listing_prompt(
            title=title,
            description=description,
            price=price,
            currency=currency,
            city=city,
            url=url,
            model=model,
            storage=storage,
            seller_name=seller_name,
            reviews=reviews,
            params=params,
            photo_count=len(usable_images),
        )

        parts: list[Any] = [types.Part.from_text(text=prompt)]
        parts.extend(
            types.Part.from_bytes(data=image, mime_type=_guess_mime(image))
            for image in usable_images
        )

        parsed, raw = await self._generate_json(
            parts, ANALYSIS_SCHEMA, system_instruction=SYSTEM_INSTRUCTION
        )
        return normalize_analysis(parsed), raw

    # ------------------------------------------------------------------
    # detector fallback
    # ------------------------------------------------------------------

    async def classify_model_storage(
        self, title: str, description: str = ""
    ) -> tuple[str | None, str | None]:
        """Text-only fallback used when the regex detector is unsure.

        Cheap by design — no images, no system instruction, tiny output —
        because it runs on the ambiguous tail of listings, not all of them.
        """
        from google.genai import types

        prompt = CLASSIFY_PROMPT.format(title=title, description=description or "(немає)")
        parsed, _ = await self._generate_json(
            [types.Part.from_text(text=prompt)], CLASSIFY_SCHEMA, temperature=0.0
        )

        model = (parsed.get("model") or "").strip() or None
        storage = (parsed.get("storage") or "").strip() or None

        # Never trust the model to stay inside our vocabulary.
        if model not in MODELS_BY_KEY:
            model = None
        if storage not in STORAGES:
            storage = None
        return model, storage


def _guess_mime(data: bytes) -> str:
    """Sniff the image type from magic bytes; OLX serves JPEG and WebP."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def normalize_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce a model response into exactly the shape the DB expects.

    The schema constrains the model, but a clamped score and guaranteed
    string fields mean a surprising response degrades the card rather
    than breaking the handler that renders it.
    """
    try:
        score = int(payload.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(1, min(10, score)) if score else 0

    risk_flags = payload.get("risk_flags") or []
    if isinstance(risk_flags, str):
        risk_flags = [risk_flags]
    risk_flags = [str(flag).strip() for flag in risk_flags if str(flag).strip()][:8]

    def _text(key: str) -> str:
        value = payload.get(key)
        return str(value).strip() if value else ""

    verdict = _text("short_verdict")
    if len(verdict) > 300:
        verdict = verdict[:297] + "…"

    return {
        "score": score,
        "short_verdict": verdict,
        "price_assessment": _text("price_assessment"),
        "condition_assessment": _text("condition_assessment"),
        "seller_assessment": _text("seller_assessment"),
        "risk_flags": risk_flags,
    }
