"""LLM 抽象、OpenAI Responses provider 与隐私硬闸门。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lecture_ai.errors import ConfigError, LLMError
from lecture_ai.llm import OpenAILLMClient, build_llm_client


class StubResponses:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            id="resp_test",
            model="gpt-test",
            output_text='{"segments": []}',
            usage=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
        )


def test_openai_client_uses_responses_structured_output():
    responses = StubResponses()
    client = OpenAILLMClient(
        "gpt-test", api_key="test-key", client=SimpleNamespace(responses=responses)
    )
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    result = client.complete(
        "clean this",
        system="rules",
        json_schema=schema,
        max_tokens=123,
        temperature=0.1,
    )

    assert result.text == '{"segments": []}'
    assert result.usage["total_tokens"] == 14
    assert responses.kwargs["input"] == "clean this"
    assert responses.kwargs["instructions"] == "rules"
    assert responses.kwargs["max_output_tokens"] == 123
    assert responses.kwargs["store"] is False
    assert responses.kwargs["text"]["format"]["type"] == "json_schema"
    assert responses.kwargs["text"]["format"]["strict"] is True


def test_llm_registry_enforces_cloud_text_privacy(config):
    config.llm.provider = "openai"
    config.privacy.allow_cloud_transcript = False
    with pytest.raises(ConfigError, match="allow_cloud_transcript"):
        build_llm_client(config)


def test_llm_registry_reports_waiting_without_key(config, monkeypatch):
    config.llm.provider = "openai"
    config.privacy.allow_cloud_transcript = True
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="WAITING FOR REAL LLM PROVIDER"):
        build_llm_client(config)


def test_unknown_llm_provider_rejected(config):
    config.llm.provider = "crystal_ball"
    with pytest.raises(ConfigError, match="未知"):
        build_llm_client(config)
