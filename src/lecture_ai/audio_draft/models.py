"""Phase 2D 运行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AudioDraftOutcome:
    session_id: str
    topic_count: int
    reused: bool = False
    dry_run: bool = False
    partial: bool = False
    output_json: str | None = None
    output_md: str | None = None
    elapsed_sec: float = 0.0
    message: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
