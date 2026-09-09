"""GPT Web Session Export Package 的安全边界与可复现性。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from lecture_ai.errors import ExportPackageError
from lecture_ai.export_package import ExportPackageBuilder
from lecture_ai.session import SessionManager, load_courses

START = datetime(2026, 9, 3, 14, 0)


def _create_session(config, db, *, repaired: bool = True):
    manager = SessionManager(config, db)
    course = load_courses(config.courses_path).get("quantum_mechanics")
    meta = manager.create(course, START)
    if repaired:
        repaired_path = manager.session_dir(meta.session_id) / "transcript" / "transcript_repaired.md"
        repaired_path.write_text("# 修复后转录\n\n完整课堂内容。\n", encoding="utf-8")
    return manager, meta


def _prompt_of(outcome):
    """从产出目录里取回 NOTE_PROMPT 的内容。"""
    hits = list(outcome.output_dir.glob("*_NOTE_PROMPT.md"))
    assert len(hits) == 1, f"应当只有一个 NOTE_PROMPT，实际 {hits}"
    return hits[0].read_text(encoding="utf-8")


def test_repaired_is_required_without_raw_fallback(config, db):
    manager, meta = _create_session(config, db, repaired=False)
    raw = manager.session_dir(meta.session_id) / "transcript" / "transcript_raw.md"
    raw.write_text("只有 RAW", encoding="utf-8")

    with pytest.raises(ExportPackageError, match="禁止回退到 RAW"):
        ExportPackageBuilder(config, db).build(meta.session_id)

    assert raw.read_text(encoding="utf-8") == "只有 RAW"


def test_export_directory_layout_and_names_include_identity(config, db):
    manager, meta = _create_session(config, db)
    session_dir = manager.session_dir(meta.session_id)
    (session_dir / "images" / "board.jpg").write_bytes(b"board")
    (session_dir / "slides" / "lecture.pdf").write_bytes(b"slides")

    outcome = ExportPackageBuilder(config, db).build(meta.session_id)
    prefix = "2026-09-03_1400_量子力学_001"

    assert outcome.output_dir.name == prefix
    assert (outcome.output_dir / f"{prefix}_01_transcript_repaired.md").is_file()
    assert (outcome.output_dir / "02_board").is_dir()
    assert (outcome.output_dir / "03_slides").is_dir()
    assert (outcome.output_dir / f"{prefix}_session_info.md").is_file()
    assert (outcome.output_dir / f"{prefix}_NOTE_PROMPT.md").is_file()
    assert (outcome.output_dir / f"{prefix}_manifest.json").is_file()
    assert all(prefix in path.name for path in (outcome.output_dir / "02_board").iterdir())
    assert all(prefix in path.name for path in (outcome.output_dir / "03_slides").iterdir())


def test_export_preserves_course_name_case(config, db):
    manager, meta = _create_session(config, db)
    meta.course.name = "计算物理B"
    manager.save(meta)

    outcome = ExportPackageBuilder(config, db).build(meta.session_id)

    assert outcome.output_dir.name == "2026-09-03_1400_计算物理B_001"


def test_manifest_records_sources_hashes_and_suggested_note_name(config, db):
    manager, meta = _create_session(config, db)
    board = manager.session_dir(meta.session_id) / "images" / "b.png"
    board.write_bytes(b"photo")

    outcome = ExportPackageBuilder(config, db).build(meta.session_id)
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))

    assert manifest["session_id"] == meta.session_id
    assert manifest["course"] == "量子力学"
    assert manifest["start_time"].startswith("2026-09-03T14:00:00")
    assert manifest["transcript"]["source_path"].endswith("transcript_repaired.md")
    assert len(manifest["transcript"]["sha256"]) == 64
    assert manifest["board_files"][0]["source_path"].endswith("images/b.png")
    assert manifest["slide_files"] == []
    assert manifest["suggested_final_note"].endswith("_final_note.md")
    assert manifest["created_at"]


def test_materials_are_copied_not_moved(config, db):
    _, meta = _create_session(config, db)
    board = config.paths.incoming_images / "明确归属.png"
    board.write_bytes(b"keep-board")
    slides = config.paths.project_root / "本节课件.pptx"
    slides.write_bytes(b"keep-slides")

    outcome = ExportPackageBuilder(config, db).build(
        meta.session_id, board_paths=[board], slide_paths=[slides]
    )

    assert board.read_bytes() == b"keep-board"
    assert slides.read_bytes() == b"keep-slides"
    assert len(list((outcome.output_dir / "02_board").iterdir())) == 1
    assert len(list((outcome.output_dir / "03_slides").iterdir())) == 1


def test_rerun_is_idempotent_without_duplicate_copies(config, db):
    manager, meta = _create_session(config, db)
    (manager.session_dir(meta.session_id) / "images" / "b.jpg").write_bytes(b"one")
    builder = ExportPackageBuilder(config, db)

    first = builder.build(meta.session_id)
    first_names = sorted(path.relative_to(first.output_dir) for path in first.output_dir.rglob("*"))
    second = builder.build(meta.session_id)
    second_names = sorted(path.relative_to(second.output_dir) for path in second.output_dir.rglob("*"))

    assert first.output_dir == second.output_dir
    assert first_names == second_names
    assert len(list((second.output_dir / "02_board").glob("*.jpg"))) == 1


def test_unassigned_incoming_board_is_warned_but_not_guessed(config, db):
    _, meta = _create_session(config, db)
    candidate = config.paths.incoming_images / "不知道哪节课.jpg"
    candidate.write_bytes(b"unassigned")

    outcome = ExportPackageBuilder(config, db).build(meta.session_id)
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))

    assert outcome.unassigned_count == 1
    assert manifest["unassigned"][0]["source_path"].endswith("不知道哪节课.jpg")
    assert "未明确关联" in manifest["unassigned"][0]["reason"]
    assert list((outcome.output_dir / "02_board").iterdir()) == []
    assert any("未打包" in warning for warning in manifest["warnings"])


def test_old_session_directory_name_still_exports(config, db):
    manager, meta = _create_session(config, db, repaired=False)
    old_id = "2026-09-03_quantum-mechanics_001"
    assert manager.relabel(meta.session_id, old_id) == old_id
    repaired = manager.session_dir(old_id) / "transcript" / "transcript_repaired.md"
    repaired.write_text("旧命名 Session 也能导出。", encoding="utf-8")

    outcome = ExportPackageBuilder(config, db).build(old_id)

    assert outcome.session_id == old_id
    assert outcome.output_dir.name == "2026-09-03_1400_量子力学_001"
    assert outcome.manifest_path.is_file()


# ----------------------------------------------------------------- 术语表

# 本地 ASR 与 GPT 之间唯一的领域知识通道。它一度是断的：ingest 已经按课表认出
# 是哪门课、也把词表读进了内存，但 ASR 侧 hotwords 被实录 A/B 关掉，投喂包这边
# 又从来没把词表交出去 —— 65 条术语读进内存后谁都没用上。
# 2026-09-09 量子力学实测：术语只在转录里命中 2 条（3%）。


def test_prompt_carries_the_course_glossary(config, db):
    """投喂包必须把本课术语表交给 GPT，否则它没有依据订正近音错字。"""
    manager, meta = _create_session(config, db)
    prompt = _prompt_of(ExportPackageBuilder(config, db).build(meta.session_id))
    assert "## 本课术语表" in prompt
    # 词表里的词必须真的出现在 prompt 里
    terms = [
        line.strip()
        for line in (config.glossary_dir / "quantum_mechanics.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert terms, "测试夹具的术语表不该是空的"
    for term in terms[:5]:
        assert term in prompt, f"术语 {term} 没有出现在投喂 prompt 里"


def test_prompt_tells_gpt_to_fix_homophone_errors(config, db):
    """光给词表不够，还要说清楚为什么给 —— 否则 GPT 不会主动去订正。"""
    manager, meta = _create_session(config, db)
    prompt = _prompt_of(ExportPackageBuilder(config, db).build(meta.session_id))
    assert "近音" in prompt
    assert "订正" in prompt
    # 不能变成"随便改"：必须保留"无法判断时不要猜"的约束
    assert "不要猜" in prompt


def test_prompt_asks_for_simplified_output(config, db):
    """转录简繁混杂是 ASR 产物，成稿该统一 —— 交给 GPT 做比本地转换省事且更准。"""
    manager, meta = _create_session(config, db)
    prompt = _prompt_of(ExportPackageBuilder(config, db).build(meta.session_id))
    assert "简体" in prompt


def test_missing_glossary_does_not_break_the_package(config, db, monkeypatch):
    """没有词表的课（unknown）照样要能出包，只是少一段。"""
    manager, meta = _create_session(config, db)
    monkeypatch.setattr(
        "lecture_ai.transcription.load_glossary",
        lambda *a, **k: type("G", (), {"terms": [], "sources": []})(),
    )
    outcome = ExportPackageBuilder(config, db).build(meta.session_id)
    prompt = _prompt_of(outcome)
    assert outcome.output_dir
    assert "暂无术语表" in prompt
