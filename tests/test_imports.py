"""导入健康检查。

存在的理由：循环 import 只有在「某个模块被第一个导入」时才暴露。
pytest 按字母序收集文件，很容易因为别的测试先导入了某个包而把环掩盖掉
（本项目就真的踩过一次：ingestion.watcher 反向依赖 pipeline）。
所以这里为每个模块单独起一个解释器，逐个验证它能独立作为入口被导入。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: 每个顶层子包都要在这里列出。漏一个就等于那个包的循环 import 没人看着。
MODULES = [
    "lecture_ai",
    "lecture_ai.cli",
    "lecture_ai.config",
    "lecture_ai.errors",
    "lecture_ai.logging_setup",
    "lecture_ai.audio",
    "lecture_ai.audio_draft",
    "lecture_ai.cleaning",
    "lecture_ai.database",
    "lecture_ai.export_package",
    "lecture_ai.fusion",
    "lecture_ai.image_processing",
    "lecture_ai.ingestion",
    "lecture_ai.knowledge",
    "lecture_ai.llm",
    "lecture_ai.note",
    "lecture_ai.obsidian",
    "lecture_ai.pipeline",
    "lecture_ai.repair",
    "lecture_ai.session",
    "lecture_ai.structure",
    "lecture_ai.transcription",
    "lecture_ai.utils",
    "lecture_ai.web",
]


def test_module_list_covers_every_subpackage():
    """防止以后新增子包却忘了加进 MODULES —— 这个测试自己看着自己。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "lecture_ai"
    packages = {
        f"lecture_ai.{p.name}" for p in root.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file() and not p.name.startswith("_")
    }
    assert packages - set(MODULES) == set(), "有子包没有加进 MODULES"


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_standalone(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, f"{module} 无法独立导入：\n{result.stderr}"


def test_no_upward_dependency_from_domain_layers():
    """领域层不得反向依赖 pipeline / cli —— 依赖方向必须严格单向向下。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "lecture_ai"
    domain_dirs = ["ingestion", "session", "audio", "transcription", "database", "utils"]

    offenders = []
    for name in domain_dirs:
        for py in (src / name).rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for forbidden in ("lecture_ai.pipeline", "lecture_ai.cli"):
                if f"import {forbidden}" in text or f"from {forbidden}" in text:
                    offenders.append(f"{name}/{py.name} -> {forbidden}")
    assert not offenders, "领域层出现向上依赖：" + "; ".join(offenders)
