import asyncio
import logging
import time
from typing import Literal

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from ledger.config import get_settings
from ledger.db.engine import get_sessionmaker
from ledger.models import AiCallLog

log = logging.getLogger(__name__)

Tier = Literal["main", "lite", "reasoning"]

_client: genai.Client | None = None
# Without a timeout a stalled connection can hang a job (and the backfill queue) indefinitely.
REQUEST_TIMEOUT_MS = 300_000


class AIUnavailable(RuntimeError):
    pass


def get_client() -> genai.Client:
    global _client
    if _client is None:
        key = get_settings().gemini_key
        if key is None:
            raise AIUnavailable("GEMINI_KEY is not configured")
        _client = genai.Client(
            api_key=key.get_secret_value(), http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS)
        )
    return _client


def model_for(tier: Tier) -> str:
    s = get_settings()
    return {"main": s.gemini_model_main, "lite": s.gemini_model_lite, "reasoning": s.gemini_model_reasoning}[tier]


async def _log_call(purpose: str, model: str, started: float, resp, error: str | None) -> None:
    usage = getattr(resp, "usage_metadata", None)
    try:
        async with get_sessionmaker()() as session:
            session.add(
                AiCallLog(
                    purpose=purpose,
                    model=model,
                    input_tokens=getattr(usage, "prompt_token_count", None),
                    output_tokens=getattr(usage, "candidates_token_count", None),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    ok=error is None,
                    error=error,
                )
            )
            await session.commit()
    except Exception:  # logging must never break the AI call
        log.exception("Failed to record AI call log")


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return True
    if not isinstance(exc, genai_errors.APIError):
        return False
    # A 429 for an exhausted spend cap/quota won't clear by retrying; only rate limits and 5xx will.
    if exc.code == 429 and any(s in str(exc).lower() for s in ("spending cap", "spend cap", "billing")):
        return False
    return exc.code in (429, 500, 502, 503, 504)


async def generate[T: BaseModel](
    contents: list | str,
    *,
    purpose: str,
    tier: Tier = "main",
    system: str | None = None,
    schema: type[T] | None = None,
    temperature: float | None = None,
    attempts: int = 4,
):
    """Call Gemini; returns a parsed `schema` instance when given, else the response text."""
    client = get_client()
    model = model_for(tier)
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json" if schema is not None else None,
        response_schema=schema,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    delay = 2.0
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            resp = await client.aio.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:
            await _log_call(purpose, model, started, None, repr(exc)[:2000])
            if attempt < attempts and _retryable(exc):
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
        await _log_call(purpose, model, started, resp, None)
        if schema is None:
            return resp.text
        if resp.parsed is not None:
            return resp.parsed
        return schema.model_validate_json(resp.text or "{}")
    raise AssertionError("unreachable")


def file_part(data: bytes, mime_type: str) -> types.Part:
    return types.Part.from_bytes(data=data, mime_type=mime_type)
