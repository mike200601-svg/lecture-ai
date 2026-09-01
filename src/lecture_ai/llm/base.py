"""Provider-neutral LLM 接口与统一响应。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    request_id: str | None = None


class LLMClient(ABC):
    provider: str = "base"
    model: str = ""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """返回文本响应；结构化输出仍以 JSON 字符串承载。"""
