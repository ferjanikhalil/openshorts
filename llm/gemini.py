"""Gemini provider — wraps the google-genai SDK behind the LLMProvider interface."""
import json
import os
from typing import Optional

from google import genai
from google.genai import types as genai_types

from .base import LLMConfig, LLMProvider, LLMResponse


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


def parse_json_response_text(text: str) -> dict:
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


def _thinking_config_from_env(model_name: str):
    raw = (os.getenv("GEMINI_THINKING_SCORE") or "off").strip().lower()
    if raw in ("", "off", "0", "none", "false"):
        return None
    try:
        if raw.isdigit():
            return genai_types.ThinkingConfig(thinking_budget=int(raw))
        if raw in ("low", "high"):
            if model_name.startswith("gemini-3"):
                return genai_types.ThinkingConfig(thinking_level=raw)
            return genai_types.ThinkingConfig(thinking_budget=2048 if raw == "low" else 8192)
    except Exception:
        return None
    return None


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def provider_name(self) -> str:
        return "gemini"

    def generate(self, prompt: str, config: LLMConfig) -> LLMResponse:
        kwargs = {
            "response_mime_type": "application/json",
            "candidate_count": 1,
            "temperature": config.temperature,
        }
        if config.response_schema is not None:
            kwargs["response_schema"] = config.response_schema
            thinking = _thinking_config_from_env(self._model)
            if thinking is not None:
                kwargs["thinking_config"] = thinking
        if config.max_output_tokens is not None:
            kwargs["max_output_tokens"] = config.max_output_tokens
        if config.timeout:
            # LLMConfig.timeout is SECONDS; HttpOptions.timeout is MILLISECONDS.
            # Passing seconds straight through gave every call 120ms, so each one
            # died with "read operation timed out" and clip selection always fell
            # back to converting the whole video.
            kwargs["http_options"] = genai_types.HttpOptions(
                timeout=int(config.timeout * 1000))

        genai_config = genai_types.GenerateContentConfig(**kwargs)
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=genai_config,
        )

        raw_text = _get_response_text(response)
        parsed_obj = getattr(response, "parsed", None)
        if parsed_obj is not None:
            parsed = parsed_obj.model_dump() if hasattr(parsed_obj, "model_dump") else parsed_obj
        else:
            parsed = parse_json_response_text(raw_text)

        usage = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0 if usage else 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0 if usage else 0
        thinking_tokens = getattr(usage, "thoughts_token_count", 0) or 0 if usage else 0

        return LLMResponse(
            parsed=parsed,
            raw_text=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            model=self._model,
        )
