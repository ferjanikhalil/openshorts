import argparse
import json
import os
import sys
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel

from clip_selection import lookup_model_prices
from llm import create_provider
from llm.base import LLMConfig, LLMResponse

load_dotenv()


# --- Structured output schemas (passed as response_schema so the API
# --- guarantees the format instead of us repairing free-form JSON). ---

class ScoredWindowModel(BaseModel):
    id: str
    start: float
    end: float
    score: int
    reason: str


class ScoreResponse(BaseModel):
    windows: List[ScoredWindowModel]


class DetailClipModel(BaseModel):
    start: float
    end: float
    source_window_id: str
    predicted_score: int
    video_description_for_tiktok: str
    video_description_for_instagram: str
    video_title_for_youtube_short: str
    viral_hook_text: str


class DetailResponse(BaseModel):
    shorts: List[DetailClipModel]


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _log(message: str) -> None:
    stream = sys.stdout
    text = str(message)
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe_text + "\n")
    stream.flush()

SCORE_PROMPT_TEMPLATE = """
You are a senior short-form video strategist.
Select the MOST viral candidate windows from this batch.

Rules:
- Return only valid JSON.
- Choose up to 3 windows from this batch.
- `score` must be an integer from 0 to 100.
- THE 2-SECOND TEST is the main criterion: would the first 2 seconds of this
  moment force a cold viewer (no context) to keep watching? Windows that only
  work with prior context score low.
- Prefer windows with strong hooks, conflict, surprise, outrage, emotion,
  novelty, big numbers, or a clear payoff.
- Ignore weak filler, housekeeping, outros, rambling transitions, and
  low-signal padding unless there is an obvious hook or payoff.

TRANSCRIPT_LANGUAGE: {language}
VIDEO_DURATION_SECONDS: {video_duration}
WINDOWS_JSON:
{windows_json}

OUTPUT FORMAT (strict — the top-level object MUST have exactly one key "windows"):
{{
  "windows": [
    {{
      "id": "<window id>",
      "start": <number>,
      "end": <number>,
      "score": <integer 0-100>,
      "reason": "<very short reason>"
    }}
  ]
}}
Do NOT rename the "windows" key. Do NOT wrap it in another object. Return ONLY the JSON above.
"""

# Clip-length directive injected into DETAIL_PROMPT_TEMPLATE ({duration_directive}).
# "auto" is the default viral-length behavior; "short" produces the tightest clip
# that still contains the whole hook->payoff (never shorter than 11s).
DURATION_DIRECTIVE_AUTO = (
    "Each clip must be 15 to 60 seconds long, in absolute seconds from the start "
    "of the source video."
)
DURATION_DIRECTIVE_SHORT = (
    "Make each clip AS SHORT AS POSSIBLE while it stays fully self-contained and "
    "understandable — it must still open on the hook and end on the payoff. "
    "NEVER go below 11 seconds and do NOT exceed 30 seconds. Trim any lead-in, "
    "tangents, or trailing filler. Absolute seconds from the start of the source video."
)

DETAIL_PROMPT_TEMPLATE = """
You are a senior short-form video editor and viral copywriter.
Choose the BEST short clips from these shortlisted candidate windows.

CLIP RULES:
- Return only valid JSON.
- {duration_directive}
- Stay within the candidate window boundaries.
- THE 2-SECOND RULE: the clip MUST open on its strongest moment. If the first
  2 seconds would not stop a cold viewer from scrolling, move the start or skip the clip.
- Start slightly before the hook and end slightly after the payoff when possible.
- Do not cut in the middle of a word or phrase.
- No generic intros/outros unless they are the hook.
- Prefer one great clip per candidate window. Maximum 2 clips per window only if clearly justified.
- DIVERSITY: never return two clips that make the same point, tell the same
  story, or land the same joke — even across different windows. Pick the
  stronger one and drop the other.

HOOK PLAYBOOK — pick the strongest fitting pattern for `viral_hook_text` (max 10 words):
- Open question: "Why does everyone get this wrong?"
- Hot take / controversy: "Stop doing this. Seriously."
- Number / fact shock: "97% of people miss this."
- Story loop: "This one email almost ruined me."
- POV / pattern interrupt: "POV: you finally understand it."
(These are English PATTERNS — always write the actual hook in TRANSCRIPT_LANGUAGE.)

COPY RULES — ALL text fields (descriptions, title, hook) MUST be written in TRANSCRIPT_LANGUAGE ({language}):
- Descriptions (TikTok + Instagram): 1-2 punchy sentences that tease the payoff
  without spoiling it, then 3-5 topically relevant hashtags. No generic hashtag spam.
- `video_title_for_youtube_short`: max 100 chars, curiosity-driven, no fake claims.
- `predicted_score`: honest 0-100 estimate of viral potential.

TRANSCRIPT_LANGUAGE: {language}
VIDEO_DURATION_SECONDS: {video_duration}
CANDIDATE_WINDOWS_JSON:
{windows_json}

OUTPUT FORMAT (strict — the top-level object MUST have exactly one key "shorts"):
{{
  "shorts": [
    {{
      "start": <number>,
      "end": <number>,
      "source_window_id": "<window id>",
      "predicted_score": <integer 0-100>,
      "video_description_for_tiktok": "<description + hashtags>",
      "video_description_for_instagram": "<description + hashtags>",
      "video_title_for_youtube_short": "<title max 100 chars>",
      "viral_hook_text": "<short overlay max 10 words>"
    }}
  ]
}}
Do NOT rename the "shorts" key. Do NOT wrap it in another object. Return ONLY the JSON above.
"""


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_json_candidate(text: str) -> str:
    cleaned = _strip_code_fences(text)
    first_brace = cleaned.find("{")
    first_bracket = cleaned.find("[")
    if first_bracket != -1 and (first_brace == -1 or first_bracket < first_brace):
        end = cleaned.rfind("]")
        if end > first_bracket:
            return cleaned[first_bracket:end + 1]
    if first_brace != -1:
        end = cleaned.rfind("}")
        if end > first_brace:
            return cleaned[first_brace:end + 1]
    return cleaned


