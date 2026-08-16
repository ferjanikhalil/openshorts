"""OpenAI-compatible provider — works with any /v1/chat/completions endpoint.

Covers: NVIDIA NIM, OmniRoute, OpenRouter, LiteLLM, vLLM, LM Studio, Ollama.
"""
from typing import Optional, Type

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from .base import LLMConfig, LLMProvider, LLMResponse
from .gemini import parse_json_response_text


def _coerce_to_schema(parsed, schema: Type[BaseModel]) -> dict:
    """Attempt to normalize parsed JSON to match the expected schema.

    Handles common model deviations:
    - Model returns a bare list instead of {"key": [...]}
    - Model uses a different wrapper key (e.g. "results" instead of "windows")
    - Model returns a single item object instead of a list wrapper
    """
    list_fields = [
        name for name, field in schema.model_fields.items()
        if hasattr(field.annotation, "__origin__") and field.annotation.__origin__ is list
    ]

    if isinstance(parsed, list):
        if len(list_fields) == 1:
            parsed = {list_fields[0]: parsed}
        else:
            return parsed
    elif not isinstance(parsed, dict):
        return parsed

    try:
        schema.model_validate(parsed)
        return parsed
    except ValidationError:
        pass

    if len(list_fields) != 1:
        return parsed
    target_key = list_fields[0]

    if target_key not in parsed:
        list_values = [v for v in parsed.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return {target_key: list_values[0]}

        item_model = schema.model_fields[target_key].annotation.__args__[0]
        item_fields = set(item_model.model_fields.keys())
        if item_fields and item_fields.issubset(parsed.keys()):
            return {target_key: [parsed]}

    return parsed


class OpenAICompatProvider(LLMProvider):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        key_fp = f"{api_key[:8]}...{api_key[-8:]}" if len(api_key) > 16 else api_key
        print(f"[LLM-INIT] base_url={base_url} model={model} timeout={timeout} key={key_fp}", flush=True)
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model

    def provider_name(self) -> str:
        return "openai_compat"

    def generate(self, prompt: str, config: LLMConfig) -> LLMResponse:
        kwargs = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": config.temperature,
            "response_format": {"type": "json_object"},
        }
        if config.max_output_tokens is not None:
            kwargs["max_tokens"] = config.max_output_tokens

        response = self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        raw_text = choice.message.content or ""

        parsed = parse_json_response_text(raw_text)
        if config.response_schema is not None:
            parsed = _coerce_to_schema(parsed, config.response_schema)
            config.response_schema.model_validate(parsed)

        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0

        return LLMResponse(
            parsed=parsed,
            raw_text=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=0,
            model=self._model,
        )
