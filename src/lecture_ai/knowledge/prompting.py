"""Phase 2C prompt 加载与最小必要输入渲染。"""

from __future__ import annotations

import json
from pathlib import Path

from lecture_ai.errors import LLMError

PROMPT_FILENAME = "concept_extraction.md"


def load_knowledge_prompt(project_root: Path) -> tuple[Path, str]:
    path = project_root / "prompts" / PROMPT_FILENAME
    if not path.is_file():
        raise LLMError(f"缺少 Phase 2C prompt：{path}")
    template = path.read_text(encoding="utf-8")
    required = (
        "{{COURSE_NAME}}", "{{CONCEPT_THRESHOLD}}", "{{CLEANED_JSON}}", "{{OUTLINE_JSON}}",
    )
    for marker in required:
        if marker not in template:
            raise LLMError(f"Phase 2C prompt 缺少占位符：{marker}")
    return path, template


def render_knowledge_prompt(
    template: str,
    *,
    course_name: str,
    concept_threshold: float,
    segments: list[dict],
    outline: dict,
) -> str:
    cleaned = [
        {
            "id": int(item["id"]),
            "start": float(item["start"]),
            "end": float(item["end"]),
            "text": str(item.get("text") or ""),
            "uncertain": list(item.get("uncertain") or []),
            "visual_references": list(item.get("visual_references") or []),
        }
        for item in segments
    ]
    outline_fields = {
        key: outline[key]
        for key in (
            "lecture_topics", "subtopics", "definitions", "derivations", "examples",
            "teacher_emphasis", "exam_tips", "transitions",
        )
    }
    return (
        template.replace("{{COURSE_NAME}}", course_name)
        .replace("{{CONCEPT_THRESHOLD}}", f"{concept_threshold:.2f}")
        .replace("{{CLEANED_JSON}}", json.dumps(cleaned, ensure_ascii=False, indent=2))
        .replace("{{OUTLINE_JSON}}", json.dumps(outline_fields, ensure_ascii=False, indent=2))
    )
