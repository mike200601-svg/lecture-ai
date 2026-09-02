"""重叠边界的确定性判定。

文本内容等价时本地合并 metadata，不浪费一次网页 LLM；只有两个块对同一
segment 给出实质冲突时才升级到边界协调。
"""

from __future__ import annotations

import re
from typing import Any


def decide_boundary(
    left_result: list[dict[str, Any]],
    right_result: list[dict[str, Any]],
) -> dict[str, Any]:
    left_map = {int(item["id"]): item for item in left_result}
    right_map = {int(item["id"]): item for item in right_result}
    shared = [segment_id for segment_id in left_map if segment_id in right_map]
    conflicts: list[int] = []
    merged: list[dict[str, Any]] = []
    punctuation_only = False

    for segment_id in shared:
        left = left_map[segment_id]
        right = right_map[segment_id]
        left_text = str(left.get("text") or "").strip()
        right_text = str(right.get("text") or "").strip()
        if _content_key(left_text) != _content_key(right_text):
            conflicts.append(segment_id)
            continue
        punctuation_only = punctuation_only or left_text != right_text
        chosen = left if len(left_text) >= len(right_text) else right
        merged.append(
            {
                "id": segment_id,
                "text": str(chosen.get("text") or "").strip(),
                "uncertain": _merge_strings(left, right, "uncertain"),
                "visual_references": _merge_strings(
                    left, right, "visual_references"
                ),
                "corrections": _merge_corrections(left, right),
            }
        )

    if conflicts:
        return {
            "decision": "llm",
            "reasons": ["overlap_text_conflict"],
            "conflicting_segment_ids": conflicts,
            "segment_ids": shared,
            "result": [],
        }
    reason = "punctuation_or_whitespace_equivalent" if punctuation_only else "identical_overlap"
    return {
        "decision": "deterministic",
        "reasons": [reason],
        "conflicting_segment_ids": [],
        "segment_ids": shared,
        "result": merged,
    }


def _content_key(text: str) -> str:
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE).casefold()


def _merge_strings(left: dict, right: dict, key: str) -> list[str]:
    return sorted(
        {
            str(value)
            for value in list(left.get(key) or []) + list(right.get(key) or [])
            if str(value).strip()
        }
    )


def _merge_corrections(left: dict, right: dict) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in list(left.get("corrections") or []) + list(right.get("corrections") or []):
        if not isinstance(item, dict):
            continue
        value = {
            "original": str(item.get("original") or ""),
            "corrected": str(item.get("corrected") or ""),
            "decision": str(item.get("decision") or ""),
            "reason": str(item.get("reason") or ""),
        }
        marker = tuple(value.values())
        if marker not in seen:
            seen.add(marker)
            values.append(value)
    return values