def _escape_invalid_unicode_escapes(text: str) -> str:
    chars = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "u":
            hex_digits = text[i + 2:i + 6]
            if len(hex_digits) < 4 or any(ch not in "0123456789abcdefABCDEF" for ch in hex_digits):
                chars.append("\\\\u")
                i += 2
                continue
        chars.append(text[i])
        i += 1
    return "".join(chars)


def _parse_json_response_text(text: str) -> dict:
    if not text:
        raise ValueError("LLM returned an empty response body.")
    candidate = _extract_json_candidate(text).replace("\x00", "").strip()
    if not candidate:
        raise ValueError("LLM response did not contain a JSON object.")
    parse_attempts = [candidate]
    sanitized_candidate = _escape_invalid_unicode_escapes(candidate)
    if sanitized_candidate != candidate:
        parse_attempts.append(sanitized_candidate)
    last_error: Optional[Exception] = None
    for parse_candidate in parse_attempts:
        try:
            return json.loads(parse_candidate)
        except json.JSONDecodeError as e:
            last_error = e
    raise ValueError(f"Failed to parse LLM JSON response: {last_error}")


def _get_response_text(response) -> str:
    try:
        text = response.text
        if text:
            return text
    except Exception:
        pass

    parts = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(part_text)
    return "\n".join(parts).strip()


def _calculate_cost_analysis(response, model_name: str) -> Optional[dict]:
    """Calculate cost from either a raw Gemini response or an LLMResponse."""
    if isinstance(response, LLMResponse):
        prompt_tokens = response.input_tokens
        output_tokens = response.output_tokens
        thinking_tokens = response.thinking_tokens
        if not prompt_tokens and not output_tokens:
            return None
    else:
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return None
        prompt_tokens = usage.prompt_token_count or 0
        output_tokens = usage.candidates_token_count or 0
        thinking_tokens = getattr(usage, "thoughts_token_count", 0) or 0

    prices = lookup_model_prices(model_name)
    price_estimated = prices is None
    if prices is None:
        prices = (0.50, 3.00)
    input_price_per_million, output_price_per_million = prices
    input_cost = (prompt_tokens / 1_000_000) * input_price_per_million
    output_cost = ((output_tokens + thinking_tokens) / 1_000_000) * output_price_per_million
    total_cost = input_cost + output_cost
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
        "model": model_name,
        "price_estimated": price_estimated,
    }


def _config_for_strategy(strategy: str, mode: str) -> LLMConfig:
    creative = mode == "detail"
    if strategy == "strict-json":
        temperature = 0.7 if creative else 0.1
        schema = None
    elif strategy == "json-text-recovery":
        temperature = 0.2 if creative else 0.0
        schema = None
    else:  # structured-schema
        temperature = 0.9 if creative else 0.2
        schema = DetailResponse if mode == "detail" else ScoreResponse
    return LLMConfig(temperature=temperature, response_schema=schema)


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(description="Run a single LLM request for clip scoring/detailing.")
    parser.add_argument("--mode", choices=["score", "detail"], required=True)
    parser.add_argument("--input", dest="input_path", required=True)
    parser.add_argument("--output", dest="output_path", required=True)
    parser.add_argument("--strategy", default="structured-schema")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    with open(args.input_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    provider = create_provider(model=args.model)
    model_name = provider._model if hasattr(provider, '_model') else (args.model or "unknown")
    config = _config_for_strategy(args.strategy, args.mode)
    language = str(payload.get("language") or "unknown")

    template = SCORE_PROMPT_TEMPLATE if args.mode == "score" else DETAIL_PROMPT_TEMPLATE
    format_kwargs = dict(
        video_duration=payload["video_duration"],
        language=language,
        windows_json=json.dumps(payload["windows"], ensure_ascii=False),
    )
    # The detail template carries a {duration_directive} slot; the score template
    # does not. Only pass it for detail so score.format() doesn't get an unused key.
    if args.mode != "score":
        format_kwargs["duration_directive"] = DURATION_DIRECTIVE_AUTO
    prompt = template.format(**format_kwargs)

    _log(f"🤖 LLM worker request: mode={args.mode} strategy={args.strategy} provider={provider.provider_name()} model={model_name} items={len(payload.get('windows', []))}")
    llm_response = provider.generate(prompt, config)

    result = {
        "mode": args.mode,
        "payload": llm_response.parsed,
        "cost_analysis": _calculate_cost_analysis(llm_response, model_name),
        "raw_text": llm_response.raw_text,
    }
    with open(args.output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    _log(f"✅ LLM worker success: mode={args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
