"""Phase 2C：从正式 CLEANED + STRUCTURED 抽取可追溯知识。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from lecture_ai.cleaning.pipeline import CLEAN_JSON
from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LLMError, WebResponseRequired
from lecture_ai.knowledge.models import KnowledgeOutcome
from lecture_ai.knowledge.prompting import load_knowledge_prompt, render_knowledge_prompt
from lecture_ai.knowledge.schema import KNOWLEDGE_RESPONSE_SCHEMA, validate_knowledge_response
from lecture_ai.llm import LLMClient, build_llm_client
from lecture_ai.session import SessionManager
from lecture_ai.structure.pipeline import OUTLINE_JSON
from lecture_ai.structure.schema import TOP_LEVEL_FIELDS, validate_outline_response
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text
from lecture_ai.utils.timefmt import now_local, to_iso

KNOWLEDGE_JSON = "knowledge.json"
UNRESOLVED_VISUAL_JSON = "unresolved_visual.json"
KNOWLEDGE_SCHEMA_VERSION = 2
STEP_KNOWLEDGE = "knowledge"


class KnowledgePipeline:
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
    ) -> KnowledgeOutcome:
        started = time.monotonic()
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)
        clean_path = session_dir / "analysis" / CLEAN_JSON
        outline_path = session_dir / "analysis" / OUTLINE_JSON
        cleaned = self._read_cleaned(clean_path, session_id)
        segments = self._normalize_segments(cleaned["segments"])
        clean_sha = sha256_file(clean_path)
        outline = self._read_outline(outline_path, session_id, clean_sha, segments)
        outline_sha = sha256_file(outline_path)
        prompt_path, template = load_knowledge_prompt(self.config.paths.project_root)
        prompt_sha = sha256_file(prompt_path)
        threshold = float(self.config.obsidian.concept_threshold)
        fingerprint = self._fingerprint(
            clean_sha=clean_sha,
            outline_sha=outline_sha,
            prompt_sha=prompt_sha,
            concept_threshold=threshold,
        )
        knowledge_path = session_dir / "analysis" / KNOWLEDGE_JSON
        unresolved_path = session_dir / "analysis" / UNRESOLVED_VISUAL_JSON

        if dry_run:
            return KnowledgeOutcome(
                session_id=session_id,
                source_segments=len(segments),
                dry_run=True,
                elapsed_sec=time.monotonic() - started,
                message="CLEANED 与 STRUCTURED 输入有效；计划生成 1 个知识抽取任务",
            )
        if not force and self._valid_outputs(
            knowledge_path, unresolved_path, fingerprint, clean_sha, outline_sha
        ):
            payload = json.loads(knowledge_path.read_text(encoding="utf-8"))
            self.sessions.mark_step(
                meta,
                STEP_KNOWLEDGE,
                "done",
                elapsed_sec=0.0,
                provider=payload.get("provider"),
                model=payload.get("model"),
            )
            return KnowledgeOutcome(
                session_id=session_id,
                source_segments=len(segments),
                reused=True,
                output_json=str(knowledge_path),
                unresolved_visual_json=str(unresolved_path),
                elapsed_sec=time.monotonic() - started,
                message="复用已有 KNOWLEDGE 产物",
            )

        client = self._get_client()
        self.sessions.mark_step(meta, STEP_KNOWLEDGE, "running")
        prompt = render_knowledge_prompt(
            template,
            course_name=meta.course.name,
            concept_threshold=threshold,
            segments=segments,
            outline=outline,
        )
        cache_key = _json_sha({
            "fingerprint": fingerprint,
            "segments": segments,
            "outline_sha256": outline_sha,
        })
        cache_path = session_dir / "analysis" / "knowledge_cache.json"
        exchange_dir = session_dir / "analysis" / "knowledge_web" / "extract"
        try:
            record = self._cached_or_call(
                cache_path=cache_path,
                cache_key=cache_key,
                prompt=prompt,
                segments=segments,
                outline=outline,
                threshold=threshold,
                client=client,
                exchange_dir=exchange_dir,
                request_context={
                    "pipeline": "knowledge",
                    "artifact": f"{KNOWLEDGE_JSON}+{UNRESOLVED_VISUAL_JSON}",
                    "stage": "knowledge",
                    "index": "extract",
                    "session_id": session_id,
                    "course": meta.course.name,
                    "source_layer": "CLEANED+STRUCTURED",
                    "source_sha256": clean_sha,
                    "fingerprint": fingerprint,
                    "schema_version": KNOWLEDGE_SCHEMA_VERSION,
                    "exchange_dir": exchange_dir,
                },
                force=force,
            )
        except WebResponseRequired as exc:
            self.sessions.mark_step(
                meta, STEP_KNOWLEDGE, "pending", provider=client.provider, model=client.model
            )
            return KnowledgeOutcome(
                session_id=session_id,
                source_segments=len(segments),
                partial=True,
                elapsed_sec=time.monotonic() - started,
                message="等待 GPT 网页返回结构化知识 JSON",
                tasks=[{
                    "pipeline": "knowledge",
                    "stage": "knowledge",
                    "index": "extract",
                    "waiting": True,
                    "prompt": str(exchange_dir / "prompt.md"),
                    "response": str(exchange_dir / "response.json"),
                    "message": str(exc),
                }],
            )
        except Exception as exc:
            self.sessions.mark_step(meta, STEP_KNOWLEDGE, "failed", error=str(exc))
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Phase 2C 知识抽取失败：{exc}") from exc

        if sha256_file(clean_path) != clean_sha or sha256_file(outline_path) != outline_sha:
            error = "Phase 2C 输入在执行期间发生变化，拒绝写入知识产物"
            self.sessions.mark_step(meta, STEP_KNOWLEDGE, "failed", error=error)
            raise LLMError(error)
        elapsed = time.monotonic() - started
        common = {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "session_id": session_id,
            "course": cleaned.get("course") or meta.course.name,
            "date": cleaned.get("date") or meta.date,
            "created_at": to_iso(now_local()),
            "provider": record["provider"],
            "model": record["model"],
            "source": {
                "cleaned_file": CLEAN_JSON,
                "cleaned_sha256": clean_sha,
                "outline_file": OUTLINE_JSON,
                "outline_sha256": outline_sha,
                "segment_count": len(segments),
            },
            "extraction": {
                "fingerprint": fingerprint,
                "prompt": "prompts/concept_extraction.md",
                "prompt_sha256": prompt_sha,
                "concept_threshold": threshold,
                "elapsed_sec": round(elapsed, 2),
            },
        }
        knowledge_payload = {
            **common,
            "layer": "KNOWLEDGE",
            "usage": record.get("usage") or {},
            "request": {
                "request_id": record.get("request_id"),
                "cache_hit": bool(record.get("cache_hit")),
                "retries": int(record.get("retries", 0)),
            },
            **record["result"],
        }
        unresolved_items = self._unresolved_visuals(record["result"], segments)
        unresolved_payload = {
            **common,
            "layer": "UNRESOLVED_VISUAL",
            "item_count": len(unresolved_items),
            "items": unresolved_items,
        }
        try:
            atomic_write_text(
                unresolved_path,
                json.dumps(unresolved_payload, ensure_ascii=False, indent=2),
            )
            atomic_write_text(
                knowledge_path,
                json.dumps(knowledge_payload, ensure_ascii=False, indent=2),
            )
        except OSError as exc:
            self.sessions.mark_step(meta, STEP_KNOWLEDGE, "failed", error=str(exc))
            raise LLMError(f"Phase 2C 产物写入失败：{exc}") from exc
        self.sessions.mark_step(
            meta,
            STEP_KNOWLEDGE,
            "done",
            elapsed_sec=elapsed,
            provider=record["provider"],
            model=record["model"],
        )
        return KnowledgeOutcome(
            session_id=session_id,
            source_segments=len(segments),
            output_json=str(knowledge_path),
            unresolved_visual_json=str(unresolved_path),
            elapsed_sec=elapsed,
            message=(
                f"抽取 {sum(len(record['result'][key]) for key in record['result'])} 个知识/审计项，"
                f"其中 {len(unresolved_items)} 个视觉项待 Phase 3"
            ),
        )

    @staticmethod
    def _read_cleaned(path: Path, session_id: str) -> dict[str, Any]:
        if not path.is_file():
            raise LLMError(f"Phase 2C 缺少正式 CLEANED：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"CLEANED 无法读取：{exc}") from exc
        if data.get("layer") != "CLEANED" or data.get("session_id") != session_id:
            raise LLMError("Phase 2C 输入必须是当前 Session 的正式 CLEANED")
        if not isinstance(data.get("clean", {}).get("fingerprint"), str):
            raise LLMError("CLEANED 缺少正式 fingerprint")
        if not isinstance(data.get("segments"), list) or not data["segments"]:
            raise LLMError("CLEANED 没有有效 segments")
        return data

    @staticmethod
    def _normalize_segments(items: list[dict]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        previous_end = -1.0
        for item in items:
            if not isinstance(item, dict):
                raise LLMError("CLEANED segment 必须是 object")
            segment_id = int(item["id"])
            start, end = float(item["start"]), float(item["end"])
            provenance = item.get("provenance")
            if (
                segment_id in seen or end < start or start < previous_end - 0.001
                or not isinstance(provenance, dict)
                or provenance.get("source_segment_id") != segment_id
            ):
                raise LLMError(f"CLEANED segment {segment_id} 拓扑或 provenance 非法")
            seen.add(segment_id)
            previous_end = end
            result.append({
                "id": segment_id,
                "start": start,
                "end": end,
                "text": str(item.get("text") or ""),
                "uncertain": list(item.get("uncertain") or []),
                "visual_references": list(item.get("visual_references") or []),
            })
        return result

    @staticmethod
    def _read_outline(
        path: Path,
        session_id: str,
        clean_sha: str,
        segments: list[dict],
    ) -> dict[str, Any]:
        if not path.is_file():
            raise LLMError(f"Phase 2C 缺少正式 STRUCTURED：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"outline 无法读取：{exc}") from exc
        if data.get("layer") != "STRUCTURED" or data.get("session_id") != session_id:
            raise LLMError("Phase 2C outline 必须属于当前 Session")
        if data.get("source", {}).get("sha256") != clean_sha:
            raise LLMError("outline 引用的 CLEANED SHA 已过期")
        if not isinstance(data.get("structure", {}).get("fingerprint"), str):
            raise LLMError("outline 缺少正式 structure fingerprint")
        response = {field: data.get(field) for field in TOP_LEVEL_FIELDS}
        normalized = validate_outline_response(response, segments)
        return {**data, **normalized}

    def _fingerprint(
        self,
        *,
        clean_sha: str,
        outline_sha: str,
        prompt_sha: str,
        concept_threshold: float,
    ) -> str:
        return _json_sha({
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "cleaned_sha256": clean_sha,
            "outline_sha256": outline_sha,
            "prompt_sha256": prompt_sha,
            "concept_threshold": concept_threshold,
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "response_schema": KNOWLEDGE_RESPONSE_SCHEMA,
        })

    @staticmethod
    def _valid_outputs(
        knowledge_path: Path,
        unresolved_path: Path,
        fingerprint: str,
        clean_sha: str,
        outline_sha: str,
    ) -> bool:
        if not knowledge_path.is_file() or not unresolved_path.is_file():
            return False
        try:
            knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
            unresolved = json.loads(unresolved_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return all(
            value.get("extraction", {}).get("fingerprint") == fingerprint
            and value.get("source", {}).get("cleaned_sha256") == clean_sha
            and value.get("source", {}).get("outline_sha256") == outline_sha
            for value in (knowledge, unresolved)
        )

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
        segments: list[dict],
        outline: dict,
        threshold: float,
        client: LLMClient,
        exchange_dir: Path,
        request_context: dict[str, Any],
        force: bool,
    ) -> dict[str, Any]:
        if cache_path.is_file() and not force:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("cache_key") == cache_key:
                    cached["cache_hit"] = True
                    return cached
            except (OSError, json.JSONDecodeError):
                pass
        last_error: Exception | None = None
        for attempt in range(self.config.clean.max_retries + 1):
            call_started = time.monotonic()
            try:
                response = client.complete(
                    prompt,
                    json_schema=KNOWLEDGE_RESPONSE_SCHEMA,
                    max_tokens=self.config.llm.max_tokens,
                    temperature=self.config.llm.temperature,
                    request_context=request_context,
                )
                parsed = json.loads(response.text)
                result = validate_knowledge_response(
                    parsed,
                    segments,
                    outline,
                    concept_threshold=threshold,
                )
                record = {
                    "schema_version": KNOWLEDGE_SCHEMA_VERSION,
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
                        f"GPT 网页知识结果未通过严格校验；已生成 {exchange_dir / 'retry.md'}"
                    ) from exc
                last_error = exc
                if attempt >= self.config.clean.max_retries:
                    break
                self._sleep(self.config.clean.retry_base_seconds * (2 ** attempt))
        raise LLMError(
            f"Phase 2C 响应在 {self.config.clean.max_retries + 1} 次尝试后失败：{last_error}"
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
                "# GPT 网页 Phase 2C 结果被拒绝",
                "",
                f"校验错误：{error}",
                f"已封存原响应：{rejected.name}",
                "",
                "请重新执行当前 prompt.md，返回完整 JSON。不得补写公式或丢失来源、",
                "uncertainty、visual reference 与 outline 中已经识别的课堂要素。",
            )),
        )
        return True

    @staticmethod
    def _unresolved_visuals(
        result: dict[str, list[dict]], segments: list[dict]
    ) -> list[dict[str, Any]]:
        source = {int(item["id"]): item for item in segments}
        unresolved: list[dict[str, Any]] = []
        for item in result["visual_references"]:
            labels = sorted({
                str(label)
                for segment_id in item["source_segment_ids"]
                for label in source[segment_id].get("visual_references") or []
            })
            unresolved.append({
                **item,
                "status": "unresolved",
                "source_visual_labels": labels,
            })
        return unresolved


def _json_sha(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
