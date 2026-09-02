"""外部 prompt 加载与严格变量渲染。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from lecture_ai.errors import LLMError

PROMPT_FILENAME = "transcript_clean.md"
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
FORMULA_GUARD_VERSION = "formula-token-lock-v1"
_FORMULA_GUARD_CUE = re.compile(
    r"公式|等式|多项式|次方|乘以|除以|进制|系数|余数|整数|小数|"
    r"(?:^|[^A-Za-z])[KkSsNnMm](?:[_\-\d{]|$)"
)
_FORMULA_GUARD = """## 本块公式与数值硬约束

本块含数字、公式或变量。数字、运算关系、指数、下标和变量属于受保护原文：
- 不得根据数学常识、相邻例题或计算结果补出原文没有明确说出的数字/字母/符号。
- 不得把疑似连读数字（如 `234`）自行解释成“2 的 3 次方”等公式。
- 不得为了让例题成立而改动数字；不得补齐漏识别的位数或变量。
- 只要原文存在歧义，就原样保留，并在 `uncertain` 标明疑点。宁可保留坏转录，不能伪造好公式。
"""


def needs_formula_guard(segments: list[dict]) -> bool:
    return any(_FORMULA_GUARD_CUE.search(str(item.get("text") or "")) for item in segments)


def clean_prompt_policy_key(segments: list[dict]) -> str:
    return FORMULA_GUARD_VERSION if needs_formula_guard(segments) else ""


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
    if needs_formula_guard(segments):
        rendered = rendered.replace("<input_json>", _FORMULA_GUARD + "\n<input_json>", 1)
    return rendered
