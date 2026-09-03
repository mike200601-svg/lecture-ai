"""Phase 2C：双输入硬门、证据路由、视觉未决队列与缓存。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lecture_ai.cli import build_parser
from lecture_ai.errors import LLMError
from lecture_ai.knowledge import KnowledgePipeline, validate_knowledge_response
from lecture_ai.llm import FakeLLMClient
from lecture_ai.structure import StructurePipeline
from tests.test_cleaning import _make_session
from tests.test_structure import _outline, _source, _write_cleaned


def _knowledge() -> dict:
    return {
        "concepts": [{
            "id": "concept_001",
            "topic_id": "topic_001",
            "name": "基本概念",
            "explanation": "老师介绍了基本概念。",
            "importance": 0.9,
            "source_segment_ids": [0, 1],
            "uncertain": [],
        }],
        "equations": [],
        "examples": [{
            "id": "example_001",
            "topic_id": "topic_002",
            "title": "课堂例题",
            "content": "老师讲解了课堂例题。",
            "source_segment_ids": [5, 6],
            "uncertain": [],
        }],
        "teacher_emphasis": [],
        "exam_tips": [],
        "common_errors": [],
        "open_questions": [],
        "visual_references": [],
        "uncertain_items": [],
    }


def _formal_inputs(config, db, *, source: list[dict] | None = None):
    meta, session_dir = _make_session(config, db, repaired=True)
    source = source or _source()
    _write_cleaned(session_dir, meta.session_id, source)
    config.llm.provider = "fake"
    StructurePipeline(
        config,
        db,
        client=FakeLLMClient(lambda _: _outline(source)),
        sleep=lambda _: None,
    ).run(meta.session_id)
    outline = json.loads(
        (session_dir / "analysis" / "outline.json").read_text(encoding="utf-8")
    )
    return meta, session_dir, source, outline


def test_knowledge_validator_accepts_traceable_result(config, db):
    _, _, source, outline = _formal_inputs(config, db)
    result = validate_knowledge_response(
        _knowledge(), source, outline, concept_threshold=0.8
    )
    assert result["concepts"][0]["importance"] == 0.9
    assert result["examples"][0]["source_segment_ids"] == [5, 6]


def test_knowledge_rejects_low_importance_unknown_source_and_lost_outline(config, db):
    _, _, source, outline = _formal_inputs(config, db)
    low = _knowledge()
    low["concepts"][0]["importance"] = 0.4
    with pytest.raises(LLMError, match="低于阈值"):
        validate_knowledge_response(low, source, outline, concept_threshold=0.8)

    unknown = _knowledge()
    unknown["concepts"][0]["source_segment_ids"] = [999]
    with pytest.raises(LLMError, match="未知 segment"):
        validate_knowledge_response(unknown, source, outline, concept_threshold=0.8)

    lost = _knowledge()
    lost["examples"] = []
    with pytest.raises(LLMError, match="examples.*丢失"):
        validate_knowledge_response(lost, source, outline, concept_threshold=0.8)


def test_knowledge_routes_uncertainty_and_visual_reference(config, db):
    source = _source()
    source[2]["uncertain"] = ["术语听不清"]
    source[2]["visual_references"] = ["板书/黑板"]
    _, _, source, outline = _formal_inputs(config, db, source=source)
    response = _knowledge()
    with pytest.raises(LLMError, match="uncertainty"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)

    response["uncertain_items"] = [{
        "id": "uncertain_001", "topic_id": "topic_001",
        "content": "相关术语听不清", "reason": "CLEANED 已标记不确定",
        "source_segment_ids": [2],
    }]
    with pytest.raises(LLMError, match="visual reference"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)

    response["visual_references"] = [{
        "id": "visual_001", "topic_id": "topic_001", "timestamp": 120.0,
        "context": "老师指向板书", "reference_type": "board", "confidence": 0.8,
        "source_segment_ids": [2], "uncertain": [],
    }]
    accepted = validate_knowledge_response(
        response, source, outline, concept_threshold=0.8
    )
    assert accepted["visual_references"][0]["timestamp"] == 120.0


def test_knowledge_rejects_fabricated_and_unrouted_incomplete_equation(config, db):
    source = _source()
    for item in source:
        item["text"] = "普通课堂内容"
    _, _, source, outline = _formal_inputs(config, db, source=source)
    response = _knowledge()
    response["equations"] = [{
        "id": "equation_001", "topic_id": "topic_001", "name": "凭空公式",
        "expression": "x=42", "status": "complete", "source_segment_ids": [2],
        "uncertain": [],
    }]
    with pytest.raises(LLMError, match="疑似编造"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)

    source[2]["text"] = "这个公式等于什么听不清。"
    response["equations"][0]["status"] = "incomplete"
    response["equations"][0]["uncertain"] = ["表达式不完整"]
    with pytest.raises(LLMError, match="不完整公式未进入"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)


def test_knowledge_requires_cleaned_and_outline(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    with pytest.raises(LLMError, match="缺少正式 CLEANED"):
        KnowledgePipeline(config, db).run(meta.session_id, dry_run=True)
    _write_cleaned(session_dir, meta.session_id)
    with pytest.raises(LLMError, match="缺少正式 STRUCTURED"):
        KnowledgePipeline(config, db).run(meta.session_id, dry_run=True)


def test_knowledge_pipeline_writes_both_outputs_and_reuses(config, db):
    meta, session_dir, _, _ = _formal_inputs(config, db)
    fake = FakeLLMClient(lambda _: _knowledge())
    pipeline = KnowledgePipeline(config, db, client=fake, sleep=lambda _: None)

    first = pipeline.run(meta.session_id)
    second = pipeline.run(meta.session_id)

    assert first.output_json and first.unresolved_visual_json
    assert second.reused and fake.calls == 1
    knowledge = json.loads(Path(first.output_json).read_text(encoding="utf-8"))
    unresolved = json.loads(
        Path(first.unresolved_visual_json).read_text(encoding="utf-8")
    )
    assert knowledge["layer"] == "KNOWLEDGE"
    assert knowledge["source"]["segment_count"] == 10
    assert unresolved["layer"] == "UNRESOLVED_VISUAL"
    assert unresolved["item_count"] == 0
    assert pipeline.sessions.load(meta.session_id).steps["knowledge"].status == "done"


def test_knowledge_web_prepares_and_rejects_invalid_response(config, db):
    meta, session_dir, _, _ = _formal_inputs(config, db)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    pipeline = KnowledgePipeline(config, db, sleep=lambda _: None)

    pending = pipeline.run(meta.session_id)
    exchange = session_dir / "analysis" / "knowledge_web" / "extract"
    assert pending.partial and (exchange / "prompt.md").exists()
    request = json.loads((exchange / "request.json").read_text(encoding="utf-8"))
    assert request["pipeline"] == "knowledge"
    (exchange / "response.json").write_text('{"concepts": []}', encoding="utf-8")

    retry = pipeline.run(meta.session_id)
    assert retry.partial
    assert len(list(exchange.glob("response.rejected.*.json"))) == 1
    assert pipeline.sessions.load(meta.session_id).steps["knowledge"].status == "pending"


def test_knowledge_cli_supports_dry_run_and_force():
    args = build_parser().parse_args(["knowledge", "session-1", "--dry-run", "--force"])
    assert args.command == "knowledge"
    assert args.session_id == "session-1" and args.dry_run and args.force
