"""把已校验的 Phase 2D 编排 JSON 确定性渲染为可审计 Markdown。"""

from __future__ import annotations

import json
from typing import Any

from lecture_ai.utils.timefmt import hhmmss


def render_audio_draft(
    *,
    session_id: str,
    course: str,
    date: str,
    draft: dict[str, Any],
    outline: dict[str, Any],
    knowledge: dict[str, Any],
    unresolved_visual: dict[str, Any],
) -> str:
    topics = {str(item["id"]): item for item in outline["lecture_topics"]}
    maps = {
        category: {str(item["id"]): item for item in knowledge[category]}
        for category in (
            "concepts", "equations", "examples", "teacher_emphasis", "exam_tips",
            "common_errors", "open_questions", "uncertain_items",
        )
    }
    maps["visual_references"] = {
        str(item["id"]): item for item in unresolved_visual["items"]
    }
    lines = [
        "---",
        f"course: {json.dumps(course, ensure_ascii=False)}",
        f"date: {json.dumps(date, ensure_ascii=False)}",
        f"session_id: {json.dumps(session_id, ensure_ascii=False)}",
        "source_layer: AUDIO_ONLY",
        "final: false",
        "---",
        "",
        f"# {_md(draft['title'])}",
        "",
        "> [!warning] Audio-only 草稿",
        "> 本文仅由已核验的课堂音频文字层生成；板书、课件和教材尚未融合，不能视为最终笔记。",
        "",
        "## 本节框架",
        "",
    ]
    for topic in outline["lecture_topics"]:
        lines.append(
            f"- [{hhmmss(float(topic['start']))}–{hhmmss(float(topic['end']))}] "
            f"{_md(topic['title'])}"
        )
    for index, section in enumerate(draft["sections"], start=1):
        topic = topics[section["topic_id"]]
        lines.extend([
            "",
            f"## {index}. {_md(section['heading'])}",
            "",
            _source_line(section["source_segment_ids"], topic),
            "",
            _md(section["summary"]),
        ])
        _render_concepts(lines, section["concept_ids"], maps["concepts"])
        _render_equations(lines, section["equation_ids"], maps["equations"])
        _render_examples(lines, section["example_ids"], maps["examples"])
        _render_callouts(
            lines, section["teacher_emphasis_ids"], maps["teacher_emphasis"],
            "important", "老师强调",
        )
        _render_callouts(
            lines, section["exam_tip_ids"], maps["exam_tips"],
            "tip", "考试提示",
        )
        _render_callouts(
            lines, section["common_error_ids"], maps["common_errors"],
            "warning", "常见错误",
        )
        _render_callouts(
            lines, section["open_question_ids"], maps["open_questions"],
            "question", "课堂未决问题",
        )
        _render_uncertain(
            lines, section["uncertain_item_ids"], maps["uncertain_items"]
        )
        _render_visual(
            lines, section["visual_reference_ids"], maps["visual_references"]
        )
    if draft["closing_summary"]:
        lines.extend(["", "## 本节小结", ""])
        for item in draft["closing_summary"]:
            lines.append(f"- {_md(item['content'])} {_sources(item['source_segment_ids'])}")
    lines.extend(["", "---", "", "_本文件是 Phase 2D audio-only draft，等待 Phase 3 视觉融合与人工验收。_", ""])
    text = "\n".join(lines)
    if "[[" in text:
        raise ValueError("audio draft renderer 不得生成 WikiLink")
    return text


def _render_concepts(lines: list[str], ids: list[str], mapping: dict[str, dict]) -> None:
    if not ids:
        return
    lines.extend(["", "### 核心概念", ""])
    for item_id in ids:
        item = mapping[item_id]
        lines.append(
            f"- **{_md(item['name'])}**：{_md(item['explanation'])} "
            f"{_sources(item['source_segment_ids'])}"
        )
        _append_uncertainty(lines, item.get("uncertain") or [])


def _render_equations(lines: list[str], ids: list[str], mapping: dict[str, dict]) -> None:
    if not ids:
        return
    lines.extend(["", "### 公式与推导", ""])
    for item_id in ids:
        item = mapping[item_id]
        expression = _md(item["expression"])
        evidence = _sources(item["source_segment_ids"])
        if item["status"] == "complete":
            formula = expression if "$" in expression else f"${expression}$"
            lines.append(f"- **{_md(item['name'])}**：{formula} {evidence}")
            _append_uncertainty(lines, item.get("uncertain") or [])
        else:
            lines.extend([
                "> [!question] 公式尚未核验",
                f"> **{_md(item['name'])}**：{expression}",
                f"> 状态：{item['status']}；{evidence}",
            ])
            for reason in item.get("uncertain") or []:
                lines.append(f"> - {_md(reason)}")


def _render_examples(lines: list[str], ids: list[str], mapping: dict[str, dict]) -> None:
    if not ids:
        return
    lines.extend(["", "### 课堂例题 / 示例", ""])
    for item_id in ids:
        item = mapping[item_id]
        lines.append(
            f"- **{_md(item['title'])}**：{_md(item['content'])} "
            f"{_sources(item['source_segment_ids'])}"
        )
        _append_uncertainty(lines, item.get("uncertain") or [])


def _render_callouts(
    lines: list[str],
    ids: list[str],
    mapping: dict[str, dict],
    kind: str,
    title: str,
) -> None:
    for item_id in ids:
        item = mapping[item_id]
        lines.extend([
            "",
            f"> [!{kind}] {title}",
            f"> {_md(item['content'])} {_sources(item['source_segment_ids'])}",
        ])
        for reason in item.get("uncertain") or []:
            lines.append(f"> - 不确定：{_md(reason)}")


def _render_uncertain(lines: list[str], ids: list[str], mapping: dict[str, dict]) -> None:
    for item_id in ids:
        item = mapping[item_id]
        lines.extend([
            "",
            "> [!question] 音频内容待核验",
            f"> {_md(item['content'])} {_sources(item['source_segment_ids'])}",
            f"> 原因：{_md(item['reason'])}",
        ])


def _render_visual(lines: list[str], ids: list[str], mapping: dict[str, dict]) -> None:
    for item_id in ids:
        item = mapping[item_id]
        lines.extend([
            "",
            "> [!question] 待板书 / 课件补充",
            f"> [{hhmmss(float(item['timestamp']))}] {_md(item['context'])}",
            f"> 类型：{item['reference_type']}；状态：unresolved；"
            f"{_sources(item['source_segment_ids'])}",
        ])


def _append_uncertainty(lines: list[str], values: list[str]) -> None:
    for value in values:
        lines.append(f"  - 不确定：{_md(value)}")


def _source_line(ids: list[int], topic: dict[str, Any]) -> str:
    return (
        f"`音频 {hhmmss(float(topic['start']))}–{hhmmss(float(topic['end']))}` "
        f"{_sources(ids)}"
    )


def _sources(ids: list[int]) -> str:
    return "〔segments: " + ", ".join(str(value) for value in ids) + "〕"


def _md(value: Any) -> str:
    return (
        str(value)
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("[[", r"\[\[")
        .replace("]]", r"\]\]")
        .strip()
    )
