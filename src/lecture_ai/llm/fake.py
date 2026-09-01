"""可注入的确定性 FakeLLM，仅用于测试，不得生成真实课堂验收产物。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from lecture_ai.llm.base import LLMClient, LLMResponse

Responder = Callable[[str], str | dict[str, Any]]
_INPUT = re.compile(r"<input_json>\s*(.*?)\s*</input_json>", re.DOTALL)


class FakeLLMClient(LLMClient):
    provider = "fake"
    model = "fake-clean-v1"

    def __init__(self, responder: Responder | None = None, *, fail_times: int = 0) -> None:
        self.responder = responder or self._echo
        self.fail_times = fail_times
        self.calls = 0

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("simulated transient LLM failure")
        value = self.responder(prompt)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            usage={
                "input_tokens": max(1, len(prompt) // 4),
                "output_tokens": max(1, len(text) // 4),
                "total_tokens": max(2, (len(prompt) + len(text)) // 4),
            },
            request_id=f"fake-{self.calls}",
        )

    @staticmethod
    def _echo(prompt: str) -> dict[str, Any]:
        match = _INPUT.search(prompt)
        if not match:
            return {"segments": []}
        items = json.loads(match.group(1))
        return {
            "segments": [
                {
                    "id": int(item["id"]),
                    "text": str(item.get("text") or item.get("left_text") or ""),
                    "uncertain": list(item.get("uncertain") or []),
                    "visual_references": list(item.get("visual_references") or []),
                }
                for item in items
            ]
        }
