"""Phase 2D 严格编排 schema：完整引用知识项，不让 Markdown 隐藏来源。"""

from __future__ import annotations

from typing import Any

from lecture_ai.errors import LLMError

ITEM_ID_FIELDS = {
    "concepts": "concept_ids",
    "equations": "equation_ids",
    "examples": "example_ids",
    "teacher_emphasis": "teacher_emphasis_ids",
    "exam_tips": "exam_tip_ids",
    "common_errors": "common_error_ids",
    "open_questions": "open_question_ids",
    "uncertain_items": "uncertain_item_ids",
    "visual_references": "visual_reference_ids",
}
SECTION_FIELDS = (
    "id", "topic_id", "heading", "summary", "source_segment_ids",
    *ITEM_ID_FIELDS.values(),
)
DRAFT_FIELDS = ("title", "sections", "closing_summary")
_SOURCE_IDS = {
    "type": "array", "items": {"type": "integer"}, "minItems": 1, "uniqueItems": True,
}
_ITEM_IDS = {
    "type": "array", "items": {"type": "string"}, "uniqueItems": True,
}

DRAFT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "topic_id": {"type": "string"},
                    "heading": {"type": "string"},
                    "summary": {"type": "string"},
                    "source_segment_ids": _SOURCE_IDS,
                    **{field: _ITEM_IDS for field in ITEM_ID_FIELDS.values()},
                },
                "required": list(SECTION_FIELDS),
                "additionalProperties": False,
            },
        },
        "closing_summary": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "source_segment_ids": _SOURCE_IDS,
                },
                "required": ["content", "source_segment_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": list(DRAFT_FIELDS),
    "additionalProperties": False,
}


