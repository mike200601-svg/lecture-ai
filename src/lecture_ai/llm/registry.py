"""LLM 工厂与云端文本隐私硬闸门。"""

from __future__ import annotations

from lecture_ai.config import Config
from lecture_ai.errors import ConfigError
from lecture_ai.llm.base import LLMClient


def build_llm_client(config: Config) -> LLMClient:
    provider = (config.llm.provider or "chatgpt_web").strip().lower()
    if provider == "fake":
        from lecture_ai.llm.fake import FakeLLMClient

        return FakeLLMClient()
    if provider not in {"chatgpt_web", "openai"}:
        raise ConfigError(
            f"未知的 llm.provider：{provider!r}。当前可选：chatgpt_web, openai, fake"
        )
    if not config.privacy.allow_cloud_transcript:
        raise ConfigError(
            f"llm.provider={provider} 会把转录文本发送到云端，但 "
            "privacy.allow_cloud_transcript 为 false"
        )
    if provider == "chatgpt_web":
        from lecture_ai.llm.web_client import ChatGPTWebClient

        return ChatGPTWebClient(config.llm.model)
    from lecture_ai.llm.openai_client import OpenAILLMClient

    return OpenAILLMClient(config.llm.model)
