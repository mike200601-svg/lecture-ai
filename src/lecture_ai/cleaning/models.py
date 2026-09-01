"""Phase 2A 清洗数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    core_start: float
    core_end: float
    window_start: float
    window_end: float
    segment_ids: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["segment_ids"] = list(self.segment_ids)
        return data


@dataclass
class CleanOutcome:
    session_id: str
    source_layer: str
    chunks_planned: int
    chunks_processed: int = 0
    boundaries_processed: int = 0
    reused: bool = False
    dry_run: bool = False
    partial: bool = False
    output_json: str | None = None
    output_md: str | None = None
    elapsed_sec: float = 0.0
    message: str = ""
    chunks: list[dict[str, Any]] = field(default_factory=list)
