"""Phase 2B：CLEANED 硬输入、结构 schema、来源覆盖、缓存与网页交换。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lecture_ai.cli import build_parser
from lecture_ai.errors import LLMError
from lecture_ai.llm import FakeLLMClient
from lecture_ai.structure import StructurePipeline, validate_outline_response
from tests.test_cleaning import _make_session


def _source() -> list[dict]:
    return [
        {
            "id": index,
            "start": index * 60.0,
            "end": (index + 1) * 60.0,
            "text": f"第{index}段课堂内容",
            "uncertain": [],
            "visual_references": [],
        }
        for index in range(10)
    ]


def _outline(source: list[dict] | None = None) -> dict:
    source = source or _source()
    ids = [item["id"] for item in source]
    split = len(ids) // 2
    left, right = ids[:split], ids[split:]
    topics = [{
        "id": "topic_001",
        "title": "课程引入",
        "type": "content",
        "start": source[0]["start"],
        "end": source[split]["start"],
        "source_segment_ids": left,
        "uncertain": [],
    }, {
        "id": "topic_002",
        "title": "例题讲解",
        "type": "example",
        "start": source[split]["start"],
        "end": source[-1]["end"],
        "source_segment_ids": right,
        "uncertain": [],
    }]
    return {
        "lecture_topics": topics,
        "subtopics": [{
            "id": "subtopic_001", "topic_id": "topic_001", "title": "基本概念",
            "start": source[1]["start"], "end": source[2]["end"],
            "source_segment_ids": [ids[1], ids[2]], "uncertain": [],
        }],
        "definitions": [],
        "derivations": [],
        "examples": [{
            "id": "example_001", "topic_id": "topic_002", "label": "课堂例题",
            "start": source[split]["start"], "end": source[split + 1]["end"],
            "source_segment_ids": [ids[split], ids[split + 1]], "uncertain": [],
        }],
        "teacher_emphasis": [],
        "exam_tips": [],
        "transitions": [{
            "id": "transition_001", "from_topic_id": "topic_001",
            "to_topic_id": "topic_002", "cue": "下面来看例题",
            "start": source[split - 1]["start"], "end": source[split]["end"],
            "source_segment_ids": [ids[split - 1], ids[split]], "uncertain": [],
        }],
    }


def _write_cleaned(session_dir: Path, session_id: str, source: list[dict] | None = None) -> Path:
    source = source or _source()
    formal_segments = [
        {
            **item,
            "provenance": {
                "source_layer": "REPAIRED",
                "source_segment_id": item["id"],
                "source_sha256": "a" * 64,
                "primary_chunk_id": 0,
            },
        }
        for item in source
    ]
    path = session_dir / "analysis" / "transcript_clean.json"
    path.write_text(
        json.dumps({
            "schema_version": 2,
            "layer": "CLEANED",
            "session_id": session_id,
            "course": "量子力学",
            "date": "2026-09-02",
            "source": {"layer": "REPAIRED", "sha256": "a" * 64},
            "clean": {"fingerprint": "b" * 64},
            "segments": formal_segments,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_outline_validator_accepts_complete_traceable_structure():
    result = validate_outline_response(_outline(), _source())

    assert len(result["lecture_topics"]) == 2
    assert [
        segment_id for topic in result["lecture_topics"]
        for segment_id in topic["source_segment_ids"]
    ] == list(range(10))


@pytest.mark.parametrize("failure", ["missing", "unknown", "wrong_time", "overlap"])
def test_outline_validator_rejects_broken_topology(failure):
    result = _outline()
    if failure == "missing":
        result["lecture_topics"][0]["source_segment_ids"].remove(2)
    elif failure == "unknown":
        result["lecture_topics"][1]["source_segment_ids"][-1] = 999
    elif failure == "wrong_time":
        result["lecture_topics"][0]["end"] = 123.0
    else:
        result["lecture_topics"][1]["source_segment_ids"].insert(0, 4)

    with pytest.raises(LLMError):
        validate_outline_response(result, _source())


def test_outline_validator_rejects_unknown_topic_and_lost_derivation():
    source = _source()
    wrong_topic = _outline(source)
    wrong_topic["examples"][0]["topic_id"] = "topic_404"
    with pytest.raises(LLMError, match="未知 topic"):
        validate_outline_response(wrong_topic, source)

    source[2]["text"] = "下面我们来推导这个表达式。"
    with pytest.raises(LLMError, match="derivations 未覆盖"):
        validate_outline_response(_outline(source), source)


def test_structure_requires_formal_cleaned(config, db):
    meta, _ = _make_session(config, db, repaired=True)
    with pytest.raises(LLMError, match="只接受正式 CLEANED"):
        StructurePipeline(config, db).run(meta.session_id, dry_run=True)


def test_structure_rejects_cleaned_without_segment_provenance(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    clean_path = _write_cleaned(session_dir, meta.session_id)
    payload = json.loads(clean_path.read_text(encoding="utf-8"))
    payload["segments"][0].pop("provenance")
    clean_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LLMError, match="provenance"):
        StructurePipeline(config, db).run(meta.session_id, dry_run=True)


def test_structure_pipeline_writes_provenance_and_reuses(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    source = _source()
    _write_cleaned(session_dir, meta.session_id, source)
    config.llm.provider = "fake"
    fake = FakeLLMClient(lambda _: _outline(source))
    pipeline = StructurePipeline(config, db, client=fake, sleep=lambda _: None)

    first = pipeline.run(meta.session_id)
    second = pipeline.run(meta.session_id)

    assert first.output_json and second.reused
    assert fake.calls == 1
    payload = json.loads(Path(first.output_json).read_text(encoding="utf-8"))
    assert payload["layer"] == "STRUCTURED"
    assert payload["source"]["layer"] == "CLEANED"
    assert payload["source"]["segment_count"] == 10
    assert payload["lecture_topics"][0]["source_segment_ids"] == list(range(5))
    assert pipeline.sessions.load(meta.session_id).steps["structure"].status == "done"


def test_structure_source_change_invalidates_cache(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    source = _source()
    clean_path = _write_cleaned(session_dir, meta.session_id, source)
    config.llm.provider = "fake"
    fake = FakeLLMClient(lambda prompt: _outline(source))
    pipeline = StructurePipeline(config, db, client=fake, sleep=lambda _: None)
    pipeline.run(meta.session_id)

    payload = json.loads(clean_path.read_text(encoding="utf-8"))
    payload["segments"][0]["text"] += " 更新"
    clean_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    pipeline.run(meta.session_id)

    assert fake.calls == 2


def test_structure_web_prepares_task_and_rejects_invalid_response(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    _write_cleaned(session_dir, meta.session_id)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    pipeline = StructurePipeline(config, db, sleep=lambda _: None)

    pending = pipeline.run(meta.session_id)
    exchange = session_dir / "analysis" / "structure_web" / "outline"
    assert pending.partial and (exchange / "prompt.md").exists()
    request = json.loads((exchange / "request.json").read_text(encoding="utf-8"))
    assert request["pipeline"] == "structure"
    assert request["source_layer"] == "CLEANED"
    (exchange / "response.json").write_text('{"lecture_topics": []}', encoding="utf-8")

    retry = pipeline.run(meta.session_id)

    assert retry.partial
    assert len(list(exchange.glob("response.rejected.*.json"))) == 1
    assert (exchange / "retry.md").exists()
    assert pipeline.sessions.load(meta.session_id).steps["structure"].status == "pending"


def test_structure_cli_supports_dry_run_and_force():
    args = build_parser().parse_args(["structure", "session-1", "--dry-run", "--force"])
    assert args.command == "structure"
    assert args.session_id == "session-1"
    assert args.dry_run and args.force
