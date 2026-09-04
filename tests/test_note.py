"""API 路线成稿（lecture-ai note）的边界、幂等与输出规范。

原则与 export-package 一致：REPAIRED 是唯一文本来源，上游只读，
不发送任何图片，重复生成必须显式 --force。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from lecture_ai.errors import NoteError
from lecture_ai.llm.fake import FakeLLMClient
from lecture_ai.note import (
    NoteBuilder,
    normalize_math_delimiters,
    strip_leading_front_matter,
    strip_wrapping_fence,
)
from lecture_ai.session import SessionManager, load_courses

START = datetime(2026, 9, 3, 14, 0)
PREFIX = "2026-09-03_1400_量子力学_001"
BS = chr(92)


def _session(config, db, *, repaired: bool = True):
    manager = SessionManager(config, db)
    course = load_courses(config.courses_path).get("quantum_mechanics")
    meta = manager.create(course, START)
    if repaired:
        path = manager.session_dir(meta.session_id) / "transcript" / "transcript_repaired.md"
        path.write_text("# 修复后转录\n\n老师讲了波函数的归一化。\n", encoding="utf-8")
    return manager, meta


def _builder(config, db, body: str = "# 课堂笔记\n\n正文。\n") -> NoteBuilder:
    return NoteBuilder(config, db, client=FakeLLMClient(responder=lambda _prompt: body))


# ----------------------------------------------------------------- 输入边界


def test_repaired_is_required_without_fallback(config, db):
    manager, meta = _session(config, db, repaired=False)
    raw = manager.session_dir(meta.session_id) / "transcript" / "transcript_raw.md"
    raw.write_text("只有 RAW", encoding="utf-8")

    with pytest.raises(NoteError, match="禁止回退到 RAW"):
        _builder(config, db).build(meta.session_id)

    assert raw.read_text(encoding="utf-8") == "只有 RAW"


def test_web_provider_is_rejected(config, db):
    _manager, meta = _session(config, db)
    config.llm.provider = "chatgpt_web"

    # 未注入 client 时才走 provider 检查：网页路线语义对不上自由格式成稿。
    with pytest.raises(NoteError, match="export-package"):
        NoteBuilder(config, db).build(meta.session_id)


def test_board_and_slides_produce_warnings_but_are_never_sent(config, db):
    manager, meta = _session(config, db)
    session_dir = manager.session_dir(meta.session_id)
    (session_dir / "images" / "board.jpg").write_bytes(b"board")
    (session_dir / "slides" / "lecture.pdf").write_bytes(b"slides")

    captured: dict[str, str] = {}

    def responder(prompt: str) -> str:
        captured["prompt"] = prompt
        return "# 笔记\n\n正文。\n"

    outcome = NoteBuilder(
        config, db, client=FakeLLMClient(responder=responder)
    ).build(meta.session_id)

    assert len(outcome.warnings) == 2
    assert any("板书" in w for w in outcome.warnings)
    assert any("课件" in w for w in outcome.warnings)
    assert "board.jpg" not in captured["prompt"]
    assert "lecture.pdf" not in captured["prompt"]


# ----------------------------------------------------------------- 输出规范


def test_output_name_matches_web_route_and_has_front_matter(config, db):
    _manager, meta = _session(config, db)
    outcome = _builder(config, db).build(meta.session_id)

    assert outcome.output_path.name == f"{PREFIX}_final_note.md"
    text = outcome.output_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")

    front = yaml.safe_load(text.split("---\n")[1])
    assert front["course"] == "量子力学"
    assert front["course_key"] == "quantum_mechanics"
    assert front["session_id"] == meta.session_id
    assert front["source_layer"] == "REPAIRED"
    assert front["materials"] == "transcript-only"
    assert len(front["transcript_sha256"]) == 64


def test_model_front_matter_is_replaced_not_duplicated(config, db):
    _manager, meta = _session(config, db)
    body = "---\ntitle: 模型自己写的\n---\n\n# 正文\n"
    outcome = _builder(config, db, body).build(meta.session_id)

    text = outcome.output_path.read_text(encoding="utf-8")
    assert text.count("---\n") == 2  # 只剩程序生成的那一组
    assert "模型自己写的" not in text


def test_math_delimiters_are_normalized_on_write(config, db):
    _manager, meta = _session(config, db)
    body = f"# 笔记\n\n{BS}[\nE = mc^2\n{BS}]\n\n行内 {BS}(n{BS}) 位。\n"
    outcome = _builder(config, db, body).build(meta.session_id)

    text = outcome.output_path.read_text(encoding="utf-8")
    assert "$$\nE = mc^2\n$$" in text
    assert "行内 $n$ 位。" in text
    assert BS + "[" not in text


# ----------------------------------------------------------------- 幂等


def test_existing_note_is_not_overwritten_without_force(config, db):
    _manager, meta = _session(config, db)
    first = _builder(config, db, "# 第一版\n").build(meta.session_id)

    with pytest.raises(NoteError, match="--force"):
        _builder(config, db, "# 第二版\n").build(meta.session_id)
    assert "第一版" in first.output_path.read_text(encoding="utf-8")

    second = _builder(config, db, "# 第二版\n").build(meta.session_id, force=True)
    assert "第二版" in second.output_path.read_text(encoding="utf-8")


def test_dry_run_writes_nothing_and_calls_no_model(config, db):
    _manager, meta = _session(config, db)
    client = FakeLLMClient(responder=lambda _p: "# 不该被调用\n")
    outcome = NoteBuilder(config, db, client=client).build(meta.session_id, dry_run=True)

    assert outcome.dry_run is True
    assert client.calls == 0
    assert not outcome.output_path.exists()
    assert outcome.prompt_chars > 0


def test_step_is_recorded_in_metadata(config, db):
    manager, meta = _session(config, db)
    _builder(config, db).build(meta.session_id)

    reloaded = manager.load(meta.session_id)
    assert reloaded.step("note").status == "done"
    assert reloaded.step("note").provider == "fake"


def test_empty_model_output_fails_without_writing(config, db):
    _manager, meta = _session(config, db)
    with pytest.raises(NoteError, match="空成稿"):
        _builder(config, db, "   \n").build(meta.session_id)

    manager = SessionManager(config, db)
    session_dir = manager.session_dir(meta.session_id)
    assert list((session_dir / "note").glob("*.md")) == []


# ----------------------------------------------------------------- 纯函数


def test_normalize_math_leaves_code_fences_alone():
    text = f"```python\nprint({BS}(x{BS}))\n```\n\n{BS}(y{BS})\n"
    result = normalize_math_delimiters(text)
    assert f"print({BS}(x{BS}))" in result
    assert "$y$" in result


def test_strip_wrapping_fence_only_when_outermost():
    wrapped = "```markdown\n# 标题\n正文\n```"
    assert strip_wrapping_fence(wrapped).strip() == "# 标题\n正文"

    with_inner = "```markdown\n# 标题\n```py\nx\n```\n```"
    assert strip_wrapping_fence(with_inner) == with_inner


def test_strip_front_matter_ignores_horizontal_rule():
    text = "# 标题\n\n---\n\n正文\n"
    assert strip_leading_front_matter(text) == text
