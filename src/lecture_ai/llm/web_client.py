"""GPT 网页版的本地交换适配器。

它不自动登录、不读取浏览器凭据，也不伪装成 API：
1. 把 prompt/schema/request 元数据写到 session 的 exchange_dir；
2. 等待浏览器协作把严格 JSON 保存为 response.json；
3. 读回响应并交给统一 schema 校验、缓存和组装流程。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from lecture_ai.errors import LLMError, WebResponseRequired
from lecture_ai.llm.base import LLMClient, LLMResponse
from lecture_ai.utils.paths import atomic_write_text
from lecture_ai.utils.timefmt import now_local, to_iso


class ChatGPTWebClient(LLMClient):
    provider = "chatgpt_web"

    def __init__(self, model: str = "chatgpt-web-high") -> None:
        self.model = model

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.2,
        request_context: dict[str, Any] | None = None,
    ) -> LLMResponse:
        context = request_context or {}
        exchange_raw = context.get("exchange_dir")
        if not exchange_raw:
            raise LLMError("chatgpt_web provider 缺少 exchange_dir 请求上下文")
        exchange_dir = Path(exchange_raw)
        exchange_dir.mkdir(parents=True, exist_ok=True)

        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prompt_path = exchange_dir / "prompt.md"
        schema_path = exchange_dir / "schema.json"
        request_path = exchange_dir / "request.json"
        response_path = exchange_dir / "response.json"
        previous_prompt_sha: str | None = None
        if request_path.exists():
            try:
                previous = json.loads(request_path.read_text(encoding="utf-8"))
                previous_prompt_sha = str(previous.get("prompt_sha256") or "") or None
            except (OSError, json.JSONDecodeError):
                previous_prompt_sha = None
        atomic_write_text(prompt_path, prompt)
        atomic_write_text(
            schema_path,
            json.dumps(json_schema or {}, ensure_ascii=False, indent=2),
        )
        atomic_write_text(
            request_path,
            json.dumps(
                {
                    "provider": self.provider,
                    "model": self.model,
                    "stage": context.get("stage"),
                    "index": context.get("index"),
                    "prompt_sha256": prompt_sha,
                    "prompt_chars": len(prompt),
                    "created_at": to_iso(now_local()),
                    "response_file": "response.json",
                    "token_usage_available": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        if not response_path.exists():
            raise WebResponseRequired(
                f"GPT 网页任务已生成：{prompt_path}；"
                f"请把网页返回的严格 JSON 保存为 {response_path}"
            )
        if previous_prompt_sha and previous_prompt_sha != prompt_sha:
            raise LLMError(
                f"GPT 网页 response.json 对应旧 prompt：{response_path}；"
                "请用当前 prompt.md 重新生成并覆盖响应"
            )

        text = response_path.read_text(encoding="utf-8").strip()
        text = _strip_single_json_fence(text)
        if not text:
            raise LLMError(f"GPT 网页响应为空：{response_path}")
        request_id = "web-" + hashlib.sha256(
            (prompt_sha + "\n" + text).encode("utf-8")
        ).hexdigest()[:16]
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            usage={
                "input_chars": len(prompt),
                "output_chars": len(text),
                "web_turns": 1,
            },
            request_id=request_id,
        )


def _strip_single_json_fence(text: str) -> str:
    """容忍网页把唯一 JSON 包在一个 markdown fence 中，其他内容仍严格拒绝。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or not lines[-1].strip().startswith("```"):
        return stripped
    if lines[0].strip().lower() not in {"```", "```json"}:
        return stripped
    return "\n".join(lines[1:-1]).strip()
