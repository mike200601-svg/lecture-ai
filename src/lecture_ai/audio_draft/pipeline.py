"""Phase 2D：只读正式 STRUCTURED/KNOWLEDGE，生成 audio-only 课堂草稿。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from lecture_ai.audio_draft.models import AudioDraftOutcome
from lecture_ai.audio_draft.prompting import load_draft_prompt, render_draft_prompt
from lecture_ai.audio_draft.renderer import render_audio_draft
from lecture_ai.audio_draft.schema import DRAFT_RESPONSE_SCHEMA, validate_draft_response
from lecture_ai.cleaning.pipeline import CLEAN_JSON
from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LLMError, WebResponseRequired
from lecture_ai.knowledge.pipeline import (
    KNOWLEDGE_JSON,
    UNRESOLVED_VISUAL_JSON,
    KnowledgePipeline,
)
from lecture_ai.knowledge.schema import KNOWLEDGE_FIELDS, validate_knowledge_response
from lecture_ai.llm import LLMClient, build_llm_client
from lecture_ai.session import SessionManager
from lecture_ai.structure.pipeline import OUTLINE_JSON
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text, ensure_dir
from lecture_ai.utils.timefmt import now_local, to_iso

AUDIO_DRAFT_JSON = "audio_draft.json"
AUDIO_DRAFT_MD = "lecture_audio_draft.md"
DRAFT_SCHEMA_VERSION = 1
STEP_NOTE = "note"


class AudioDraftPipeline:
    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        *,
        client: LLMClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)
        self._client = client
        self._sleep = sleep

    def run(
        self,
        session_id: str,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> AudioDraftOutcome:
        started = time.monotonic()
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)
        analysis = session_dir / "analysis"
        clean_path = analysis / CLEAN_JSON
        outline_path = analysis / OUTLINE_JSON
        knowledge_path = analysis / KNOWLEDGE_JSON
        unresolved_path = analysis / UNRESOLVED_VISUAL_JSON

        cleaned = KnowledgePipeline._read_cleaned(clean_path, session_id)
        segments = KnowledgePipeline._normalize_segments(cleaned["segments"])
        clean_sha = sha256_file(clean_path)
        outline = KnowledgePipeline._read_outline(
            outline_path, session_id, clean_sha, segments
        )
        outline_sha = sha256_file(outline_path)
        knowledge = self._read_knowledge(
            knowledge_path,
            session_id=session_id,
            clean_sha=clean_sha,
            outline_sha=outline_sha,
            segments=segments,
            outline=outline,
        )
        knowledge_sha = sha256_file(knowledge_path)
        unresolved = self._read_unresolved(
            unresolved_path,
            session_id=session_id,
            clean_sha=clean_sha,
            outline_sha=outline_sha,
            knowledge=knowledge,
        )
        unresolved_sha = sha256_file(unresolved_path)
        prompt_path, template = load_draft_prompt(self.config.paths.project_root)
        prompt_sha = sha256_file(prompt_path)
        fingerprint = self._fingerprint(
            outline_sha=outline_sha,
            knowledge_sha=knowledge_sha,
            unresolved_sha=unresolved_sha,
            prompt_sha=prompt_sha,
        )
        output_json = analysis / AUDIO_DRAFT_JSON
        output_md = session_dir / "note" / AUDIO_DRAFT_MD

        if dry_run:
            return AudioDraftOutcome(
                session_id=session_id,
                topic_count=len(outline["lecture_topics"]),
                dry_run=True,
                elapsed_sec=time.monotonic() - started,
                message="STRUCTURED、KNOWLEDGE 与视觉未决输入有效；计划生成 1 个草稿编排任务",
            )
        if not force and self._valid_outputs(
            output_json,
            output_md,
            fingerprint=fingerprint,
            outline_sha=outline_sha,
            knowledge_sha=knowledge_sha,
            unresolved_sha=unresolved_sha,
            outline=outline,
            knowledge=knowledge,
        ):
            payload = json.loads(output_json.read_text(encoding="utf-8"))
            self.sessions.mark_step(
                meta,
                STEP_NOTE,
                "done",
                elapsed_sec=0.0,
                provider=payload.get("provider"),
                model=payload.get("model"),
            )
            return AudioDraftOutcome(
                session_id=session_id,
                topic_count=len(outline["lecture_topics"]),
                reused=True,
                output_json=str(output_json),
                output_md=str(output_md),
                elapsed_sec=time.monotonic() - started,
                message="复用已有 AUDIO_DRAFT 产物",
            )

        client = self._get_client()
        self.sessions.mark_step(meta, STEP_NOTE, "running")
        prompt = render_draft_prompt(
            template,
            course_name=meta.course.name,
            date=meta.date,
            session_id=session_id,
            outline=outline,
            knowledge=knowledge,
            unresolved_visual=unresolved,
        )
        cache_key = _json_sha({
            "fingerprint": fingerprint,
            "outline_sha256": outline_sha,
            "knowledge_sha256": knowledge_sha,
            "unresolved_visual_sha256": unresolved_sha,
        })
        cache_path = analysis / "audio_draft_cache.json"
        exchange_dir = analysis / "audio_draft_web" / "draft"
        try:
            record = self._cached_or_call(
                cache_path=cache_path,
                cache_key=cache_key,
                prompt=prompt,
                outline=outline,
                knowledge=knowledge,
                client=client,
                exchange_dir=exchange_dir,
                request_context={
                    "pipeline": "audio_draft",
                    "artifact": f"analysis/{AUDIO_DRAFT_JSON}+note/{AUDIO_DRAFT_MD}",
                    "stage": "audio_draft",
                    "index": "draft",
                    "session_id": session_id,
                    "course": meta.course.name,
                    "source_layer": "STRUCTURED+KNOWLEDGE+UNRESOLVED_VISUAL",
                    "source_sha256": knowledge_sha,
                    "fingerprint": fingerprint,
                    "schema_version": DRAFT_SCHEMA_VERSION,
                    "exchange_dir": exchange_dir,
                },
                force=force,
            )
        except WebResponseRequired as exc:
            self.sessions.mark_step(
                meta, STEP_NOTE, "pending", provider=client.provider, model=client.model
            )
            return AudioDraftOutcome(
                session_id=session_id,
                topic_count=len(outline["lecture_topics"]),
                partial=True,
                elapsed_sec=time.monotonic() - started,
                message="等待 GPT 网页返回 audio-only 草稿编排 JSON",
                tasks=[{
                    "pipeline": "audio_draft",
                    "stage": "audio_draft",
                    "index": "draft",
                    "waiting": True,
                    "prompt": str(exchange_dir / "prompt.md"),
                    "response": str(exchange_dir / "response.json"),
                    "message": str(exc),
                }],
            )
        except Exception as exc:
            self.sessions.mark_step(meta, STEP_NOTE, "failed", error=str(exc))
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Phase 2D 草稿生成失败：{exc}") from exc

        current_hashes = (
            sha256_file(outline_path),
            sha256_file(knowledge_path),
            sha256_file(unresolved_path),
        )
        if current_hashes != (outline_sha, knowledge_sha, unresolved_sha):
            error = "Phase 2D 输入在执行期间发生变化，拒绝写入草稿"
            self.sessions.mark_step(meta, STEP_NOTE, "failed", error=error)
            raise LLMError(error)
        elapsed = time.monotonic() - started
        markdown = render_audio_draft(
            session_id=session_id,
            course=meta.course.name,
            date=meta.date,
            draft=record["result"],
            outline=outline,
            knowledge=knowledge,
            unresolved_visual=unresolved,
        )
        markdown_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        payload = {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "layer": "AUDIO_DRAFT",
            "session_id": session_id,
            "course": meta.course.name,
            "date": meta.date,
            "created_at": to_iso(now_local()),
            "provider": record["provider"],
            "model": record["model"],
            "source": {
                "outline_file": OUTLINE_JSON,
                "outline_sha256": outline_sha,
                "knowledge_file": KNOWLEDGE_JSON,
                "knowledge_sha256": knowledge_sha,
                "unresolved_visual_file": UNRESOLVED_VISUAL_JSON,
                "unresolved_visual_sha256": unresolved_sha,
            },
            "generation": {
                "fingerprint": fingerprint,
                "prompt": "prompts/lecture_note.md",
                "prompt_sha256": prompt_sha,
                "markdown_file": f"note/{AUDIO_DRAFT_MD}",
                "markdown_sha256": markdown_sha,
                "audio_only": True,
                "final": False,
                "elapsed_sec": round(elapsed, 2),
            },
            "usage": record.get("usage") or {},
            "request": {
                "request_id": record.get("request_id"),
                "cache_hit": bool(record.get("cache_hit")),
                "retries": int(record.get("retries", 0)),
            },
            **record["result"],
        }
        try:
            ensure_dir(output_md.parent)
            atomic_write_text(output_md, markdown)
            atomic_write_text(output_json, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError as exc:
            self.sessions.mark_step(meta, STEP_NOTE, "failed", error=str(exc))
            raise LLMError(f"Phase 2D 产物写入失败：{exc}") from exc
        self.sessions.mark_step(
            meta,
            STEP_NOTE,
            "done",
            elapsed_sec=elapsed,
            provider=record["provider"],
            model=record["model"],
        )
        return AudioDraftOutcome(
            session_id=session_id,
            topic_count=len(outline["lecture_topics"]),
            output_json=str(output_json),
            output_md=str(output_md),
            elapsed_sec=elapsed,
            message=f"生成 {len(record['result']['sections'])} 个可追溯章节；仍是 audio-only 草稿",
        )

    def _read_knowledge(
        self,
        path: Path,
        *,
        session_id: str,
        clean_sha: str,
        outline_sha: str,
        segments: list[dict[str, Any]],
        outline: dict[str, Any],
    ) -> dict[str, Any]:
        data = _read_json(path, "Phase 2D 缺少正式 KNOWLEDGE")
        if data.get("layer") != "KNOWLEDGE" or data.get("session_id") != session_id:
            raise LLMError("Phase 2D knowledge 必须属于当前 Session 的正式 KNOWLEDGE")
        source = data.get("source") or {}
        if (
            source.get("cleaned_sha256") != clean_sha
            or source.get("outline_sha256") != outline_sha
        ):
            raise LLMError("Phase 2D knowledge 的上游 SHA 已过期")
        if not isinstance(data.get("extraction", {}).get("fingerprint"), str):
            raise LLMError("knowledge 缺少正式 extraction fingerprint")
        response = {field: data.get(field) for field in KNOWLEDGE_FIELDS}
        normalized = validate_knowledge_response(
            response,
            segments,
            outline,
            concept_threshold=float(self.config.obsidian.concept_threshold),
        )
        return {**data, **normalized}

    @staticmethod
    def _read_unresolved(
        path: Path,
        *,
        session_id: str,
        clean_sha: str,
        outline_sha: str,
        knowledge: dict[str, Any],
    ) -> dict[str, Any]:
        data = _read_json(path, "Phase 2D 缺少正式 UNRESOLVED_VISUAL")
        if data.get("layer") != "UNRESOLVED_VISUAL" or data.get("session_id") != session_id:
            raise LLMError("Phase 2D unresolved_visual 必须属于当前 Session")
        source = data.get("source") or {}
        if (
            source.get("cleaned_sha256") != clean_sha
            or source.get("outline_sha256") != outline_sha
            or data.get("extraction", {}).get("fingerprint")
            != knowledge.get("extraction", {}).get("fingerprint")
        ):
            raise LLMError("unresolved_visual 与 knowledge/upstream 不一致")
        items = data.get("items")
        if not isinstance(items, list) or data.get("item_count") != len(items):
            raise LLMError("unresolved_visual items 或 item_count 非法")
        expected = {str(item["id"]): item for item in knowledge["visual_references"]}
        actual: dict[str, dict[str, Any]] = {}
        actual_ids: list[str] = []
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(item_id, str) or item_id in actual or item_id not in expected:
                raise LLMError(f"unresolved_visual id 非法或重复：{item_id}")
            if item.get("status") != "unresolved":
                raise LLMError(f"visual reference {item_id} 被错误标记为已解决")
            if set(item) != set(expected[item_id]) | {"status", "source_visual_labels"}:
                raise LLMError(f"unresolved_visual {item_id} 字段不符合正式 schema")
            if not isinstance(item.get("source_visual_labels"), list) or not all(
                isinstance(value, str) for value in item["source_visual_labels"]
            ):
                raise LLMError(f"unresolved_visual {item_id} source_visual_labels 非法")
            for key, value in expected[item_id].items():
                if item.get(key) != value:
                    raise LLMError(f"unresolved_visual {item_id} 与 knowledge 字段 {key} 不一致")
            actual[item_id] = item
            actual_ids.append(item_id)
        if actual_ids != list(expected):
            raise LLMError("unresolved_visual 未完整覆盖 knowledge visual_references")
        return data

    def _fingerprint(
        self,
        *,
        outline_sha: str,
        knowledge_sha: str,
        unresolved_sha: str,
        prompt_sha: str,
    ) -> str:
        return _json_sha({
            "schema_version": DRAFT_SCHEMA_VERSION,
            "outline_sha256": outline_sha,
            "knowledge_sha256": knowledge_sha,
            "unresolved_visual_sha256": unresolved_sha,
            "prompt_sha256": prompt_sha,
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "response_schema": DRAFT_RESPONSE_SCHEMA,
        })

    @staticmethod
    def _valid_outputs(
        output_json: Path,
        output_md: Path,
        *,
        fingerprint: str,
        outline_sha: str,
        knowledge_sha: str,
        unresolved_sha: str,
        outline: dict[str, Any],
        knowledge: dict[str, Any],
    ) -> bool:
        if not output_json.is_file() or not output_md.is_file():
            return False
        try:
            data = json.loads(output_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        valid_metadata = (
            data.get("layer") == "AUDIO_DRAFT"
            and data.get("generation", {}).get("fingerprint") == fingerprint
            and data.get("source", {}).get("outline_sha256") == outline_sha
            and data.get("source", {}).get("knowledge_sha256") == knowledge_sha
            and data.get("source", {}).get("unresolved_visual_sha256") == unresolved_sha
            and data.get("generation", {}).get("markdown_sha256") == sha256_file(output_md)
            and data.get("generation", {}).get("audio_only") is True
            and data.get("generation", {}).get("final") is False
        )
        if not valid_metadata:
            return False
        try:
            validate_draft_response(
                {field: data.get(field) for field in ("title", "sections", "closing_summary")},
                outline,
                knowledge,
            )
        except LLMError:
            return False
        return True

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = build_llm_client(self.config)
        return self._client

    def _cached_or_call(
        self,
        *,
        cache_path: Path,
        cache_key: str,
        prompt: str,
        outline: dict[str, Any],
        knowledge: dict[str, Any],
        client: LLMClient,
        exchange_dir: Path,
        request_context: dict[str, Any],
        force: bool,
    ) -> dict[str, Any]:
        if cache_path.is_file() and not force:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("cache_key") == cache_key:
                    cached["result"] = validate_draft_response(
                        cached.get("result"), outline, knowledge
                    )
                    cached["cache_hit"] = True
                    return cached
            except (OSError, json.JSONDecodeError, LLMError):
                pass
        last_error: Exception | None = None
        for attempt in range(self.config.clean.max_retries + 1):
            call_started = time.monotonic()
            try:
                response = client.complete(
                    prompt,
                    json_schema=DRAFT_RESPONSE_SCHEMA,
                    max_tokens=self.config.llm.max_tokens,
                    temperature=self.config.llm.temperature,
                    request_context=request_context,
                )
                parsed = json.loads(response.text)
                result = validate_draft_response(parsed, outline, knowledge)
                record = {
                    "schema_version": DRAFT_SCHEMA_VERSION,
                    "cache_key": cache_key,
                    "cache_hit": False,
                    "provider": response.provider,
                    "model": response.model,
                    "request_id": response.request_id,
                    "usage": response.usage,
                    "retries": attempt,
                    "elapsed_sec": round(time.monotonic() - call_started, 3),
                    "created_at": to_iso(now_local()),
                    "result": result,
                }
                atomic_write_text(cache_path, json.dumps(record, ensure_ascii=False, indent=2))
                return record
            except Exception as exc:
                if isinstance(exc, WebResponseRequired):
                    raise
                if client.provider == "chatgpt_web" and self._reject_web_response(
                    exchange_dir, exc
                ):
                    raise WebResponseRequired(
                        f"GPT 网页草稿结果未通过严格校验；已生成 {exchange_dir / 'retry.md'}"
                    ) from exc
                last_error = exc
                if attempt >= self.config.clean.max_retries:
                    break
                self._sleep(self.config.clean.retry_base_seconds * (2 ** attempt))
        raise LLMError(
            f"Phase 2D 响应在 {self.config.clean.max_retries + 1} 次尝试后失败：{last_error}"
        ) from last_error

    @staticmethod
    def _reject_web_response(exchange_dir: Path, error: Exception) -> bool:
        response_path = exchange_dir / "response.json"
        if not response_path.is_file():
            return False
        digest = sha256_file(response_path)[:12]
        rejected = exchange_dir / f"response.rejected.{digest}.json"
        suffix = 1
        while rejected.exists():
            rejected = exchange_dir / f"response.rejected.{digest}.{suffix}.json"
            suffix += 1
        response_path.rename(rejected)
        atomic_write_text(
            exchange_dir / "retry.md",
            "\n".join((
                "# GPT 网页 Phase 2D 结果被拒绝",
                "",
                f"校验错误：{error}",
                f"已封存原响应：{rejected.name}",
                "",
                "请重新执行当前 prompt.md，返回完整 JSON。不得遗漏任何 knowledge item，",
                "不得补写知识、跨 topic 编排或把视觉/不确定内容伪装成已解决。",
            )),
        )
        return True


def _read_json(path: Path, missing: str) -> dict[str, Any]:
    if not path.is_file():
        raise LLMError(f"{missing}：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LLMError(f"{path.name} 无法读取：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMError(f"{path.name} 顶层必须是 object")
    return data


def _json_sha(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
