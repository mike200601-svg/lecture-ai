"""Phase 2D：双层正式输入、知识全覆盖、未决 callout 与确定性 Markdown。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lecture_ai.audio_draft import AudioDraftPipeline, validate_draft_response
from lecture_ai.audio_draft.renderer import render_audio_draft
from lecture_ai.cli import build_parser
from lecture_ai.errors import LLMError
from lecture_ai.knowledge import KnowledgePipeline
from lecture_ai.llm import FakeLLMClient
from tests.test_cleaning import _make_session
from tests.test_knowledge import _formal_inputs, _knowledge
from tests.test_structure import _source, _write_cleaned


def _draft(outline: dict, knowledge: dict) -> dict:
    categories = {
        "concept_ids": "concepts",
        "equation_ids": "equations",
        "example_ids": "examples",
        "teacher_emphasis_ids": "teacher_emphasis",
        "exam_tip_ids": "exam_tips",
        "common_error_ids": "common_errors",
        "open_question_ids": "open_questions",
        "uncertain_item_ids": "uncertain_items",
        "visual_reference_ids": "visual_references",
    }
    sections = []
    for index, topic in enumerate(outline["lecture_topics"], start=1):
        item_ids = {
            field: [
                item["id"] for item in knowledge[category]
                if item["topic_id"] == topic["id"]
            ]
            for field, category in categories.items()
        }
        sources = {
            value
            for field, category in categories.items()
            for item in knowledge[category]
            if item["id"] in item_ids[field]
            for value in item["source_segment_ids"]
        }
        sections.append({
            "id": f"section_{index:03d}",
            "topic_id": topic["id"],
            "heading": topic["title"],
            "summary": f"本节围绕{topic['title']}展开。",
            "source_segment_ids": sorted(sources) or [topic["source_segment_ids"][0]],
            **item_ids,
        })
    return {
        "title": "量子力学课堂记录",
        "sections": sections,
        "closing_summary": [{
            "content": "本节介绍基本概念并讲解课堂例题。",
            "source_segment_ids": [0, 5],
        }],
    }


def _formal_draft_inputs(config, db, *, source=None, knowledge_response=None):
    meta, session_dir, source, outline = _formal_inputs(
        config, db, source=source
    )
    response = knowledge_response or _knowledge()
    KnowledgePipeline(
        config,
        db,
        client=FakeLLMClient(lambda _: response),
        sleep=lambda _: None,
    ).run(meta.session_id)
    knowledge = json.loads(
        (session_dir / "analysis" / "knowledge.json").read_text(encoding="utf-8")
    )
    unresolved = json.loads(
        (session_dir / "analysis" / "unresolved_visual.json").read_text(encoding="utf-8")
    )
    return meta, session_dir, outline, knowledge, unresolved


def test_draft_validator_accepts_exact_topic_and_item_coverage(config, db):
    _, _, outline, knowledge, _ = _formal_draft_inputs(config, db)
    result = validate_draft_response(_draft(outline, knowledge), outline, knowledge)
    assert [item["topic_id"] for item in result["sections"]] == [
        "topic_001", "topic_002"
    ]
    assert result["sections"][0]["concept_ids"] == ["concept_001"]
    assert result["sections"][1]["example_ids"] == ["example_001"]


def test_draft_validator_rejects_topic_loss_cross_topic_and_wikilink(config, db):
    _, _, outline, knowledge, _ = _formal_draft_inputs(config, db)
    missing_topic = _draft(outline, knowledge)
    missing_topic["sections"].pop()
    with pytest.raises(LLMError, match="一一覆盖"):
        validate_draft_response(missing_topic, outline, knowledge)

    cross_topic = _draft(outline, knowledge)
    cross_topic["sections"][0]["example_ids"] = ["example_001"]
    cross_topic["sections"][1]["example_ids"] = []
    with pytest.raises(LLMError, match="其他 topic"):
        validate_draft_response(cross_topic, outline, knowledge)

    wikilink = _draft(outline, knowledge)
    wikilink["sections"][0]["summary"] = "参见 [[凭空概念]]"
    with pytest.raises(LLMError, match="WikiLink"):
        validate_draft_response(wikilink, outline, knowledge)


def test_draft_validator_rejects_item_loss_and_incomplete_provenance(config, db):
    _, _, outline, knowledge, _ = _formal_draft_inputs(config, db)
    lost = _draft(outline, knowledge)
    lost["sections"][0]["concept_ids"] = []
    with pytest.raises(LLMError, match="丢失 concept_ids"):
        validate_draft_response(lost, outline, knowledge)

    incomplete = _draft(outline, knowledge)
    incomplete["sections"][0]["source_segment_ids"] = [0]
    with pytest.raises(LLMError, match="provenance"):
        validate_draft_response(incomplete, outline, knowledge)


def test_renderer_marks_audio_only_uncertainty_and_visual_as_questions(config, db):
    source = _source()
    source[2]["uncertain"] = ["术语听不清"]
    source[2]["visual_references"] = ["板书/黑板"]
    source[3]["text"] = "这个公式等于一乘二的三次方，后面听不清。"
    response = _knowledge()
    response["equations"] = [{
        "id": "equation_001", "topic_id": "topic_001", "name": "课堂公式",
        "expression": "1\\times2^3+\\cdots", "status": "incomplete",
        "source_segment_ids": [3], "uncertain": ["后半部分听不清"],
    }]
    response["uncertain_items"] = [{
        "id": "uncertain_001", "topic_id": "topic_001",
        "content": "相关术语与公式后半部分听不清", "reason": "CLEANED 与公式已标记不确定",
        "source_segment_ids": [2, 3],
    }]
    response["visual_references"] = [{
        "id": "visual_001", "topic_id": "topic_001", "timestamp": 120.0,
        "context": "老师指向板书", "reference_type": "board", "confidence": 0.8,
        "source_segment_ids": [2], "uncertain": [],
    }]
    meta, _, outline, knowledge, unresolved = _formal_draft_inputs(
        config, db, source=source, knowledge_response=response
    )
    draft = validate_draft_response(_draft(outline, knowledge), outline, knowledge)
    markdown = render_audio_draft(
        session_id=meta.session_id,
        course=meta.course.name,
        date=meta.date,
        draft=draft,
        outline=outline,
        knowledge=knowledge,
        unresolved_visual=unresolved,
    )
    assert "Audio-only 草稿" in markdown
    assert "[!question] 音频内容待核验" in markdown
    assert "[!question] 公式尚未核验" in markdown
    assert "[!question] 待板书 / 课件补充" in markdown
    assert "final: false" in markdown
    assert "[[" not in markdown


def test_audio_draft_requires_all_formal_upstreams(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    with pytest.raises(LLMError, match="缺少正式 CLEANED"):
        AudioDraftPipeline(config, db).run(meta.session_id, dry_run=True)
    _write_cleaned(session_dir, meta.session_id)
    with pytest.raises(LLMError, match="缺少正式 STRUCTURED"):
        AudioDraftPipeline(config, db).run(meta.session_id, dry_run=True)


def test_audio_draft_rejects_visual_queue_marked_resolved(config, db):
    source = _source()
    source[2]["visual_references"] = ["板书/黑板"]
    response = _knowledge()
    response["visual_references"] = [{
        "id": "visual_001", "topic_id": "topic_001", "timestamp": 120.0,
        "context": "老师指向板书", "reference_type": "board", "confidence": 0.8,
        "source_segment_ids": [2], "uncertain": [],
    }]
    meta, session_dir, _, _, _ = _formal_draft_inputs(
        config, db, source=source, knowledge_response=response
    )
    path = session_dir / "analysis" / "unresolved_visual.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"][0]["status"] = "resolved"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LLMError, match="错误标记为已解决"):
        AudioDraftPipeline(config, db).run(meta.session_id, dry_run=True)


def test_audio_draft_pipeline_writes_json_markdown_and_reuses(config, db):
    meta, session_dir, outline, knowledge, _ = _formal_draft_inputs(config, db)
    fake = FakeLLMClient(lambda _: _draft(outline, knowledge))
    pipeline = AudioDraftPipeline(config, db, client=fake, sleep=lambda _: None)

    first = pipeline.run(meta.session_id)
    second = pipeline.run(meta.session_id)

    assert first.output_json and first.output_md
    assert second.reused and fake.calls == 1
    payload = json.loads(Path(first.output_json).read_text(encoding="utf-8"))
    markdown = Path(first.output_md).read_text(encoding="utf-8")
    assert payload["layer"] == "AUDIO_DRAFT"
    assert payload["generation"]["audio_only"] is True
    assert payload["generation"]["final"] is False
    assert "[!warning] Audio-only 草稿" in markdown
    assert (session_dir / "note" / "lecture_audio_draft.md").exists()
    assert pipeline.sessions.load(meta.session_id).steps["note"].status == "done"


def test_audio_draft_web_prepares_and_rejects_invalid_response(config, db):
    meta, session_dir, _, _, _ = _formal_draft_inputs(config, db)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    pipeline = AudioDraftPipeline(config, db, sleep=lambda _: None)

    pending = pipeline.run(meta.session_id)
    exchange = session_dir / "analysis" / "audio_draft_web" / "draft"
    assert pending.partial and (exchange / "prompt.md").exists()
    request = json.loads((exchange / "request.json").read_text(encoding="utf-8"))
    assert request["pipeline"] == "audio_draft"
    (exchange / "response.json").write_text('{"title":"残缺"}', encoding="utf-8")

    retry = pipeline.run(meta.session_id)
    assert retry.partial
    assert len(list(exchange.glob("response.rejected.*.json"))) == 1
    assert pipeline.sessions.load(meta.session_id).steps["note"].status == "pending"


def test_audio_draft_cli_supports_dry_run_and_force():
    args = build_parser().parse_args(["draft", "session-1", "--dry-run", "--force"])
    assert args.command == "draft"
    assert args.session_id == "session-1" and args.dry_run and args.force
