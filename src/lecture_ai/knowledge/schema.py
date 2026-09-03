"""Phase 2C 知识响应 schema 与证据路由质量门。"""

from __future__ import annotations

import re
from typing import Any

from lecture_ai.errors import LLMError

KNOWLEDGE_FIELDS = (
    "concepts", "equations", "examples", "teacher_emphasis", "exam_tips",
    "common_errors", "open_questions", "visual_references", "uncertain_items",
)
_SOURCE_IDS = {
    "type": "array", "items": {"type": "integer"}, "minItems": 1, "uniqueItems": True,
}
_UNCERTAIN = {"type": "array", "items": {"type": "string"}}


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_BASE = {
    "id": {"type": "string"},
    "topic_id": {"type": "string"},
}
_TRACE = {
    "source_segment_ids": _SOURCE_IDS,
    "uncertain": _UNCERTAIN,
}

CONCEPT_SCHEMA = _object_schema({
    **_BASE,
    "name": {"type": "string"},
    "explanation": {"type": "string"},
    "importance": {"type": "number", "minimum": 0, "maximum": 1},
    **_TRACE,
})
EQUATION_SCHEMA = _object_schema({
    **_BASE,
    "name": {"type": "string"},
    "expression": {"type": "string"},
    "status": {"type": "string", "enum": ["complete", "incomplete", "uncertain"]},
    **_TRACE,
})
EXAMPLE_SCHEMA = _object_schema({
    **_BASE,
    "title": {"type": "string"},
    "content": {"type": "string"},
    **_TRACE,
})
SIMPLE_SCHEMA = _object_schema({
    **_BASE,
    "content": {"type": "string"},
    **_TRACE,
})
VISUAL_SCHEMA = _object_schema({
    **_BASE,
    "timestamp": {"type": "number"},
    "context": {"type": "string"},
    "reference_type": {
        "type": "string",
        "enum": ["board", "slide", "image", "diagram", "formula", "other"],
    },
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    **_TRACE,
})
UNCERTAIN_ITEM_SCHEMA = _object_schema({
    **_BASE,
    "content": {"type": "string"},
    "reason": {"type": "string"},
    "source_segment_ids": _SOURCE_IDS,
})

KNOWLEDGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "concepts": {"type": "array", "items": CONCEPT_SCHEMA},
        "equations": {"type": "array", "items": EQUATION_SCHEMA},
        "examples": {"type": "array", "items": EXAMPLE_SCHEMA},
        "teacher_emphasis": {"type": "array", "items": SIMPLE_SCHEMA},
        "exam_tips": {"type": "array", "items": SIMPLE_SCHEMA},
        "common_errors": {"type": "array", "items": SIMPLE_SCHEMA},
        "open_questions": {"type": "array", "items": SIMPLE_SCHEMA},
        "visual_references": {"type": "array", "items": VISUAL_SCHEMA},
        "uncertain_items": {"type": "array", "items": UNCERTAIN_ITEM_SCHEMA},
    },
    "required": list(KNOWLEDGE_FIELDS),
    "additionalProperties": False,
}

_FORMULA_CUE = re.compile(
    r"\d|[=+\-×÷*/^]|等于|乘以|除以|次方|公式|方程|函数|表达式|进制|幂"
)


