"""Phase 2B：只读 CLEANED，生成带完整来源映射的课堂结构。"""

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
from lecture_ai.llm import LLMClient, build_llm_client
from lecture_ai.session import SessionManager
from lecture_ai.structure.models import StructureOutcome
from lecture_ai.structure.prompting import load_structure_prompt, render_structure_prompt
from lecture_ai.structure.schema import OUTLINE_RESPONSE_SCHEMA, validate_outline_response
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text
from lecture_ai.utils.timefmt import now_local, to_iso

OUTLINE_JSON = "outline.json"
STRUCTURE_SCHEMA_VERSION = 1
STEP_STRUCTURE = "structure"


class StructurePipeline:
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
    ) -> StructureOutcome:
        started = time.monotonic()
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)
        source_path = session_dir / "analysis" / CLEAN_JSON
        source = self._read_cleaned(source_path, session_id)
        segments = self._normalize_segments(source["segments"])
        source_sha = sha256_file(source_path)
        prompt_path, template = load_structure_prompt(self.config.paths.project_root)
        prompt_sha = sha256_file(prompt_path)
        fingerprint = self._fingerprint(source_sha=source_sha, prompt_sha=prompt_sha)
        output_path = session_dir / "analysis" / OUTLINE_JSON

        if dry_run:
            return StructureOutcome(
                session_id=session_id,
                source_segments=len(segments),
                dry_run=True,
                elapsed_sec=time.monotonic() - started,
                message="CLEANED 输入有效；计划生成 1 个全课结构任务",
            )
        if not force and self._valid_output(output_path, fingerprint, source_sha):
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.sessions.mark_step(
                meta,
                STEP_STRUCTURE,
                "done",
                elapsed_sec=0.0,
                provider=payload.get("provider"),
                model=payload.get("model"),
            )
            return StructureOutcome(
                session_id=session_id,
                source_segments=len(segments),
                reused=True,
                output_json=str(output_path),
                elapsed_sec=time.monotonic() - started,
                message="复用已有 STRUCTURED 产物",
            )

        client = self._get_client()
        self.sessions.mark_step(meta, STEP_STRUCTURE, "running")
        prompt = render_structure_prompt(
            template,
            course_name=meta.course.name,
            segments=segments,
        )
        cache_key = self._cache_key(fingerprint, segments)
        cache_path = session_dir / "analysis" / "structure_cache.json"
        exchange_dir = session_dir / "analysis" / "structure_web" / "outline"
        try:
            record = self._cached_or_call(
                cache_path=cache_path,
                cache_key=cache_key,
                prompt=prompt,
                segments=segments,
                client=client,
                exchange_dir=exchange_dir,
                request_context={
                    "pipeline": "structure",
                    "artifact": OUTLINE_JSON,
                    "stage": "structure",
                    "index": "outline",
                    "session_id": session_id,
                    "course": meta.course.name,
                    "source_layer": "CLEANED",
                    "source_sha256": source_sha,
                    "fingerprint": fingerprint,
                    "schema_version": STRUCTURE_SCHEMA_VERSION,
                    "exchange_dir": exchange_dir,
                },
                force=force,
            )
        except WebResponseRequired as exc:
            self.sessions.mark_step(
                meta, STEP_STRUCTURE, "pending", provider=client.provider, model=client.model
            )
            return StructureOutcome(
                session_id=session_id,
                source_segments=len(segments),
                partial=True,
                elapsed_sec=time.monotonic() - started,
                message="等待 GPT 网页返回课堂结构 JSON",
                tasks=[{
                    "pipeline": "structure",
                    "stage": "structure",
                    "index": "outline",
                    "waiting": True,
                    "prompt": str(exchange_dir / "prompt.md"),
                    "response": str(exchange_dir / "response.json"),
                    "message": str(exc),
                }],
            )
        except Exception as exc:
            self.sessions.mark_step(meta, STEP_STRUCTURE, "failed", error=str(exc))
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Phase 2B 结构识别失败：{exc}") from exc

        elapsed = time.monotonic() - started
        if sha256_file(source_path) != source_sha:
            error = "CLEANED 在 Phase 2B 执行期间发生变化，拒绝写入 outline"
            self.sessions.mark_step(meta, STEP_STRUCTURE, "failed", error=error)
            raise LLMError(error)
        payload = {
            "schema_version": STRUCTURE_SCHEMA_VERSION,
            "layer": "STRUCTURED",
            "session_id": session_id,
            "course": source.get("course") or meta.course.name,
            "date": source.get("date") or meta.date,
            "created_at": to_iso(now_local()),
            "provider": record["provider"],
            "model": record["model"],
            "source": {
                "layer": "CLEANED",
                "file": CLEAN_JSON,
                "sha256": source_sha,
                "segment_count": len(segments),
            },
            "structure": {
                "fingerprint": fingerprint,
                "prompt": "prompts/chapter_detection.md",
                "prompt_sha256": prompt_sha,
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
            atomic_write_text(output_path, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError as exc:
            self.sessions.mark_step(meta, STEP_STRUCTURE, "failed", error=str(exc))
            raise LLMError(f"outline.json 写入失败：{exc}") from exc
        self.sessions.mark_step(
            meta,
            STEP_STRUCTURE,
            "done",
            elapsed_sec=elapsed,
            provider=record["provider"],
            model=record["model"],
        )
        return StructureOutcome(
            session_id=session_id,
            source_segments=len(segments),
            output_json=str(output_path),
            elapsed_sec=elapsed,
            message=(
                f"识别 {len(record['result']['lecture_topics'])} 个章节，"
                f"覆盖 {len(segments)} 个 CLEANED segments"
            ),
        )

    @staticmethod
    def _read_cleaned(path: Path, session_id: str) -> dict[str, Any]:
        if not path.is_file():
            raise LLMError(
                f"Phase 2B 只接受正式 CLEANED；缺少 {path}，不得回退 RAW/REPAIRED"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"CLEANED 无法读取：{exc}") from exc
        if data.get("layer") != "CLEANED":
            raise LLMError("Phase 2B 输入 layer 必须是 CLEANED")
        if data.get("session_id") != session_id:
            raise LLMError("CLEANED session_id 与目标 Session 不匹配")
        if not isinstance(data.get("segments"), list) or not data["segments"]:
            raise LLMError("CLEANED 没有有效 segments")
        if not isinstance(data.get("clean"), dict) or not isinstance(
            data["clean"].get("fingerprint"), str
        ):
            raise LLMError("CLEANED 缺少正式 clean fingerprint")
        if not isinstance(data.get("source"), dict) or not isinstance(
            data["source"].get("sha256"), str
        ):
            raise LLMError("CLEANED 缺少上游 source SHA")
        return data

    @staticmethod
    def _normalize_segments(items: list[dict]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        previous_end = -1.0
        seen: set[int] = set()
        for item in items:
            if not isinstance(item, dict):
                raise LLMError("CLEANED segment 必须是 object")
            segment_id = int(item["id"])
            start, end = float(item["start"]), float(item["end"])
            provenance = item.get("provenance")
            if not isinstance(provenance, dict) or provenance.get(
                "source_segment_id"
            ) != segment_id:
                raise LLMError(f"CLEANED segment {segment_id} 缺少有效 provenance")
            if segment_id in seen:
                raise LLMError(f"CLEANED 包含重复 segment id：{segment_id}")
            if start < previous_end - 0.001 or end < start:
                raise LLMError(f"CLEANED segment {segment_id} 时间轴不单调")
            seen.add(segment_id)
            previous_end = end
            normalized.append({
                "id": segment_id,
                "start": start,
                "end": end,
                "text": str(item.get("text") or ""),
                "uncertain": list(item.get("uncertain") or []),
                "visual_references": list(item.get("visual_references") or []),
            })
        return normalized

    def _fingerprint(self, *, source_sha: str, prompt_sha: str) -> str:
        data = {
            "schema_version": STRUCTURE_SCHEMA_VERSION,
            "source_layer": "CLEANED",
            "source_sha256": source_sha,
            "prompt_sha256": prompt_sha,
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "response_schema": OUTLINE_RESPONSE_SCHEMA,
        }
        return _json_sha(data)

    @staticmethod
    def _cache_key(fingerprint: str, segments: list[dict]) -> str:
        return _json_sha({"fingerprint": fingerprint, "segments": segments})

    @staticmethod
    def _valid_output(path: Path, fingerprint: str, source_sha: str) -> bool:
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            data.get("layer") == "STRUCTURED"
            and data.get("source", {}).get("sha256") == source_sha
            and data.get("structure", {}).get("fingerprint") == fingerprint
            and isinstance(data.get("lecture_topics"), list)
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
                    json_schema=OUTLINE_RESPONSE_SCHEMA,
                    max_tokens=self.config.llm.max_tokens,
                    temperature=self.config.llm.temperature,
                    request_context=request_context,
                )
                parsed = json.loads(response.text)
                result = validate_outline_response(parsed, segments)
                record = {
                    "schema_version": STRUCTURE_SCHEMA_VERSION,
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
                        f"GPT 网页结构结果未通过严格校验；已生成 {exchange_dir / 'retry.md'}"
                    ) from exc
                last_error = exc
                if attempt >= self.config.clean.max_retries:
                    break
                self._sleep(self.config.clean.retry_base_seconds * (2 ** attempt))
        raise LLMError(
            f"Phase 2B 结构化响应在 {self.config.clean.max_retries + 1} 次尝试后失败："
            f"{last_error}"
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
                "# GPT 网页 Phase 2B 结果被拒绝",
                "",
                f"校验错误：{error}",
                f"已封存原响应：{rejected.name}",
                "",
                "请重新执行当前 prompt.md，并返回完整 JSON。不要改动来源 segment id、",
                "不要补写课堂知识，也不要省略 lecture_topics 的完整覆盖。",
            )),
        )
        return True


def _json_sha(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
