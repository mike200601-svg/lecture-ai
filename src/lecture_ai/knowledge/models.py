"""Phase 2C 运行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnowledgeOutcome:
    session_id: str
    source_segments: int
    reused: bool = False
    dry_run: bool = False
    partial: bool = False
    output_json: str | None = None
    unresolved_visual_json: str | None = None
    elapsed_sec: float = 0.0
    message: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
