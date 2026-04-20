"""Voice transcription + combined items/duration extractor.

- `transcribe(audio_bytes)` calls OpenAI Whisper.
- `extract_intent(text)` runs one GPT-4o call that returns both the cart items
  and (optionally) the rental duration if the user volunteered it in the same
  message. One round-trip saves latency and keeps the flow consistent whether
  the user typed or spoke.

Both helpers swallow errors into safe defaults: a transcription failure returns
"", an extraction failure returns `{"items": [], "duration_months": None}`.
Callers must handle the empty path (flow falls to fallback_discount).
"""

from __future__ import annotations

import io
import json
import logging
import re
from functools import lru_cache
from typing import Any

from config import LLM_EXTRACTOR_MODEL, OPENAI_API_KEY, WHISPER_MODEL
from products import id_to_name

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client():
    from openai import OpenAI
    return OpenAI(api_key=OPENAI_API_KEY)


def transcribe(audio_bytes: bytes, filename: str = "voice_note.ogg") -> str:
    if not audio_bytes:
        return ""
    try:
        audio = io.BytesIO(audio_bytes)
        audio.name = filename
        result = _client().audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=audio,
        )
        return (getattr(result, "text", "") or "").strip()
    except Exception as e:
        log.exception("transcribe failed: %s", e)
        return ""


_DURATION_RE = re.compile(
    r"(\d+)\s*(?:months?|mo\b|m\b)|(\d+)\s*(?:years?|yr\b|y\b)",
    re.IGNORECASE,
)


def _regex_duration(text: str) -> int | None:
    if not text:
        return None
    m = _DURATION_RE.search(text)
    if not m:
        return None
    if m.group(1):
        months = int(m.group(1))
    else:
        months = int(m.group(2)) * 12
    return months if 1 <= months <= 36 else None


def _catalogue_for_prompt() -> str:
    # Compact id -> name listing so GPT-4o can pick ids accurately without
    # blowing up the context.
    return "\n".join(f"{pid}: {name}" for pid, name in id_to_name.items())


_SYS_PROMPT = """You extract rental cart intent from a short user message.

Return ONLY a JSON object with this exact shape:
{"items": [{"product_id": <int>, "qty": <int>}], "duration_months": <int or null>}

Rules:
- `product_id` MUST be chosen from the catalogue below (exact id). If a requested item doesn't match any catalogue entry, skip it.
- `qty` defaults to 1 if not stated. Must be a positive integer.
- `duration_months` is an integer 1-36. Only fill it if the user explicitly said a duration in the same message. If they said "for a year" -> 12. If unclear or not mentioned, return null.
- Parse Hindi / Hinglish / English freely.
- Return NO prose. Only the JSON object.

Catalogue (id: name):
{catalogue}
"""


def extract_intent(text: str) -> dict[str, Any]:
    """Returns {"items": [{product_id, qty, name}], "duration_months": int|None}."""
    if not text or not text.strip():
        return {"items": [], "duration_months": None}

    regex_dur = _regex_duration(text)

    try:
        resp = _client().chat.completions.create(
            model=LLM_EXTRACTOR_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYS_PROMPT.replace("{catalogue}", _catalogue_for_prompt())},
                {"role": "user", "content": text.strip()},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as e:
        log.exception("extract_intent failed: %s", e)
        return {"items": [], "duration_months": regex_dur}

    items: list[dict[str, Any]] = []
    for it in (data.get("items") or []):
        try:
            pid = int(it.get("product_id"))
            qty = int(it.get("qty") or 1)
            if pid in id_to_name and qty > 0:
                items.append({"product_id": pid, "qty": qty, "name": id_to_name[pid]})
        except (TypeError, ValueError):
            continue

    duration = data.get("duration_months")
    try:
        duration = int(duration) if duration is not None else None
        if duration is not None and not (1 <= duration <= 36):
            duration = None
    except (TypeError, ValueError):
        duration = None

    # Prefer LLM duration; fall back to the regex capture if it missed.
    if duration is None:
        duration = regex_dur

    return {"items": items, "duration_months": duration}