def validate_draft_response(
    data: Any,
    outline: dict[str, Any],
    knowledge: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != set(DRAFT_FIELDS):
        raise LLMError("Phase 2D JSON 顶层字段必须严格匹配 audio draft schema")
    title = _text(data.get("title"), "title", limit=200)
    topics = outline.get("lecture_topics")
    if not isinstance(topics, list) or not topics:
        raise LLMError("Phase 2D outline 缺少 lecture_topics")
    topic_ids = [str(item.get("id")) for item in topics]
    topic_sources = {
        str(item["id"]): [int(value) for value in item["source_segment_ids"]]
        for item in topics
    }
    flattened_sources = [value for values in topic_sources.values() for value in values]
    all_source_ids = set(flattened_sources)
    positions = {value: index for index, value in enumerate(flattened_sources)}

    item_maps: dict[str, dict[str, dict[str, Any]]] = {}
    for category, id_field in ITEM_ID_FIELDS.items():
        values = knowledge.get(category)
        if not isinstance(values, list):
            raise LLMError(f"Phase 2D knowledge 缺少 {category}")
        mapping: dict[str, dict[str, Any]] = {}
        for item in values:
            item_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(item_id, str) or not item_id or item_id in mapping:
                raise LLMError(f"knowledge {category} id 为空或重复：{item_id}")
            mapping[item_id] = item
        item_maps[id_field] = mapping

    sections = data.get("sections")
    if not isinstance(sections, list):
        raise LLMError("Phase 2D sections 必须是数组")
    received_topic_ids = [
        str(item.get("topic_id")) if isinstance(item, dict) else ""
        for item in sections
    ]
    if received_topic_ids != topic_ids:
        raise LLMError("Phase 2D sections 必须按原顺序一一覆盖全部 outline topics")

    used_section_ids: set[str] = set()
    used_items: dict[str, set[str]] = {field: set() for field in ITEM_ID_FIELDS.values()}
    normalized_sections: list[dict[str, Any]] = []
    for raw in sections:
        if not isinstance(raw, dict) or set(raw) != set(SECTION_FIELDS):
            raise LLMError("Phase 2D section 字段不符合严格 schema")
        section_id = _text(raw["id"], "section.id", limit=120)
        if section_id in used_section_ids:
            raise LLMError(f"重复 section id：{section_id}")
        used_section_ids.add(section_id)
        topic_id = str(raw["topic_id"])
        heading = _text(raw["heading"], f"section {section_id} heading", limit=300)
        summary = _text(raw["summary"], f"section {section_id} summary", limit=5000)
        source_ids = _source_ids(
            raw["source_segment_ids"],
            allowed=set(topic_sources[topic_id]),
            positions=positions,
            label=f"section {section_id}",
        )
        required_sources: set[int] = set()
        normalized = {
            "id": section_id,
            "topic_id": topic_id,
            "heading": heading,
            "summary": summary,
            "source_segment_ids": source_ids,
        }
        for id_field, mapping in item_maps.items():
            values = raw[id_field]
            if (
                not isinstance(values, list)
                or not all(isinstance(value, str) and value for value in values)
                or len(values) != len(set(values))
            ):
                raise LLMError(f"section {section_id} 的 {id_field} 非法")
            unknown = [value for value in values if value not in mapping]
            if unknown:
                raise LLMError(f"section {section_id} 引用了未知 {id_field}：{unknown[:20]}")
            duplicates = [value for value in values if value in used_items[id_field]]
            if duplicates:
                raise LLMError(f"{id_field} 被重复编排：{duplicates[:20]}")
            wrong_topic = [
                value for value in values
                if str(mapping[value].get("topic_id")) != topic_id
            ]
            if wrong_topic:
                raise LLMError(f"section {section_id} 编排了其他 topic 的 {id_field}")
            used_items[id_field].update(values)
            normalized[id_field] = list(values)
            for value in values:
                required_sources.update(
                    int(source_id)
                    for source_id in mapping[value].get("source_segment_ids", [])
                )
        missing_sources = required_sources - set(source_ids)
        if missing_sources:
            raise LLMError(
                f"section {section_id} provenance 未覆盖知识项来源：{sorted(missing_sources)[:20]}"
            )
        normalized_sections.append(normalized)

    for id_field, mapping in item_maps.items():
        missing = set(mapping) - used_items[id_field]
        if missing:
            raise LLMError(f"Phase 2D 丢失 {id_field}：{sorted(missing)[:20]}")

    closing = data.get("closing_summary")
    if not isinstance(closing, list) or len(closing) > 5:
        raise LLMError("closing_summary 必须是最多 5 项的数组")
    normalized_closing: list[dict[str, Any]] = []
    for index, raw in enumerate(closing):
        if not isinstance(raw, dict) or set(raw) != {"content", "source_segment_ids"}:
            raise LLMError("closing_summary item 字段不符合严格 schema")
        normalized_closing.append({
            "content": _text(raw["content"], f"closing_summary[{index}]", limit=1000),
            "source_segment_ids": _source_ids(
                raw["source_segment_ids"],
                allowed=all_source_ids,
                positions=positions,
                label=f"closing_summary[{index}]",
            ),
        })
    return {
        "title": title,
        "sections": normalized_sections,
        "closing_summary": normalized_closing,
    }


def _text(value: Any, label: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LLMError(f"{label} 必须是非空字符串")
    text = value.strip()
    if (
        len(text) > limit or "[[" in text or "<outline_json>" in text
        or "<knowledge_json>" in text or "<unresolved_visual_json>" in text
        or "\n" in text or "\r" in text or "> [!" in text
    ):
        raise LLMError(f"{label} 异常、包含 WikiLink 或疑似 prompt echo")
    return text


def _source_ids(
    values: Any,
    *,
    allowed: set[int],
    positions: dict[int, int],
    label: str,
) -> list[int]:
    if (
        not isinstance(values, list) or not values
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in values)
        or len(values) != len(set(values))
    ):
        raise LLMError(f"{label} source_segment_ids 非法")
    unknown = [value for value in values if value not in allowed]
    if unknown:
        raise LLMError(f"{label} source_segment_ids 越界：{unknown[:20]}")
    order = [positions[value] for value in values]
    if order != sorted(order):
        raise LLMError(f"{label} source_segment_ids 被重排")
    return list(values)
