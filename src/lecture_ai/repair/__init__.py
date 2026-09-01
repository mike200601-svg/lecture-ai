"""Phase 1.5：选择性重转录。"""

from lecture_ai.repair.detector import (
    decide_repair,
    detect_suspicious_regions,
    measure_segments,
    measure_text,
)
from lecture_ai.repair.models import (
    RepairDecision,
    RepairOutcome,
    SuspiciousRegion,
    TextMetrics,
)
from lecture_ai.repair.pipeline import (
    REPAIRED_JSON,
    REPAIRED_MD,
    RepairPipeline,
    extract_wav_region,
    merge_repairs,
)

__all__ = [
    "TextMetrics",
    "SuspiciousRegion",
    "RepairDecision",
    "RepairOutcome",
    "measure_text",
    "measure_segments",
    "detect_suspicious_regions",
    "decide_repair",
    "RepairPipeline",
    "REPAIRED_JSON",
    "REPAIRED_MD",
    "extract_wav_region",
    "merge_repairs",
]