def validate_knowledge_response(
    data: Any,
    source: list[dict],
    outline: dict,
    *,
    concept_threshold: float,
    timestamp_tolerance: float = 1.5,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(data, dict) or set(data) != set(KNOWLEDGE_FIELDS):
        raise LLMError("Phase 2C JSON 顶层字段必须严格匹配 knowledge schema")
    if any(not isinstance(data[field], list) for field in KNOWLEDGE_FIELDS):
        raise LLMError("Phase 2C JSON 的所有顶层字段都必须是数组")
    source_by_id = {int(item["id"]): item for item in source}
    source_positions = {
        int(item["id"]): index for index, item in enumerate(source)
    }
    if not source_by_id or len(source_by_id) != len(source):
        raise LLMError("Phase 2C 来源 segments 为空或包含重复 id")
    topics = outline.get("lecture_topics")
    if not isinstance(topics, list) or not topics:
        raise LLMError("outline 缺少 lecture_topics")
    topic_sources = {
        str(topic["id"]): set(int(value) for value in topic["source_segment_ids"])
        for topic in topics
    }

    normalized: dict[str, list[dict[str, Any]]] = {}
    normalized["concepts"] = _validate_category(
        data["concepts"], "concepts",
        fields={
            "id", "topic_id", "name", "explanation", "importance",
            "source_segment_ids", "uncertain",
        },
        text_fields=("name", "explanation"),
        source_by_id=source_by_id,
        source_positions=source_positions,
        topic_sources=topic_sources,
    )
    for item in normalized["concepts"]:
        importance = item["importance"]
        if not _number(importance) or not 0 <= float(importance) <= 1:
            raise LLMError(f"concept {item['id']} importance 必须位于 0..1")
        if float(importance) < concept_threshold:
            raise LLMError(
                f"concept {item['id']} importance={importance} 低于阈值 {concept_threshold}"
            )
        item["importance"] = float(importance)

    normalized["equations"] = _validate_category(
        data["equations"], "equations",
        fields={
            "id", "topic_id", "name", "expression", "status",
            "source_segment_ids", "uncertain",
        },
        text_fields=("name", "expression"),
        source_by_id=source_by_id,
        source_positions=source_positions,
        topic_sources=topic_sources,
    )
    for item in normalized["equations"]:
        if item["status"] not in {"complete", "incomplete", "uncertain"}:
            raise LLMError(f"equation {item['id']} status 非法")
        evidence = "".join(
            str(source_by_id[value].get("text") or "")
            for value in item["source_segment_ids"]
        )
        if not _FORMULA_CUE.search(evidence):
            raise LLMError(f"equation {item['id']} 来源中没有公式/计算证据，疑似编造")
        if item["status"] != "complete" and not item["uncertain"]:
            raise LLMError(f"equation {item['id']} 不完整时必须说明 uncertainty")

    normalized["examples"] = _validate_category(
        data["examples"], "examples",
        fields={"id", "topic_id", "title", "content", "source_segment_ids", "uncertain"},
        text_fields=("title", "content"),
        source_by_id=source_by_id,
        source_positions=source_positions,
        topic_sources=topic_sources,
    )
    for category in ("teacher_emphasis", "exam_tips", "common_errors", "open_questions"):
        normalized[category] = _validate_category(
            data[category], category,
            fields={"id", "topic_id", "content", "source_segment_ids", "uncertain"},
            text_fields=("content",),
            source_by_id=source_by_id,
            source_positions=source_positions,
            topic_sources=topic_sources,
        )

    normalized["uncertain_items"] = _validate_category(
        data["uncertain_items"], "uncertain_items",
        fields={"id", "topic_id", "content", "reason", "source_segment_ids"},
        text_fields=("content", "reason"),
        source_by_id=source_by_id,
        source_positions=source_positions,
        topic_sources=topic_sources,
        has_uncertain=False,
        blank_ok_fields=frozenset({"content"}),
    )
    uncertain_coverage = _coverage(normalized["uncertain_items"])

    visuals = _validate_category(
        data["visual_references"], "visual_references",
        fields={
            "id", "topic_id", "timestamp", "context", "reference_type", "confidence",
            "source_segment_ids", "uncertain",
        },
        text_fields=("context",),
        source_by_id=source_by_id,
        source_positions=source_positions,
        topic_sources=topic_sources,
    )
    for item in visuals:
        if item["reference_type"] not in {
            "board", "slide", "image", "diagram", "formula", "other"
        }:
            raise LLMError(f"visual_reference {item['id']} reference_type 非法")
        if not _number(item["confidence"]) or not 0 <= float(item["confidence"]) <= 1:
            raise LLMError(f"visual_reference {item['id']} confidence 必须位于 0..1")
        expected = float(source_by_id[item["source_segment_ids"][0]]["start"])
        if not _number(item["timestamp"]) or abs(float(item["timestamp"]) - expected) > timestamp_tolerance:
            raise LLMError(f"visual_reference {item['id']} timestamp 与来源不一致")
        item["timestamp"] = float(item["timestamp"])
        item["confidence"] = float(item["confidence"])
    normalized["visual_references"] = visuals

    source_uncertain = {
        int(item["id"]) for item in source if item.get("uncertain")
    }
    missing_uncertain = source_uncertain - uncertain_coverage
    if missing_uncertain:
        raise LLMError(
            f"CLEANED uncertainty 未进入 uncertain_items：{sorted(missing_uncertain)[:20]}"
        )
    visual_source_ids = {
        int(item["id"]) for item in source if item.get("visual_references")
    }
    missing_visual = visual_source_ids - _coverage(visuals)
    if missing_visual:
        raise LLMError(
            f"CLEANED visual reference 未进入 unresolved queue：{sorted(missing_visual)[:20]}"
        )

    # 不完整公式必须可追溯到显式 uncertainty，但只要求命中来源区间中的问题段，
    # 不要求整段证据区间。一条推导常跨十几个 segment，问题往往只在其中一两处；
    # 强制全覆盖会把清晰语音也标成存疑，而 Phase 2D 会把每条 uncertainty 渲染成
    # [!question]，等于凭空制造疑点。equation 自身的 uncertain 非空由上面单独把关。
    unrouted_equations = [
        item["id"]
        for item in normalized["equations"]
        if item["status"] != "complete"
        and not set(item["source_segment_ids"]) & uncertain_coverage
    ]
    if unrouted_equations:
        raise LLMError(
            f"不完整公式未进入 uncertain_items：{unrouted_equations[:20]}"
        )

    retention_map = {
        "definitions": "concepts",
        "derivations": "equations",
        "examples": "examples",
        "teacher_emphasis": "teacher_emphasis",
        "exam_tips": "exam_tips",
    }
    for outline_category, knowledge_category in retention_map.items():
        required = _coverage(outline.get(outline_category) or [])
        retained = _coverage(normalized[knowledge_category]) | uncertain_coverage
        missing = required - retained
        if missing:
            raise LLMError(
                f"outline {outline_category} 在知识层丢失来源：{sorted(missing)[:20]}"
            )
    return normalized


def _sources_blank(ids: Any, source_by_id: dict[int, dict]) -> bool:
    """来源 segment 在 CLEANED 中是否全部为空。

    Phase 2A 会把术语串入、模板幻觉和复读副本审计清空并留下 delete 记录。这些
    segment 没有正文可引用，要求引用它们的条目写出非空文本等于逼模型编内容。
    """
    if not isinstance(ids, list) or not ids:
        return False
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value in source_by_id
        for value in ids
    ):
        return False
    return all(
        not str(source_by_id[value].get("text") or "").strip() for value in ids
    )


