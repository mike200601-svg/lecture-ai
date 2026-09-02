"""结构化清洗响应 schema 与不依赖第三方库的严格校验。"""

from __future__ import annotations

from typing import Any

from lecture_ai.config import CleanConfig
from lecture_ai.errors import LLMError

CLEAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                    "uncertain": {"type": "array", "items": {"type": "string"}},
                    "visual_references": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "corrections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "original": {"type": "string"},
                                "corrected": {"type": "string"},
                                "decision": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["original", "corrected", "decision", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "id", "text", "uncertain", "visual_references", "corrections"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def validate_clean_response(
    data: Any,
    expected: list[dict],
    config: CleanConfig,
) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or set(data) != {"segments"}:
        raise LLMError("LLM JSON 顶层必须且只能包含 segments")
    items = data["segments"]
    if not isinstance(items, list):
        raise LLMError("LLM JSON 的 segments 必须是数组")

    expected_ids = [int(item["id"]) for item in expected]
    actual_ids = [item.get("id") for item in items if isinstance(item, dict)]
    if actual_ids != expected_ids:
        raise LLMError(
            f"LLM 改变了 segment 拓扑：expected={expected_ids}, actual={actual_ids}"
        )

    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "id", "text", "uncertain", "visual_references", "corrections"
        }:
            raise LLMError("每个清洗 segment 字段必须严格匹配 schema")
        text = item["text"]
        uncertain = item["uncertain"]
        visual = item["visual_references"]
        corrections = item["corrections"]
        if not isinstance(text, str):
            raise LLMError(f"segment {item['id']} text 必须是字符串")
        if not isinstance(uncertain, list) or not all(isinstance(x, str) for x in uncertain):
            raise LLMError(f"segment {item['id']} uncertain 必须是字符串数组")
        if not isinstance(visual, list) or not all(isinstance(x, str) for x in visual):
            raise LLMError(f"segment {item['id']} visual_references 必须是字符串数组")
        correction_keys = {"original", "corrected", "decision", "reason"}
        if not isinstance(corrections, list) or not all(
            isinstance(value, dict)
            and set(value) == correction_keys
            and all(isinstance(value[key], str) for key in correction_keys)
            for value in corrections
        ):
            raise LLMError(f"segment {item['id']} corrections 不符合严格 schema")
        if not text.strip() and not uncertain:
            raise LLMError(
                f"segment {item['id']} 清洗文本为空时必须在 uncertain 记录原因"
            )
        cleaned.append(
            {
                "id": int(item["id"]),
                "text": text.strip(),
                "uncertain": uncertain,
                "visual_references": visual,
                "corrections": corrections,
            }
        )

    before_chars = sum(len(str(item.get("text") or "").strip()) for item in expected)
    after_chars = sum(len(item["text"]) for item in cleaned)
    ratio = after_chars / max(1, before_chars)
    if ratio < config.min_retention_ratio:
        raise LLMError(f"清洗结果疑似摘要/删减：字符保留率 {ratio:.3f}")
    if ratio > config.max_expansion_ratio:
        raise LLMError(f"清洗结果疑似扩写：字符膨胀率 {ratio:.3f}")

    for source, output in zip(expected, cleaned):
        original_length = len(str(source.get("text") or "").strip())
        if len(output["text"]) > original_length * 3 + 30:
            raise LLMError(f"segment {output['id']} 疑似注入额外内容")
    return cleaned
