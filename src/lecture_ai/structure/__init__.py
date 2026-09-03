"""Phase 2B：基于 CLEANED 的课堂结构识别。"""

from lecture_ai.structure.models import StructureOutcome
from lecture_ai.structure.pipeline import OUTLINE_JSON, StructurePipeline
from lecture_ai.structure.schema import OUTLINE_RESPONSE_SCHEMA, validate_outline_response

__all__ = [
    "OUTLINE_JSON",
    "OUTLINE_RESPONSE_SCHEMA",
    "StructureOutcome",
    "StructurePipeline",
    "validate_outline_response",
]
