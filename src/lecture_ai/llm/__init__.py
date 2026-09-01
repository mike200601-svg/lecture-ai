"""Phase 2 LLM 接入层。"""

from lecture_ai.llm.base import LLMClient, LLMResponse
from lecture_ai.llm.fake import FakeLLMClient
from lecture_ai.llm.openai_client import OpenAILLMClient
from lecture_ai.llm.registry import build_llm_client

__all__ = [
    "LLMClient",
    "LLMResponse",
    "FakeLLMClient",
    "OpenAILLMClient",
    "build_llm_client",
]
