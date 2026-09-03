"""Phase 2C：带来源约束的课堂知识抽取。"""

from lecture_ai.knowledge.models import KnowledgeOutcome
from lecture_ai.knowledge.pipeline import (
    KNOWLEDGE_JSON,
    UNRESOLVED_VISUAL_JSON,
    KnowledgePipeline,
)
from lecture_ai.knowledge.schema import KNOWLEDGE_RESPONSE_SCHEMA, validate_knowledge_response

__all__ = [
    "KNOWLEDGE_JSON",
    "UNRESOLVED_VISUAL_JSON",
    "KNOWLEDGE_RESPONSE_SCHEMA",
    "KnowledgeOutcome",
    "KnowledgePipeline",
    "validate_knowledge_response",
]
