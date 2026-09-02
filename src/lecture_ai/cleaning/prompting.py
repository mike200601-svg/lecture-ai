"""外部 prompt 加载与严格变量渲染。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from lecture_ai.errors import LLMError

PROMPT_FILENAME = "transcript_clean.md"
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def load_clean_prompt(project_root: Path) -> tuple[Path, str]:
    path = project_root / "prompts" / PROMPT_FILENAME
    if not path.exists():
        raise LLMError(f"缺少 Phase 2A prompt：{path}")
    return path, path.read_text(encoding="utf-8")


def render_clean_prompt(
    template: str,
    *,
    mode: str,
    course_name: str,
    glossary: list[str],
    segments: list[dict],
    boundary_context: str = "无",
) -> str:
    values = {
        "mode": mode,
        "course_name": course_name,
        "glossary": "、".join(glossary) if glossary else "（无）",
        "segments_json": json.dumps(segments, ensure_ascii=False, indent=2),
        "boundary_context": boundary_context or "无",
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    remaining = sorted(set(_PLACEHOLDER.findall(rendered)))
    if remaining:
        raise LLMError(f"prompt 存在未提供变量：{', '.join(remaining)}")
    return rendered
