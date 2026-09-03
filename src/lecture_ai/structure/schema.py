"""Phase 2B 严格 schema、完整覆盖与来源时间轴校验。"""

from __future__ import annotations

import re
from typing import Any

from lecture_ai.errors import LLMError

TOPIC_TYPES = ("content", "derivation", "example", "review", "administrative", "break")
TOP_LEVEL_FIELDS = (
    "lecture_topics", "subtopics", "definitions", "derivations", "examples",
    "teacher_emphasis", "exam_tips", "transitions",
)
_UNCERTAIN = {"type": "array", "items": {"type": "string"}}
_SOURCE_IDS = {
    "type": "array", "items": {"type": "integer"}, "minItems": 1, "uniqueItems": True,
}


def _evidence_schema(title_field: str = "label") -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "topic_id": {"type": "string"},
            title_field: {"type": "string"},
            "start": {"type": "number"},
            "end": {"type": "number"},
            "source_segment_ids": _SOURCE_IDS,
            "uncertain": _UNCERTAIN,
        },
        "required": [
            "id", "topic_id", title_field, "start", "end", "source_segment_ids", "uncertain",
        ],
        "additionalProperties": False,
    }


OUTLINE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lecture_topics": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "type": {"type": "string", "enum": list(TOPIC_TYPES)},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "source_segment_ids": _SOURCE_IDS,
                    "uncertain": _UNCERTAIN,
                },
                "required": [
                    "id", "title", "type", "start", "end", "source_segment_ids", "uncertain",
                ],
                "additionalProperties": False,
            },
        },
        "subtopics": {"type": "array", "items": _evidence_schema("title")},
        "definitions": {"type": "array", "items": _evidence_schema()},
        "derivations": {"type": "array", "items": _evidence_schema()},
        "examples": {"type": "array", "items": _evidence_schema()},
        "teacher_emphasis": {"type": "array", "items": _evidence_schema()},
        "exam_tips": {"type": "array", "items": _evidence_schema()},
        "transitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "from_topic_id": {"type": "string"},
                    "to_topic_id": {"type": "string"},
                    "cue": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "source_segment_ids": _SOURCE_IDS,
                    "uncertain": _UNCERTAIN,
                },
                "required": [
                    "id", "from_topic_id", "to_topic_id", "cue", "start", "end",
                    "source_segment_ids", "uncertain",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": list(TOP_LEVEL_FIELDS),
    "additionalProperties": False,
}

_STRONG_DERIVATION = re.compile(
    r"(?:下面|现在|接下来|首先|这里|我们)(?:来)?(?:进行|做|看)?[^。！？]{0,8}(?:推导|证明)"
    r"|推导过程|证明过程"
)


