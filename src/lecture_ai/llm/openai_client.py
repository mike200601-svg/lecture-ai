"""OpenAI Responses API 实现。仅文本离开本机，音频不经过这里。"""

from __future__ import annotations

import os
from typing import Any

from lecture_ai.errors import DependencyMissing, LLMError
from lecture_ai.llm.base import LLMClient, LLMResponse


class OpenAILLMClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str, *, api_key: str | None = None, client=None) -> None:
        self.model = model
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if client is None and not key:
            raise LLMError(
                "WAITING FOR REAL LLM PROVIDER: 缺少 OPENAI_API_KEY（请只写入 .env）"
            )
        if client is not None:
            self._client = client
            return
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise DependencyMissing(
                'WAITING FOR REAL LLM PROVIDER: 未安装 OpenAI SDK；运行 pip install "lecture-ai[cloud]"'
            ) from exc
        self._client = OpenAI(api_key=key)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": prompt,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
            "store": False,
        }
        if system:
            kwargs["instructions"] = system
        if json_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "transcript_clean_result",
                    "schema": json_schema,
                    "strict": True,
                }
            }
        try:
            response = self._client.responses.create(**kwargs)
            text = str(getattr(response, "output_text", "") or "")
            if not text:
                raise LLMError("OpenAI 返回空 output_text")
            usage_obj = getattr(response, "usage", None)
            usage = {
                "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
                "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
            }
            return LLMResponse(
                text=text,
                provider=self.provider,
                model=str(getattr(response, "model", None) or self.model),
                usage=usage,
                request_id=getattr(response, "id", None),
            )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"OpenAI Responses API 调用失败：{exc}") from exc
