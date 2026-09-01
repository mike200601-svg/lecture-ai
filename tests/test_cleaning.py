"""Phase 2A 分块清洗、边界协调、缓存、重试与 provenance。"""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from lecture_ai.cleaning import (
    CleanPipeline,
    build_chunk_plan,
    validate_clean_response,
)
from lecture_ai.cleaning.prompting import load_clean_prompt, render_clean_prompt
from lecture_ai.cli import build_parser
from lecture_ai.errors import LLMError
from lecture_ai.llm import FakeLLMClient
from lecture_ai.session import SessionManager, SessionState, load_courses
from lecture_ai.transcription import TranscriptResult, TranscriptSegment, write_transcript
from lecture_ai.utils.hashing import sha256_file

_INPUT = re.compile(r"<input_json>\s*(.*?)\s*</input_json>", re.DOTALL)


def _faithful_responder(prompt: str):
    items = json.loads(_INPUT.search(prompt).group(1))
    return {
        "segments": [
            {
                "id": item["id"],
                "text": str(item.get("text") or item.get("left_text") or "").replace(
                    "波涵数", "波函数"
                ),
                "uncertain": list(item.get("uncertain") or []),
                "visual_references": list(item.get("visual_references") or []),
            }
            for item in items
        ]
    }


def _make_session(config, db, *, repaired: bool = True):
    manager = SessionManager(config, db)
    course = load_courses(config.courses_path).get("quantum_mechanics")
    meta = manager.create(
        course, datetime.fromisoformat("2026-09-02T14:00:00+08:00")
    )
    meta.audio.duration_sec = 600.0
    manager.save(meta)
    manager.transition(meta, SessionState.AUDIO_READY)
    manager.transition(meta, SessionState.TRANSCRIBING)
    manager.transition(meta, SessionState.TRANSCRIBED)
    session_dir = manager.session_dir(meta.session_id)

    result = TranscriptResult(
        segments=[
            TranscriptSegment(i * 60, (i + 1) * 60, f"第{i}段讲波涵数和例题。")
            for i in range(10)
        ],
        language="zh",
        duration_sec=600,
        provider="fake-asr",
        model="fake-asr-v1",
    )
    raw_files = write_transcript(
        result,
        session_dir / "transcript",
        session_id=meta.session_id,
        course_name=meta.course.name,
        date=meta.date,
        audio_start_iso=meta.start_time,
    )
    if repaired:
        raw = json.loads(raw_files.json_path.read_text(encoding="utf-8"))
        raw["layer"] = "REPAIRED"
        repaired_path = session_dir / "transcript" / "transcript_repaired.json"
        repaired_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return manager.load(meta.session_id), session_dir


def test_chunk_plan_has_overlap_and_stable_ids():
    segments = [
        {"id": i, "start": i * 60, "end": (i + 1) * 60, "text": str(i)}
        for i in range(10)
    ]
    plans = build_chunk_plan(
        segments, duration_sec=600, chunk_minutes=8, overlap_seconds=30
    )
    assert len(plans) == 2
    assert plans[0].core_end == 480
    assert plans[0].window_end == 510
    assert plans[1].window_start == 450
    assert set(plans[0].segment_ids) & set(plans[1].segment_ids)


def test_clean_response_rejects_topology_and_summary(config):
    expected = [
        {"id": 1, "text": "老师详细讲解了一个很长很长的推导过程"},
        {"id": 2, "text": "这里还有例题和考试提示"},
    ]
    missing = {"segments": [{
        "id": 1, "text": "内容", "uncertain": [], "visual_references": []
    }]}
    with pytest.raises(LLMError, match="拓扑"):
        validate_clean_response(missing, expected, config.clean)

    summary = {"segments": [
        {"id": 1, "text": "推导", "uncertain": [], "visual_references": []},
        {"id": 2, "text": "例题", "uncertain": [], "visual_references": []},
    ]}
    with pytest.raises(LLMError, match="摘要"):
        validate_clean_response(summary, expected, config.clean)

    empty_without_audit = {"segments": [
        {"id": 1, "text": "", "uncertain": [], "visual_references": []},
        {"id": 2, "text": "这里还有例题和考试提示", "uncertain": [], "visual_references": []},
    ]}
    with pytest.raises(LLMError, match="uncertain"):
        validate_clean_response(empty_without_audit, expected, config.clean)


def test_prompt_is_external_and_renders_all_variables(config):
    path, template = load_clean_prompt(config.paths.project_root)
    rendered = render_clean_prompt(
        template,
        mode="clean_chunk",
        course_name="量子力学",
        glossary=["波函数"],
        segments=[{"id": 1, "text": "波涵数"}],
    )
    assert path.name == "transcript_clean.md"
    assert "波函数" in rendered
    assert "{{" not in rendered


def test_clean_cli_supports_dry_run_chunk_and_force():
    args = build_parser().parse_args([
        "clean", "session-1", "--dry-run", "--chunk", "2", "--force"
    ])
    assert args.command == "clean"
    assert args.session_id == "session-1"
    assert args.dry_run and args.chunk == 2 and args.force


