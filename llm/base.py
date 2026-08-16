"""Provider-agnostic LLM abstraction for OpenShorts.

The core contract: text prompt in, validated JSON dict out.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Type

from pydantic import BaseModel


@dataclass
class LLMConfig:
    temperature: float = 0.2
    max_output_tokens: Optional[int] = None
    response_schema: Optional[Type[BaseModel]] = None
    # SECONDS. Providers whose SDK wants other units must convert (google-genai's
    # HttpOptions.timeout is milliseconds — see llm/gemini.py).
    timeout: float = 120.0


@dataclass
class LLMResponse:
    parsed: dict
    raw_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    model: str = ""


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, config: LLMConfig) -> LLMResponse:
        ...

    @abstractmethod
    def provider_name(self) -> str:
        ...
