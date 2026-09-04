"""REPAIRED → 成稿笔记，一次 API 调用。

这是 A/B 对照选出的默认整理路线（B 路线）的程序化版本：不分块清洗、不抽概念、
不建大纲，把正式转录一次性交给模型出成稿。对照结论见 ROADMAP 第 0 节 ——
9 轮流水线没有比 1 轮直接整理多捞到任何一条知识项，却丢掉了推导过程。

与 `export-package` 的分工（两者不混用）：

- `export-package` 面向 GPT 网页会话：能带上板书照片和课件，由人上传，笔记质量上限更高；
- `note` 走 API：**只发送文本，从不读取任何图片**，因此有板书/课件的课仍应走网页路线。

成稿文件名与网页路线完全一致（`utils.naming`），同一节课不会因为换路线而出现两种命名。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LectureAIError, NoteError
from lecture_ai.llm import LLMClient, build_llm_client
from lecture_ai.repair import REPAIRED_MD
from lecture_ai.session import SessionManager
from lecture_ai.session.models import SessionMeta
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.naming import final_note_name, identity_prefix
from lecture_ai.utils.paths import atomic_write_text
from lecture_ai.utils.timefmt import now_local, to_iso

#: metadata.json 里的步骤名，已在 SessionMeta.STEP_NAMES 中预留。
STEP_NOTE = "note"

#: 网页路线的 provider。它按「写任务包 → 等人工回填严格 JSON」工作，
#: 而成稿是自由格式 Markdown，语义对不上，因此这里直接拒绝而不是凑合。
WEB_PROVIDER = "chatgpt_web"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class NoteOutcome:
    session_id: str
    output_path: Path
    transcript_source: Path
    prompt_chars: int
    provider: str = ""
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


def strip_leading_front_matter(text: str) -> str:
    """去掉模型自己加的 YAML front-matter —— 元数据由程序生成，不能有两份。"""
    stripped = text.lstrip("﻿ \t\r\n")
    if not stripped.startswith("---"):
        return text
    lines = stripped.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "".join(lines[index + 1:]).lstrip("\r\n")
    return text


def strip_wrapping_fence(text: str) -> str:
    """模型偶尔把整篇笔记裹进 ```markdown 围栏里，这里剥掉最外层。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2 or lines[-1].strip() != "```":
        return text
    # 中间若还有围栏，说明这不是最外层包裹，而是正文里的代码块，不能剥。
    if any(line.strip().startswith("```") for line in lines[1:-1]):
        return text
    return "\n".join(lines[1:-1]) + "\n"


def normalize_math_delimiters(text: str) -> str:
    """把 ChatGPT 习惯的 ``\\[..\\]`` / ``\\(..\\)`` 换成 Obsidian 的 ``$$``/``$``。

    Obsidian 的 MathJax 只认 ``$``；``\\[`` 在 Markdown 里是「转义左方括号」，
    会被渲染成一个孤零零的 ``[`` 加一坨裸 LaTeX 源码。提示词里已经要求了，
    这里再兜一次底 —— 模型的格式习惯很顽固。

    代码围栏内不动：那里的反斜杠可能就是代码本身。
    """
    bs = chr(92)
    pairs = ((bs + "[", "$$"), (bs + "]", "$$"), (bs + "(", "$"), (bs + ")", "$"))
    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            for old, new in pairs:
                line = line.replace(old, new)
        out.append(line)
    return "".join(out)