def validate_outline_response(
    data: Any,
    source: list[dict],
    *,
    timestamp_tolerance: float = 1.5,
) -> dict[str, list[dict[str, Any]]]:
    """拒绝漂亮但不可追溯的目录；返回字段已标准化的响应。"""
    if not isinstance(data, dict) or set(data) != set(TOP_LEVEL_FIELDS):
        raise LLMError("Phase 2B JSON 顶层字段必须严格匹配 outline schema")
    if any(not isinstance(data[field], list) for field in TOP_LEVEL_FIELDS):
        raise LLMError("Phase 2B JSON 的所有顶层字段都必须是数组")
    if not source:
        raise LLMError("CLEANED 没有 segments，无法识别结构")

    source_ids = [int(item["id"]) for item in source]
    source_by_id = {int(item["id"]): item for item in source}
    source_positions = {segment_id: index for index, segment_id in enumerate(source_ids)}
    if len(source_ids) != len(source_by_id):
        raise LLMError("CLEANED 包含重复 segment id")

    topics = data["lecture_topics"]
    if not topics:
        raise LLMError("lecture_topics 不得为空")
    topic_ids: set[str] = set()
    topic_sources: dict[str, set[int]] = {}
    flattened: list[int] = []
    normalized_topics: list[dict[str, Any]] = []
    for index, item in enumerate(topics):
        topic = _validate_item(
            item,
            fields={"id", "title", "type", "start", "end", "source_segment_ids", "uncertain"},
            text_field="title",
            source_by_id=source_by_id,
            source_positions=source_positions,
        )
        if topic["type"] not in TOPIC_TYPES:
            raise LLMError(f"topic {topic['id']} type 非法：{topic['type']}")
        if topic["id"] in topic_ids:
            raise LLMError(f"重复 topic id：{topic['id']}")
        topic_ids.add(topic["id"])
        topic_sources[topic["id"]] = set(topic["source_segment_ids"])
        flattened.extend(topic["source_segment_ids"])
        expected_start = float(source_by_id[topic["source_segment_ids"][0]]["start"])
        if index + 1 < len(topics):
            following = topics[index + 1]
            next_ids = following.get("source_segment_ids") if isinstance(following, dict) else None
            if not isinstance(next_ids, list) or not next_ids:
                raise LLMError("下一 topic 缺少 source_segment_ids")
            next_id = next_ids[0]
            if next_id not in source_by_id:
                raise LLMError(f"topic 引用了未知 segment id：{next_id}")
            expected_end = float(source_by_id[next_id]["start"])
        else:
            expected_end = float(source_by_id[topic["source_segment_ids"][-1]]["end"])
        _check_time(topic, expected_start, expected_end, timestamp_tolerance)
        normalized_topics.append(topic)
    if flattened != source_ids:
        missing = [value for value in source_ids if value not in flattened]
        counts: dict[int, int] = {}
        for value in flattened:
            counts[value] = counts.get(value, 0) + 1
        duplicates = sorted(value for value, count in counts.items() if count > 1)
        raise LLMError(
            f"章节未按原顺序完整覆盖 CLEANED：missing={missing[:20]}, duplicates={duplicates[:20]}"
        )

    normalized: dict[str, list[dict[str, Any]]] = {"lecture_topics": normalized_topics}
    evidence_fields = {
        "subtopics": "title",
        "definitions": "label",
        "derivations": "label",
        "examples": "label",
        "teacher_emphasis": "label",
        "exam_tips": "label",
    }
    for category, text_field in evidence_fields.items():
        seen: set[str] = set()
        values: list[dict[str, Any]] = []
        fields = {"id", "topic_id", text_field, "start", "end", "source_segment_ids", "uncertain"}
        for raw in data[category]:
            item = _validate_item(
                raw,
                fields=fields,
                text_field=text_field,
                source_by_id=source_by_id,
                source_positions=source_positions,
            )
            _check_unique_id(item, category, seen)
            topic_id = item["topic_id"]
            if topic_id not in topic_ids:
                raise LLMError(f"{category} {item['id']} 引用了未知 topic：{topic_id}")
            if not set(item["source_segment_ids"]).issubset(topic_sources[topic_id]):
                raise LLMError(f"{category} {item['id']} 来源越出所属 topic")
            _check_source_bound_time(item, source_by_id, timestamp_tolerance)
            values.append(item)
        normalized[category] = values

    transitions: list[dict[str, Any]] = []
    seen_transitions: set[str] = set()
    transition_fields = {
        "id", "from_topic_id", "to_topic_id", "cue", "start", "end",
        "source_segment_ids", "uncertain",
    }
    for raw in data["transitions"]:
        item = _validate_item(
            raw,
            fields=transition_fields,
            text_field="cue",
            source_by_id=source_by_id,
            source_positions=source_positions,
        )
        _check_unique_id(item, "transitions", seen_transitions)
        if item["from_topic_id"] not in topic_ids or item["to_topic_id"] not in topic_ids:
            raise LLMError(f"transition {item['id']} 引用了未知 topic")
        if item["from_topic_id"] == item["to_topic_id"]:
            raise LLMError(f"transition {item['id']} 前后 topic 不能相同")
        _check_source_bound_time(item, source_by_id, timestamp_tolerance)
        transitions.append(item)
    normalized["transitions"] = transitions

    derivation_cues = {
        int(item["id"])
        for item in source
        if _STRONG_DERIVATION.search(str(item.get("text") or ""))
    }
    covered_derivations = {
        segment_id
        for item in normalized["derivations"]
        for segment_id in item["source_segment_ids"]
    }
    missing_derivations = derivation_cues - covered_derivations
    if missing_derivations:
        raise LLMError(
            f"检测到明确推导/证明提示但 derivations 未覆盖：{sorted(missing_derivations)[:20]}"
        )
    return normalized


