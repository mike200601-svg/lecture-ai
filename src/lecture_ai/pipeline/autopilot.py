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
            if meta.merged_into:
                # 内容已经在主 session 里了，再 repair / 出包就是重复产出
                continue
            if self._waiting_for_siblings(meta):
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

    def _waiting_for_siblings(self, meta, now=None) -> bool:
        """这节课可能还有一段录音没同步过来，先别急着出稿。

        录音中途断开时，后一段往往要晚几十分钟才传完（100+ MB）。不等的话
        会先给半节课出一份投喂包，等另一半到了、自动合并完，又得推翻重来 ——
        桌面上先冒出一个半截的包再消失，比晚半小时拿到完整的包更让人困惑。

        只在「课表时段刚结束不久」这段窗口里等。匹配不到课表就不等 ——
        没有依据说明还有下一段。
        """
        grace = getattr(self.config.processing, "merge_grace_minutes", 0)
        if grace <= 0 or not meta.start_time:
            return False

        from datetime import datetime, timedelta

        from lecture_ai.merge import _slot_of
        from lecture_ai.session import load_courses

        try:
            start = datetime.fromisoformat(str(meta.start_time))
            start = start.replace(tzinfo=None) if start.tzinfo else start
        except ValueError:
            return False

        courses = load_courses(self.config.courses_path, self.config.course.default_course_key)
        slot = _slot_of(
            courses.get(meta.course.key), start, self.config.course.match_tolerance_minutes
        )
        if slot is None:
            return False

        deadline = datetime.combine(start.date(), slot.end) + timedelta(minutes=grace)
        if (now or datetime.now()) >= deadline:
            return False
        log.debug("%s 处于合并宽限期内（到 %s），暂不 repair/出包",
                  meta.session_id, deadline.strftime("%H:%M"))
        return True

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
