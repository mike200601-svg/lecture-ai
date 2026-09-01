"""LLM 工厂与云端文本隐私硬闸门。"""

from __future__ import annotations

from lecture_ai.config import Config
from lecture_ai.errors import ConfigError
from lecture_ai.llm.base import LLMClient


def build_llm_client(config: Config) -> LLMClient:
    provider = (config.llm.provider or "openai").strip().lower()
    if provider == "fake":
        from lecture_ai.llm.fake import FakeLLMClient

        return FakeLLMClient()
    if provider != "openai":
        raise ConfigError(f"未知的 llm.provider：{provider!r}。当前可选：openai, fake")
    if not config.privacy.allow_cloud_transcript:
        raise ConfigError(
            "llm.provider=openai 会把转录文本发送到云端，但 "
            "privacy.allow_cloud_transcript 为 false"
        )
    from lecture_ai.llm.openai_client import OpenAILLMClient

    return OpenAILLMClient(config.llm.model)
