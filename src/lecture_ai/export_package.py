"""把一节课的已确认材料复制成可直接投喂 GPT Web 的目录。

这个模块刻意只做文件整理：不读取图片内容、不做 OCR、不猜课件归属，也不上传网页。
Session 内的正式 REPAIRED 转录是唯一文本来源；缺失时直接失败。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import ExportPackageError
from lecture_ai.repair import REPAIRED_MD
from lecture_ai.session import SessionManager
from lecture_ai.session.models import SessionMeta
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text, rel_to
from lecture_ai.utils.slug import slugify
from lecture_ai.utils.timefmt import now_local, parse_iso, to_iso

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
SLIDE_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".odp", ".key", ".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class ExportPackageOutcome:
    session_id: str
    output_dir: Path
    transcript_source: Path
    manifest_path: Path
    board_count: int
    slide_count: int
    unassigned_count: int
    dry_run: bool = False


class ExportPackageBuilder:
    """构建幂等、可审计且不改动上游文件的 GPT Web 投喂包。"""

    def __init__(self, config: Config, db: Database | None = None) -> None:
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)

    def build(
        self,
        session_id: str,
        *,
        board_paths: Iterable[str | Path] = (),
        slide_paths: Iterable[str | Path] = (),
        dry_run: bool = False,
    ) -> ExportPackageOutcome:
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)
        transcript = session_dir / "transcript" / REPAIRED_MD
        if not transcript.is_file():
            raise ExportPackageError(
                f"session {session_id} 缺少正式 {REPAIRED_MD}；"
                "export-package 禁止回退到 RAW，请先完成 selective repair。"
            )

        prefix = self._identity_prefix(meta)
        package_dir = self._safe_destination(prefix)
        board, board_warnings = self._board_sources(meta, session_dir, board_paths)
        slides = self._material_sources(
            [session_dir / "slides"], slide_paths, SLIDE_EXTENSIONS, "课件"
        )
        unassigned = self._unassigned_board(board)
        warnings = board_warnings[:]
        if unassigned:
            warnings.append(
                f"发现 {len(unassigned)} 张未明确归属的板书候选；已列入 unassigned，未打包。"
            )

        names = self._package_names(prefix)
        outcome = ExportPackageOutcome(
            session_id=session_id,
            output_dir=package_dir,
            transcript_source=transcript,
            manifest_path=package_dir / names["manifest"],
            board_count=len(board),
            slide_count=len(slides),
            unassigned_count=len(unassigned),
            dry_run=dry_run,
        )
        if dry_run:
            return outcome

        self.config.paths.export_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(
            tempfile.mkdtemp(prefix=f".{prefix}-", dir=self.config.paths.export_dir)
        )
        try:
            board_dir = temp_dir / "02_board"
            slide_dir = temp_dir / "03_slides"
            board_dir.mkdir()
            slide_dir.mkdir()

            transcript_target = temp_dir / names["transcript"]
            shutil.copy2(transcript, transcript_target)
            board_records = self._copy_materials(
                board, board_dir, prefix=prefix, label="board"
            )
            slide_records = self._copy_materials(
                slides, slide_dir, prefix=prefix, label="slides"
            )

            prompt_path = temp_dir / names["prompt"]
            atomic_write_text(
                prompt_path,
                self._render_prompt(
                    meta,
                    names["transcript"],
                    names["final_note"],
                    board_records,
                    slide_records,
                ),
            )
            info_path = temp_dir / names["info"]
            atomic_write_text(
                info_path,
                self._render_session_info(meta, transcript, board_records, slide_records, warnings),
            )

            manifest = {
                "schema_version": 1,
                "session_id": meta.session_id,
                "course": meta.course.name,
                "course_key": meta.course.key,
                "date": meta.date,
                "start_time": meta.start_time,
                "end_time": meta.end_time,
                "created_at": to_iso(now_local()),
                "package_name": prefix,
                "suggested_final_note": names["final_note"],
                "transcript": {
                    "layer": "REPAIRED",
                    "source_path": rel_to(transcript, self.config.paths.project_root),
                    "package_path": names["transcript"],
                    "sha256": sha256_file(transcript_target),
                    "bytes": transcript_target.stat().st_size,
                },
                "board_files": board_records,
                "slide_files": slide_records,
                "session_info": {
                    "package_path": names["info"],
                    "sha256": sha256_file(info_path),
                },
                "prompt": {
                    "package_path": names["prompt"],
                    "sha256": sha256_file(prompt_path),
                },
                "unassigned": unassigned,
                "warnings": warnings,
            }
            atomic_write_text(
                temp_dir / names["manifest"],
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            self._replace_generated_directory(temp_dir, package_dir)
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            raise

        return outcome

    def _identity_prefix(self, meta: SessionMeta) -> str:
        start = parse_iso(meta.start_time)
        date = meta.date or (start.strftime("%Y-%m-%d") if start else "unknown-date")
        time = start.strftime("%H%M") if start else self._time_from_session_id(meta.session_id)
        course = slugify(meta.course.name or meta.course.key or "unknown-course", max_len=60)
        seq_match = re.search(r"_(\d{3})$", meta.session_id)
        sequence = seq_match.group(1) if seq_match else "001"
        return f"{date}_{time}_{course}_{sequence}"

    @staticmethod
    def _time_from_session_id(session_id: str) -> str:
        match = re.match(r"^\d{4}-\d{2}-\d{2}_(\d{4})_", session_id)
        return match.group(1) if match else "unknown-time"

    def _safe_destination(self, package_name: str) -> Path:
        root = self.config.paths.export_dir.resolve()
        destination = (root / package_name).resolve()
        if destination.parent != root:
            raise ExportPackageError(f"非法导出目录名：{package_name}")
        return destination

    @staticmethod
    def _package_names(prefix: str) -> dict[str, str]:
        return {
            "transcript": f"{prefix}_01_transcript_repaired.md",
            "info": f"{prefix}_session_info.md",
            "prompt": f"{prefix}_NOTE_PROMPT.md",
            "manifest": f"{prefix}_manifest.json",
            "final_note": f"{prefix}_final_note.md",
        }

    def _board_sources(
        self,
        meta: SessionMeta,
        session_dir: Path,
        explicit: Iterable[str | Path],
    ) -> tuple[list[Path], list[str]]:
        sources = self._material_sources(
            [session_dir / "images"], explicit, IMAGE_EXTENSIONS, "板书"
        )
        warnings: list[str] = []
        metadata_paths: list[Path] = []
        for item in meta.images:
            if not isinstance(item, dict):
                continue
            raw = item.get("path") or item.get("file") or item.get("source")
            if not raw:
                continue
            path = Path(str(raw)).expanduser()
            candidates = [path] if path.is_absolute() else [session_dir / path, self.config.paths.project_root / path]
            found = next((candidate for candidate in candidates if candidate.is_file()), None)
            if found is None:
                warnings.append(f"metadata 中已关联的板书不存在：{raw}")
            elif found.suffix.lower() in IMAGE_EXTENSIONS:
                metadata_paths.append(found.resolve())
        return self._dedupe([*sources, *metadata_paths]), warnings

    def _material_sources(
        self,
        automatic_roots: Iterable[Path],
        explicit: Iterable[str | Path],
        extensions: set[str],
        label: str,
    ) -> list[Path]:
        found: list[Path] = []
        for root in automatic_roots:
            if root.is_dir():
                found.extend(
                    path.resolve()
                    for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() in extensions
                )
        for raw in explicit:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.config.paths.project_root / path
            if not path.exists():
                raise ExportPackageError(f"明确指定的{label}不存在：{path}")
            if path.is_dir():
                found.extend(
                    item.resolve()
                    for item in path.rglob("*")
                    if item.is_file() and item.suffix.lower() in extensions
                )
            elif path.suffix.lower() in extensions:
                found.append(path.resolve())
            else:
                raise ExportPackageError(f"不支持的{label}文件类型：{path.name}")
        return self._dedupe(found)

    @staticmethod
    def _dedupe(paths: Iterable[Path]) -> list[Path]:
        unique = {str(path.resolve()).casefold(): path.resolve() for path in paths}
        return sorted(unique.values(), key=lambda path: str(path).casefold())

    def _unassigned_board(self, assigned: Iterable[Path]) -> list[dict[str, str]]:
        assigned_keys = {str(path.resolve()).casefold() for path in assigned}
        incoming = self.config.paths.incoming_images
        if not incoming.is_dir():
            return []
        result = []
        for path in sorted(incoming.rglob("*"), key=lambda p: str(p).casefold()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if str(path.resolve()).casefold() in assigned_keys:
                continue
            result.append(
                {
                    "kind": "board_candidate",
                    "source_path": rel_to(path, self.config.paths.project_root),
                    "reason": "未明确关联到此 session，未打包",
                }
            )
        return result

    def _copy_materials(
        self,
        sources: Iterable[Path],
        destination: Path,
        *,
        prefix: str,
        label: str,
    ) -> list[dict[str, str | int]]:
        records: list[dict[str, str | int]] = []
        for index, source in enumerate(sources, start=1):
            name = f"{prefix}_{label}_{index:03d}{source.suffix.lower()}"
            target = destination / name
            shutil.copy2(source, target)
            records.append(
                {
                    "source_path": rel_to(source, self.config.paths.project_root),
                    "package_path": target.relative_to(destination.parent).as_posix(),
                    "sha256": sha256_file(target),
                    "bytes": target.stat().st_size,
                }
            )
        return records

    def _render_prompt(
        self,
        meta: SessionMeta,
        transcript_name: str,
        final_note_name: str,
        board: list[dict[str, str | int]],
        slides: list[dict[str, str | int]],
    ) -> str:
        template = self.config.paths.project_root / "prompts" / "export_session.md"
        if not template.is_file():
            raise ExportPackageError(f"缺少投喂提示词模板：{template}")
        return (
            template.read_text(encoding="utf-8")
            .replace("{{SESSION_ID}}", meta.session_id)
            .replace("{{COURSE}}", meta.course.name)
            .replace("{{DATE}}", meta.date)
            .replace("{{START_TIME}}", meta.start_time or "unknown")
            .replace("{{TRANSCRIPT_FILE}}", transcript_name)
            .replace("{{FINAL_NOTE_FILE}}", final_note_name)
            .replace("{{BOARD_COUNT}}", str(len(board)))
            .replace("{{SLIDE_COUNT}}", str(len(slides)))
        )

    def _render_session_info(
        self,
        meta: SessionMeta,
        transcript: Path,
        board: list[dict[str, str | int]],
        slides: list[dict[str, str | int]],
        warnings: list[str],
    ) -> str:
        warning_lines = [f"- {warning}" for warning in warnings] or ["- 无"]
        return "\n".join(
            [
                f"# {meta.date} {meta.course.name} · GPT Web 投喂包",
                "",
                f"- Session：`{meta.session_id}`",
                f"- 开始时间：`{meta.start_time or 'unknown'}`",
                f"- 结束时间：`{meta.end_time or 'unknown'}`",
                f"- 正式转录来源：`{rel_to(transcript, self.config.paths.project_root)}`",
                f"- 板书：{len(board)} 个文件",
                f"- 课件：{len(slides)} 个文件",
                "",
                "## 使用方法",
                "",
                "把本目录中的 NOTE_PROMPT、REPAIRED 转录、板书与课件一起上传到固定 GPT 网页会话。",
                f"网页输出请保存为：`{self._package_names(self._identity_prefix(meta))['final_note']}`。",
                "本目录是可重建的输入包；最终笔记请另行保存，不要写回本目录。",
                "",
                "## Warning",
                "",
                *warning_lines,
                "",
            ]
        )

    @staticmethod
    def _replace_generated_directory(temp_dir: Path, destination: Path) -> None:
        # destination 已由 _safe_destination 约束为 export_dir 的单层子目录。
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temp_dir, destination)