def test_clean_pipeline_two_stage_cache_and_provenance(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    config.llm.provider = "fake"
    config.llm.model = "fake-clean-v1"
    fake = FakeLLMClient(_faithful_responder)
    pipeline = CleanPipeline(config, db, client=fake, sleep=lambda _: None)

    first = pipeline.run(meta.session_id)
    output_path = session_dir / "analysis" / "transcript_clean.json"
    mtime = output_path.stat().st_mtime_ns
    second = pipeline.run(meta.session_id)

    assert first.source_layer == "REPAIRED"
    assert first.chunks_processed == 2
    assert first.boundaries_processed == 1
    assert fake.calls == 3
    assert second.reused and output_path.stat().st_mtime_ns == mtime

    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["layer"] == "CLEANED"
    assert output["segment_count"] == 10
    assert output["usage"]["api_calls"] == 3
    assert all("波函数" in item["text"] for item in output["segments"])
    assert [item["start"] for item in output["segments"]] == [i * 60 for i in range(10)]
    assert any(item["provenance"]["boundary_reconciled"] for item in output["segments"])
    assert all(item["provenance"]["source_layer"] == "REPAIRED" for item in output["segments"])
    final = pipeline.sessions.load(meta.session_id)
    assert final.state is SessionState.TRANSCRIBED
    assert final.steps["clean"].status == "done"


def test_clean_falls_back_to_raw_and_dry_run_needs_no_client(config, db):
    meta, session_dir = _make_session(config, db, repaired=False)
    raw = session_dir / "transcript" / "transcript_raw.json"
    before = sha256_file(raw)
    outcome = CleanPipeline(config, db).run(meta.session_id, dry_run=True)
    assert outcome.source_layer == "RAW"
    assert outcome.chunks_planned == 2
    assert before == sha256_file(raw)
    assert not (session_dir / "analysis" / "transcript_clean.json").exists()


def test_single_chunk_mode_only_writes_cache(config, db):
    meta, session_dir = _make_session(config, db)
    config.llm.provider = "fake"
    fake = FakeLLMClient(_faithful_responder)
    outcome = CleanPipeline(config, db, client=fake, sleep=lambda _: None).run(
        meta.session_id, chunk=0
    )
    assert outcome.partial and outcome.chunks_processed == 1
    assert fake.calls == 1
    assert (session_dir / "analysis" / "clean_cache" / "chunk_000.json").exists()
    assert not (session_dir / "analysis" / "transcript_clean.json").exists()


def test_clean_retries_transient_failure(config, db):
    meta, _ = _make_session(config, db)
    config.llm.provider = "fake"
    fake = FakeLLMClient(_faithful_responder, fail_times=1)
    outcome = CleanPipeline(config, db, client=fake, sleep=lambda _: None).run(
        meta.session_id, chunk=0
    )
    assert outcome.partial
    assert fake.calls == 2
    assert outcome.chunks[0]["retries"] == 1


def test_retry_reuses_successful_chunks_and_only_reruns_failed_chunk(config, db):
    meta, session_dir = _make_session(config, db)
    config.llm.provider = "fake"
    config.llm.model = "fake-clean-v1"
    config.clean.max_retries = 0

    def fail_on_second_chunk(prompt):
        if '"id": 9' in prompt:
            raise RuntimeError("second chunk unavailable")
        return _faithful_responder(prompt)

    first_client = FakeLLMClient(fail_on_second_chunk)
    pipeline = CleanPipeline(config, db, client=first_client, sleep=lambda _: None)
    with pytest.raises(LLMError, match="second chunk unavailable"):
        pipeline.run(meta.session_id)
    first_cache = session_dir / "analysis" / "clean_cache" / "chunk_000.json"
    assert first_cache.exists()
    first_mtime = first_cache.stat().st_mtime_ns

    recovery_client = FakeLLMClient(_faithful_responder)
    recovered = CleanPipeline(
        config, db, client=recovery_client, sleep=lambda _: None
    ).run(meta.session_id)
    assert recovered.chunks_processed == 2 and recovered.boundaries_processed == 1
    assert recovery_client.calls == 2  # chunk 1 + boundary；chunk 0 直接复用
    assert first_cache.stat().st_mtime_ns == first_mtime


def test_clean_invalid_json_fails_without_final_artifact(config, db):
    meta, session_dir = _make_session(config, db)
    config.llm.provider = "fake"
    config.clean.max_retries = 0
    fake = FakeLLMClient(lambda _: "not json")
    with pytest.raises(LLMError, match="1 次尝试后失败"):
        CleanPipeline(config, db, client=fake, sleep=lambda _: None).run(meta.session_id)
    assert not (session_dir / "analysis" / "transcript_clean.json").exists()
    assert CleanPipeline(config, db).sessions.load(meta.session_id).steps["clean"].status == "failed"
