"""Phase 2D prompt 加载与最小必要输入渲染。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lecture_ai.errors import LLMError

PROMPT_FILENAME = "lecture_note.md"


def load_draft_prompt(project_root: Path) -> tuple[Path, str]:
    path = project_root / "prompts" / PROMPT_FILENAME
    if not path.is_file():
        raise LLMError(f"缺少 Phase 2D prompt：{path}")
    template = path.read_text(encoding="utf-8")
    required = (
        "{{COURSE_NAME}}", "{{DATE}}", "{{SESSION_ID}}", "{{OUTLINE_JSON}}",
        "{{KNOWLEDGE_JSON}}", "{{UNRESOLVED_VISUAL_JSON}}",
    )
    for marker in required:
        if marker not in template:
            raise LLMError(f"Phase 2D prompt 缺少占位符：{marker}")
    return path, template


def render_draft_prompt(
    template: str,
    *,
    course_name: str,
    date: str,
    session_id: str,
    outline: dict[str, Any],
    knowledge: dict[str, Any],
    unresolved_visual: dict[str, Any],
) -> str:
    outline_payload = {
        key: outline[key]
        for key in (
            "lecture_topics", "subtopics", "definitions", "derivations", "examples",
            "teacher_emphasis", "exam_tips", "transitions",
        )
    }
    knowledge_payload = {
        key: knowledge[key]
        for key in (
            "concepts", "equations", "derivations", "examples", "teacher_emphasis",
            "exam_tips", "common_errors", "open_questions", "uncertain_items",
        )
    }
    visual_payload = {"items": unresolved_visual["items"]}
    return (
        template.replace("{{COURSE_NAME}}", course_name)
        .replace("{{DATE}}", date)
        .replace("{{SESSION_ID}}", session_id)
        .replace("{{OUTLINE_JSON}}", json.dumps(outline_payload, ensure_ascii=False, indent=2))
        .replace("{{KNOWLEDGE_JSON}}", json.dumps(knowledge_payload, ensure_ascii=False, indent=2))
        .replace(
            "{{UNRESOLVED_VISUAL_JSON}}",
            json.dumps(visual_payload, ensure_ascii=False, indent=2),
        )
    )
