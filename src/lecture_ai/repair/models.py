"""Phase 1.5 的纯数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TextMetrics:
    character_count: int
    utf8_bytes: int
    compression_ratio: float
    unique_char_ratio: float
    repeated_ngram_ratio: float
    longest_run: int
    no_speech_mean: float | None
    anomaly_score: float
    suspicious: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SuspiciousRegion:
    region_id: int
    start: float
    end: float
    window_start: float
    window_end: float
    segment_ids: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    original_metrics: TextMetrics | None = None
    text_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.original_metrics is not None:
            data["original_metrics"] = self.original_metrics.to_dict()
        return data


@dataclass(frozen=True)
class RepairDecision:
    accepted: bool
    reason: str
    improvement_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairOutcome:
    session_id: str
    regions_detected: int
    regions_processed: int = 0
    regions_accepted: int = 0
    reused: bool = False
    dry_run: bool = False
    output_json: str | None = None
    output_md: str | None = None
    elapsed_sec: float = 0.0
    message: str = ""
    regions: list[dict[str, Any]] = field(default_factory=list)
