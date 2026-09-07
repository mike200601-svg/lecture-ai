"""Phase 1 收尾自动化：自动 repair + 自动出投喂包。

重点是"课上完直接去 exports 取包"这条链路能自己跑通，且不会每轮重复打包、
不会因为单个 session 出错就拖垮 watch。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from lecture_ai.errors import ExportPackageError, LectureAIError
from lecture_ai.export_package import ExportPackageBuilder
from lecture_ai.pipeline.autopilot import AutopilotService
from lecture_ai.repair.models import RepairOutcome
from lecture_ai.session import SessionManager, load_courses
from lecture_ai.session.models import SessionState

START = datetime(2026, 9, 7, 9, 44)

TO_TRANSCRIBED = (
    SessionState.AUDIO_READY,
    SessionState.TRANSCRIBING,
    SessionState.TRANSCRIBED,
)


class FakeRepair:
    """替身：真 repair 要音频和 ASR，这里只关心它有没有被调用、产物有没有落地。"""

    def __init__(self, manager, *, fail: bool = False, writes: bool = True):
        self.manager = manager
        self.fail = fail
        self.writes = writes
        self.calls: list[str] = []

    def run(self, session_id, **_kwargs):
        self.calls.append(session_id)
        if self.fail:
            raise LectureAIError("模拟 repair 失败")
        if self.writes:
            path = (self.manager.session_dir(session_id)
                    / "transcript" / "transcript_repaired.md")
            path.write_text("# 修复后转录\n\n课堂内容。\n", encoding="utf-8")
        return RepairOutcome(
            session_id=session_id, regions_detected=1, regions_processed=1,
            regions_accepted=1, message="ok",
        )


def _session(config, db, *, repaired: bool = False, state=SessionState.TRANSCRIBED):
    manager = SessionManager(config, db)
    course = load_courses(config.courses_path).get("quantum_mechanics")
    meta = manager.create(course, START)
    for target in TO_TRANSCRIBED:
        meta = manager.transition(meta, target)
        if meta.state == state:
            break
    if repaired:
        (manager.session_dir(meta.session_id)
         / "transcript" / "transcript_repaired.md").write_text(
            "# 修复后转录\n\n课堂内容。\n", encoding="utf-8")
    return manager, meta


def _service(config, db, manager, **kwargs):
    return AutopilotService(config, db, repair=FakeRepair(manager, **kwargs))


# ------------------------------------------------------------ needs_rebuild


def test_needs_rebuild_true_before_first_build(config, db):
    manager, meta = _session(config, db, repaired=True)
    needed, reason = ExportPackageBuilder(config, db).needs_rebuild(meta.session_id)
    assert needed is True
    assert "尚未生成" in reason


def test_needs_rebuild_false_right_after_build(config, db):
    manager, meta = _session(config, db, repaired=True)
    builder = ExportPackageBuilder(config, db)
    builder.build(meta.session_id)
    needed, reason = builder.needs_rebuild(meta.session_id)
    assert needed is False
    assert reason == "已是最新"


def test_needs_rebuild_when_transcript_changes(config, db):
    """repair 重跑改了 REPAIRED，投喂包必须跟着更新，否则投喂的是旧稿。"""
    manager, meta = _session(config, db, repaired=True)
    builder = ExportPackageBuilder(config, db)
    builder.build(meta.session_id)

    (manager.session_dir(meta.session_id)
     / "transcript" / "transcript_repaired.md").write_text(
        "# 修复后转录\n\n补了一段。\n", encoding="utf-8")
    needed, reason = builder.needs_rebuild(meta.session_id)
    assert needed is True
    assert "转录已更新" in reason


def test_needs_rebuild_when_board_added_later(config, db):
    """课后才补拍板书是常态，投喂包要能自动把它带上。"""
    manager, meta = _session(config, db, repaired=True)
    builder = ExportPackageBuilder(config, db)
    builder.build(meta.session_id)
    assert builder.needs_rebuild(meta.session_id)[0] is False

    images = manager.session_dir(meta.session_id) / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "board_01.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    needed, reason = builder.needs_rebuild(meta.session_id)
    assert needed is True
    assert "板书" in reason


def test_needs_rebuild_false_without_repaired(config, db):
    manager, meta = _session(config, db, repaired=False)
    needed, _ = ExportPackageBuilder(config, db).needs_rebuild(meta.session_id)
    assert needed is False


# ------------------------------------------------------------------ run_once


def test_autopilot_repairs_then_exports(config, db):
    """TRANSCRIBED 进来，一轮之内 repair 和投喂包都补齐。"""
    manager, meta = _session(config, db, repaired=False)
    service = _service(config, db, manager)

    results = service.run_once()

    assert [r.action for r in results] == ["repair", "export"]
    assert all(r.ok for r in results)
    assert service.repair.calls == [meta.session_id]
    package = results[-1].output_dir
    assert package and (config.paths.export_dir).is_dir()
    assert any(config.paths.export_dir.iterdir())


def test_autopilot_is_idempotent_across_rounds(config, db):
    """第二轮不能重复 repair，也不能重复打包 —— 否则 watch 每 15 秒刷一次包。"""
    manager, meta = _session(config, db, repaired=False)
    service = _service(config, db, manager)

    first = service.run_once()
    second = service.run_once()

    assert len(first) == 2
    assert second == []
    assert service.repair.calls == [meta.session_id]  # 只 repair 了一次


def test_autopilot_skips_sessions_before_transcribed(config, db):
    manager, meta = _session(config, db, state=SessionState.AUDIO_READY)
    service = _service(config, db, manager)
    assert service.run_once() == []
    assert service.repair.calls == []


def test_repair_failure_is_reported_not_raised(config, db):
    """单个 session 修复失败只能记账，不能把 watch 主循环带崩。"""
    manager, meta = _session(config, db, repaired=False)
    service = _service(config, db, manager, fail=True)

    results = service.run_once()

    assert len(results) == 1
    assert results[0].action == "repair"
    assert results[0].ok is False
    assert "模拟 repair 失败" in results[0].message


def test_export_skipped_when_repair_produced_nothing(config, db):
    """repair 没产出 REPAIRED 时不能硬打包（那会回退到 RAW，是明令禁止的）。"""
    manager, meta = _session(config, db, repaired=False)
    service = _service(config, db, manager, writes=False)

    results = service.run_once()

    assert [r.action for r in results] == ["repair"]
    packaged = list(config.paths.export_dir.iterdir()) if config.paths.export_dir.is_dir() else []
    assert packaged == []


def test_switches_off_disables_everything(config, db):
    manager, meta = _session(config, db, repaired=False)
    config.processing.auto_repair = False
    config.processing.auto_export_package = False
    service = _service(config, db, manager)

    assert service.run_once() == []
    assert service.repair.calls == []


def test_auto_repair_off_still_exports_existing_repaired(config, db):
    """只关 repair 的人，手动修完还是应该自动拿到投喂包。"""
    manager, meta = _session(config, db, repaired=True)
    config.processing.auto_repair = False
    service = _service(config, db, manager)

    results = service.run_once()

    assert [r.action for r in results] == ["export"]
    assert service.repair.calls == []


def test_freshly_touched_materials_defer_packaging(config, db):
    """板书还在同步就打包会打进半个文件，必须等它静默。"""
    manager, meta = _session(config, db, repaired=True)
    images = manager.session_dir(meta.session_id) / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "board_01.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    config.processing.quiet_seconds = 600  # 刚写的文件必然落在静默期内

    service = _service(config, db, manager)
    assert service.run_once() == []

    # 把 mtime 推回过去，等同于"同步早已完成"
    old = time.time() - 3600
    import os
    os.utime(images / "board_01.png", (old, old))
    results = service.run_once()
    assert [r.action for r in results] == ["export"]
