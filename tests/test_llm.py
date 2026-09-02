"""LLM 抽象、OpenAI Responses provider 与隐私硬闸门。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lecture_ai.errors import ConfigError, LLMError, WebResponseRequired
from lecture_ai.llm import ChatGPTWebClient, OpenAILLMClient, build_llm_client


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


def test_chatgpt_web_client_prepares_exchange_and_imports_response(tmp_path):
    exchange = tmp_path / "chunk_002"
    client = ChatGPTWebClient("chatgpt-web-high")
    context = {"exchange_dir": exchange, "stage": "chunk", "index": 2}
    with pytest.raises(WebResponseRequired, match="GPT 网页任务已生成"):
        client.complete(
            "prompt text", json_schema={"type": "object"},
            request_context=context,
        )
    assert (exchange / "prompt.md").read_text(encoding="utf-8") == "prompt text"
    assert not (exchange / "response.json").exists()

    (exchange / "response.json").write_text(
        '```json\n{"segments": []}\n```', encoding="utf-8"
    )
    result = client.complete(
        "prompt text", json_schema={"type": "object"}, request_context=context
    )
    assert result.text == '{"segments": []}'
    assert result.provider == "chatgpt_web"
    assert result.usage["web_turns"] == 1


def test_chatgpt_web_registry_requires_transcript_privacy(config):
    config.llm.provider = "chatgpt_web"
    config.privacy.allow_cloud_transcript = False
    with pytest.raises(ConfigError, match="allow_cloud_transcript"):
        build_llm_client(config)


def test_chatgpt_web_registry_needs_no_api_key(config, monkeypatch):
    config.llm.provider = "chatgpt_web"
    config.privacy.allow_cloud_transcript = True
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert isinstance(build_llm_client(config), ChatGPTWebClient)


def test_chatgpt_web_rejects_response_for_stale_prompt(tmp_path):
    exchange = tmp_path / "chunk"
    client = ChatGPTWebClient()
    context = {"exchange_dir": exchange, "stage": "chunk", "index": 0}
    with pytest.raises(WebResponseRequired):
        client.complete("old prompt", request_context=context)
    (exchange / "response.json").write_text('{"segments": []}', encoding="utf-8")
    with pytest.raises(LLMError, match="旧 prompt"):
        client.complete("new prompt", request_context=context)