def _validate_item(
    item: Any,
    *,
    fields: set[str],
    text_field: str,
    source_by_id: dict[int, dict],
    source_positions: dict[int, int],
) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != fields:
        raise LLMError(f"Phase 2B item 字段不符合 schema：expected={sorted(fields)}")
    if not isinstance(item["id"], str) or not item["id"].strip():
        raise LLMError("Phase 2B item id 必须是非空字符串")
    if not isinstance(item[text_field], str) or not item[text_field].strip():
        raise LLMError(f"Phase 2B item {item['id']} 的 {text_field} 不能为空")
    if len(item[text_field]) > 200 or "<input_json>" in item[text_field]:
        raise LLMError(f"Phase 2B item {item['id']} 文本异常或疑似 prompt echo")
    if "type" in fields and not isinstance(item["type"], str):
        raise LLMError(f"Phase 2B item {item['id']} type 必须是字符串")
    if "topic_id" in fields and (
        not isinstance(item["topic_id"], str) or not item["topic_id"].strip()
    ):
        raise LLMError(f"Phase 2B item {item['id']} topic_id 非法")
    for key in {"from_topic_id", "to_topic_id"} & fields:
        if not isinstance(item[key], str) or not item[key].strip():
            raise LLMError(f"Phase 2B item {item['id']} {key} 非法")
    if not isinstance(item["start"], (int, float)) or isinstance(item["start"], bool):
        raise LLMError(f"Phase 2B item {item['id']} start 必须是数字")
    if not isinstance(item["end"], (int, float)) or isinstance(item["end"], bool):
        raise LLMError(f"Phase 2B item {item['id']} end 必须是数字")
    if float(item["start"]) >= float(item["end"]):
        raise LLMError(f"Phase 2B item {item['id']} 时间范围非法")
    uncertain = item["uncertain"]
    if not isinstance(uncertain, list) or not all(isinstance(value, str) for value in uncertain):
        raise LLMError(f"Phase 2B item {item['id']} uncertain 必须是字符串数组")
    ids = item["source_segment_ids"]
    if (
        not isinstance(ids, list) or not ids
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in ids)
        or len(ids) != len(set(ids))
    ):
        raise LLMError(f"Phase 2B item {item['id']} source_segment_ids 非法")
    unknown = [value for value in ids if value not in source_by_id]
    if unknown:
        raise LLMError(f"Phase 2B item {item['id']} 引用了未知 segment id：{unknown[:20]}")
    positions = [source_positions[value] for value in ids]
    if positions != list(range(positions[0], positions[-1] + 1)):
        raise LLMError(f"Phase 2B item {item['id']} 来源必须连续且保持原顺序")
    result = dict(item)
    result["id"] = item["id"].strip()
    result[text_field] = item[text_field].strip()
    result["start"] = float(item["start"])
    result["end"] = float(item["end"])
    result["source_segment_ids"] = list(ids)
    result["uncertain"] = list(dict.fromkeys(value.strip() for value in uncertain if value.strip()))
    return result


def _check_unique_id(item: dict[str, Any], category: str, seen: set[str]) -> None:
    if item["id"] in seen:
        raise LLMError(f"{category} 包含重复 id：{item['id']}")
    seen.add(item["id"])


def _check_time(
    item: dict[str, Any], expected_start: float, expected_end: float, tolerance: float
) -> None:
    if abs(item["start"] - expected_start) > tolerance:
        raise LLMError(f"{item['id']} start 与来源时间轴不一致")
    if abs(item["end"] - expected_end) > tolerance:
        raise LLMError(f"{item['id']} end 与来源时间轴不一致")


def _check_source_bound_time(
    item: dict[str, Any], source_by_id: dict[int, dict], tolerance: float
) -> None:
    ids = item["source_segment_ids"]
    _check_time(
        item,
        float(source_by_id[ids[0]]["start"]),
        float(source_by_id[ids[-1]]["end"]),
        tolerance,
    )