def _validate_category(
    values: list[Any],
    category: str,
    *,
    fields: set[str],
    text_fields: tuple[str, ...],
    source_by_id: dict[int, dict],
    source_positions: dict[int, int],
    topic_sources: dict[str, set[int]],
    has_uncertain: bool = True,
    blank_ok_fields: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    if len(values) > len(source_by_id):
        raise LLMError(f"{category} 数量异常，超过来源 segment 数")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise LLMError(f"{category} item 字段不符合严格 schema")
        item_id = raw.get("id")
        topic_id = raw.get("topic_id")
        if not isinstance(item_id, str) or not item_id.strip() or item_id in seen:
            raise LLMError(f"{category} item id 为空或重复：{item_id}")
        if not isinstance(topic_id, str) or topic_id not in topic_sources:
            raise LLMError(f"{category} {item_id} 引用了未知 topic：{topic_id}")
        seen.add(item_id)
        for field in text_fields:
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                if field in blank_ok_fields and _sources_blank(
                    raw.get("source_segment_ids"), source_by_id
                ):
                    continue
                raise LLMError(f"{category} {item_id} 的 {field} 不能为空")
            if len(value) > 2000 or "<cleaned_json>" in value or "<outline_json>" in value:
                raise LLMError(f"{category} {item_id} 文本异常或疑似 prompt echo")
        ids = raw.get("source_segment_ids")
        if (
            not isinstance(ids, list) or not ids
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in ids)
            or len(ids) != len(set(ids))
        ):
            raise LLMError(f"{category} {item_id} source_segment_ids 非法")
        unknown = [value for value in ids if value not in source_by_id]
        if unknown:
            raise LLMError(f"{category} {item_id} 引用了未知 segment id：{unknown[:20]}")
        positions = [source_positions[value] for value in ids]
        if positions != sorted(positions):
            raise LLMError(f"{category} {item_id} source_segment_ids 被重排")
        if not set(ids).issubset(topic_sources[topic_id]):
            raise LLMError(f"{category} {item_id} 来源越出所属 topic")
        if has_uncertain:
            uncertain = raw.get("uncertain")
            if not isinstance(uncertain, list) or not all(
                isinstance(value, str) for value in uncertain
            ):
                raise LLMError(f"{category} {item_id} uncertain 必须是字符串数组")
        normalized = dict(raw)
        normalized["id"] = item_id.strip()
        normalized["source_segment_ids"] = list(ids)
        for field in text_fields:
            normalized[field] = raw[field].strip()
        if has_uncertain:
            normalized["uncertain"] = list(dict.fromkeys(
                value.strip() for value in raw["uncertain"] if value.strip()
            ))
        result.append(normalized)
    return result


def _coverage(items: list[dict[str, Any]]) -> set[int]:
    return {
        int(value)
        for item in items
        for value in item.get("source_segment_ids", [])
    }


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