class NoteBuilder:
    """把一节课的 REPAIRED 转录整理成成稿笔记。上游只读，成稿只写 session 的 note/。"""

    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        *,
        client: LLMClient | None = None,
        api_key: str | None = None,
    ) -> None:
        """``client`` 仅测试注入；``api_key`` 供 WebUI 传入内存中的令牌。

        WebUI 走 ``api_key`` 而不是自带 client，是为了保留 provider 检查
        和 registry 里的云端文本隐私闸门。
        """
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)
        self.client = client
        self.api_key = api_key

    def build(
        self,
        session_id: str,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> NoteOutcome:
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)

        transcript = session_dir / "transcript" / REPAIRED_MD
        if not transcript.is_file():
            raise NoteError(
                f"session {session_id} 缺少正式 {REPAIRED_MD}；"
                "note 禁止回退到 RAW 或 CLEANED，请先完成 selective repair。"
            )

        if self.client is None and self._provider() == WEB_PROVIDER:
            raise NoteError(
                f"llm.provider={WEB_PROVIDER} 是网页路线，不能用于 note。\n"
                "  · 想用 API 出稿：把 config.yaml 的 llm.provider 改成 openai，"
                "并在 .env 里填 OPENAI_API_KEY；\n"
                "  · 想继续走网页：用 lecture-ai export-package <session>。"
            )

        prefix = identity_prefix(meta)
        output_path = session_dir / "note" / final_note_name(prefix)
        if output_path.exists() and not force:
            raise NoteError(
                f"成稿已存在：{output_path}\n"
                "  重新生成会覆盖它，请显式加 --force（旧稿不会自动备份）。"
            )

        warnings = self._material_warnings(session_dir)
        prompt = self._render_prompt(meta, transcript)

        if dry_run:
            return NoteOutcome(
                session_id=session_id,
                output_path=output_path,
                transcript_source=transcript,
                prompt_chars=len(prompt),
                warnings=warnings,
                dry_run=True,
            )

        client = self.client or build_llm_client(self.config, api_key=self.api_key)
        self.sessions.mark_step(
            meta, STEP_NOTE, "running", provider=client.provider, model=client.model
        )
        started = time.monotonic()
        try:
            response = client.complete(
                prompt,
                max_tokens=self.config.note.max_output_tokens,
                temperature=self.config.note.temperature,
            )
        except LectureAIError as exc:
            self.sessions.mark_step(
                meta, STEP_NOTE, "failed", elapsed_sec=time.monotonic() - started, error=str(exc)
            )
            raise
        except Exception as exc:
            self.sessions.mark_step(
                meta, STEP_NOTE, "failed", elapsed_sec=time.monotonic() - started, error=str(exc)
            )
            raise NoteError(f"成稿生成失败：{exc}") from exc
        elapsed = time.monotonic() - started

        body = normalize_math_delimiters(
            strip_leading_front_matter(strip_wrapping_fence(response.text))
        ).strip()
        if not body:
            self.sessions.mark_step(
                meta, STEP_NOTE, "failed", elapsed_sec=elapsed, error="模型返回空成稿"
            )
            raise NoteError("模型返回空成稿，未写入任何文件。")

        document = self._front_matter(meta, transcript, response) + body + "\n"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output_path, document)

        self.sessions.mark_step(
            meta,
            STEP_NOTE,
            "done",
            elapsed_sec=elapsed,
            provider=response.provider,
            model=response.model,
        )
        return NoteOutcome(
            session_id=session_id,
            output_path=output_path,
            transcript_source=transcript,
            prompt_chars=len(prompt),
            provider=response.provider,
            model=response.model,
            usage=dict(response.usage),
            warnings=warnings,
        )

    # ------------------------------------------------------------------ 内部

    def _provider(self) -> str:
        return (self.config.llm.provider or "").strip().lower()

    def _material_warnings(self, session_dir: Path) -> list[str]:
        """有板书/课件却走 API，是在无声地丢材料 —— 必须说出来。"""
        warnings: list[str] = []
        images = [
            p for p in (session_dir / "images").glob("**/*")
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        ]
        slides = [p for p in (session_dir / "slides").glob("**/*") if p.is_file()]
        if images:
            warnings.append(
                f"本 session 有 {len(images)} 张板书照片，API 路线不会发送图片，成稿不包含它们。"
                "需要板书请改用 export-package 走网页会话。"
            )
        if slides:
            warnings.append(
                f"本 session 有 {len(slides)} 个课件文件，API 路线不会发送，成稿不包含它们。"
            )
        return warnings

    def _render_prompt(self, meta: SessionMeta, transcript: Path) -> str:
        template = self.config.paths.project_root / "prompts" / "api_note.md"
        if not template.is_file():
            raise NoteError(f"缺少 API 成稿提示词模板：{template}")
        return (
            template.read_text(encoding="utf-8")
            .replace("{{SESSION_ID}}", meta.session_id)
            .replace("{{COURSE}}", meta.course.name)
            .replace("{{DATE}}", meta.date)
            .replace("{{START_TIME}}", meta.start_time or "unknown")
            .replace("{{TRANSCRIPT}}", transcript.read_text(encoding="utf-8"))
        )

    def _front_matter(self, meta: SessionMeta, transcript: Path, response) -> str:
        """程序生成的 YAML 属性块。Obsidian 靠它做查询，模型不许自己写。"""
        data = {
            "course": meta.course.name,
            "course_key": meta.course.key,
            "date": meta.date,
            "start_time": meta.start_time or "unknown",
            "session_id": meta.session_id,
            "source_layer": "REPAIRED",
            "transcript_sha256": sha256_file(transcript),
            "materials": "transcript-only",
            "generated_by": f"{response.provider}/{response.model}",
            "generated_at": to_iso(now_local()),
        }
        body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return "---\n" + body + "---\n\n"
