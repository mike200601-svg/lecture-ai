"""转录模块。上层只依赖 base 里的抽象 + registry 的工厂函数。"""

from lecture_ai.transcription.base import (
    ProgressCallback,
    TranscribeOptions,
    Transcriber,
    TranscriptResult,
    TranscriptSegment,
    merge_chunk_results,
)
from lecture_ai.transcription.glossary import Glossary, load_glossary
from lecture_ai.transcription.registry import (
    ModelCacheStatus,
    build_transcriber,
    find_cached_model,
    inspect_model_cache,
    resolve_model_reference,
    validate_local_model,
)
from lecture_ai.transcription.writer import (
    TRANSCRIPT_JSON,
    TRANSCRIPT_MD,
    TranscriptFiles,
    is_valid_transcript,
    read_transcript,
    write_transcript,
)

__all__ = [
    "Transcriber",
    "TranscriptResult",
    "TranscriptSegment",
    "TranscribeOptions",
    "ProgressCallback",
    "merge_chunk_results",
    "build_transcriber",
    "find_cached_model",
    "inspect_model_cache",
    "resolve_model_reference",
    "validate_local_model",
    "ModelCacheStatus",
    "Glossary",
    "load_glossary",
    "write_transcript",
    "read_transcript",
    "is_valid_transcript",
    "TranscriptFiles",
    "TRANSCRIPT_JSON",
    "TRANSCRIPT_MD",
]
