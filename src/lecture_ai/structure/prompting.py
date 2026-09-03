"""Phase 2B 外部 prompt 加载和渲染。"""

from __future__ import annotations

import json
from pathlib import Path

from lecture_ai.errors import LLMError

PROMPT_FILENAME = "chapter_detection.md"


def load_structure_prompt(project_root: Path) -> tuple[Path, str]:
    path = project_root / "prompts" / PROMPT_FILENAME
    if not path.is_file():
        raise LLMError(f"缺少 Phase 2B prompt：{path}")
    template = path.read_text(encoding="utf-8")
    for marker in ("{{COURSE_NAME}}", "{{INPUT_JSON}}"):
        if marker not in template:
            raise LLMError(f"Phase 2B prompt 缺少占位符：{marker}")
    return path, template


def render_structure_prompt(
    template: str,
    *,
    course_name: str,
    segments: list[dict],
) -> str:
    payload = [
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
    return (
        template.replace("{{COURSE_NAME}}", course_name)
        .replace("{{INPUT_JSON}}", json.dumps(payload, ensure_ascii=False, indent=2))
    )
