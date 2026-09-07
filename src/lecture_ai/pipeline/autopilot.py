"""Phase 1 收尾自动化：转录完成后自动补 repair，再自动出投喂包。

Phase1Pipeline 到 TRANSCRIBED 就结束了，而投喂包要求正式 REPAIRED 转录，
中间这一步以前只能手动跑。课上完就想直接去 exports 目录取包，所以把
`repair` 和 `export-package` 挂进 watch 循环。

只做本地确定性的事：不上传、不调用网络 LLM。两个开关都可在 config.yaml 关掉。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LectureAIError
from lecture_ai.export_package import ExportPackageBuilder
from lecture_ai.logging_setup import get_logger
from lecture_ai.repair import REPAIRED_MD, RepairPipeline
from lecture_ai.session import SessionManager
from lecture_ai.session.models import SessionState

log = get_logger(__name__)

#: Phase 1 已结束、可以进入 repair/打包的状态。
ELIGIBLE_STATES = frozenset(
    {
        SessionState.TRANSCRIBED,
        SessionState.IMAGES_READY,
        SessionState.FUSING,
        SessionState.GENERATING_NOTE,
        SessionState.EXPORTED,
        SessionState.DONE,
    }
)

#: 打包时会被收集的素材目录，用于判断"用户是否还在往里传板书"。
MATERIAL_DIRS = ("images", "slides")


@dataclass
class AutopilotOutcome:
    session_id: str
    action: str          # repair | export
    ok: bool
    message: str
    output_dir: str | None = None


class AutopilotService:
    """把 TRANSCRIBED 的 session 一路推到"投喂包已就绪"。"""

    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        *,
        repair: RepairPipeline | None = None,
        exporter: ExportPackageBuilder | None = None,
    ) -> None:
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)
        self.repair = repair or RepairPipeline(config, self.db)
        self.exporter = exporter or ExportPackageBuilder(config, self.db)

    def run_once(self) -> list[AutopilotOutcome]:
        """扫一遍所有 session，把该补的 repair 和投喂包补上。"""
        processing = self.config.processing
        if not (processing.auto_repair or processing.auto_export_package):
            return []

        results: list[AutopilotOutcome] = []
        for session_id in self.sessions.list_ids():
            try:
                meta = self.sessions.load(session_id)
            except LectureAIError as exc:
                log.error("autopilot 读取 session 失败：%s：%s", session_id, exc)
                continue
            if meta.state not in ELIGIBLE_STATES:
                continue

            session_dir = self.sessions.session_dir(session_id)
            if processing.auto_repair and not (
                session_dir / "transcript" / REPAIRED_MD
            ).is_file():
                results.append(self._repair(session_id))

            if processing.auto_export_package:
                outcome = self._export(session_id, session_dir)
                if outcome is not None:
                    results.append(outcome)
        return results

    # ------------------------------------------------------------------ 单步
    def _repair(self, session_id: str) -> AutopilotOutcome:
        try:
            outcome = self.repair.run(session_id)
        except LectureAIError as exc:
            return AutopilotOutcome(session_id, "repair", False, f"自动 repair 失败：{exc}")
        except Exception as exc:  # 不能让单个 session 打断 watch 主循环
            log.exception("自动 repair 发生未预期错误：%s", session_id)
            return AutopilotOutcome(session_id, "repair", False, f"自动 repair 异常：{exc}")
        return AutopilotOutcome(
            session_id, "repair", True,
            f"自动 repair 完成：检出 {outcome.regions_detected} 段、"
            f"采纳 {outcome.regions_accepted} 段",
        )

    def _export(self, session_id: str, session_dir: Path) -> AutopilotOutcome | None:
        try:
            needed, reason = self.exporter.needs_rebuild(session_id)
        except LectureAIError as exc:
            return AutopilotOutcome(session_id, "export", False, f"检查投喂包失败：{exc}")
        if not needed:
            return None
        # 板书/课件可能正在同步，等它静默下来再打包，避免打进半个文件。
        if not self._materials_settled(session_dir):
            return None
        try:
            outcome = self.exporter.build(session_id)
        except LectureAIError as exc:
            return AutopilotOutcome(session_id, "export", False, f"自动打包失败：{exc}")
        except Exception as exc:
            log.exception("自动打包发生未预期错误：%s", session_id)
            return AutopilotOutcome(session_id, "export", False, f"自动打包异常：{exc}")
        return AutopilotOutcome(
            session_id, "export", True,
            f"投喂包已就绪（{reason}；板书 {outcome.board_count} / 课件 {outcome.slide_count}）",
            output_dir=str(outcome.output_dir),
        )

    def _materials_settled(self, session_dir: Path) -> bool:
        """素材目录里最近一次改动是否已超过 quiet_seconds。"""
        quiet = max(0, self.config.processing.quiet_seconds)
        if not quiet:
            return True
        newest = 0.0
        for name in MATERIAL_DIRS:
            directory = session_dir / name
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if path.is_file():
                    try:
                        newest = max(newest, path.stat().st_mtime)
                    except OSError:
                        continue
        return not newest or (time.time() - newest) >= quiet
