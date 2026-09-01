"""Phase 2A：分块忠实清洗、边界协调、缓存、重试与 provenance。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from lecture_ai.cleaning.chunking import build_chunk_plan
from lecture_ai.cleaning.models import ChunkPlan, CleanOutcome
from lecture_ai.cleaning.prompting import load_clean_prompt, render_clean_prompt
from lecture_ai.cleaning.schema import CLEAN_RESPONSE_SCHEMA, validate_clean_response
from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LLMError
from lecture_ai.llm import LLMClient, build_llm_client
from lecture_ai.repair import REPAIRED_JSON
from lecture_ai.session import SessionManager, load_courses
from lecture_ai.transcription import load_glossary
from lecture_ai.transcription.writer import TRANSCRIPT_JSON
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text
from lecture_ai.utils.timefmt import hhmmss, now_local, to_iso

CLEAN_JSON = "transcript_clean.json"
CLEAN_MD = "transcript_clean.md"
CLEAN_SCHEMA_VERSION = 1
STEP_CLEAN = "clean"


class CleanPipeline:
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
        self.courses = load_courses(config.courses_path, config.course.default_course_key)
        self._client = client
        self._sleep = sleep

    def run(
        self,
        session_id: str,
        *,
        dry_run: bool = False,
        chunk: int | None = None,
        force: bool = False,
    ) -> CleanOutcome:
        started = time.monotonic()
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)
        source_path, source_layer = self._select_source(session_dir)
        source_sha = sha256_file(source_path)
        source = self._read_source(source_path)
        segments = self._normalize_segments(source["segments"])
        duration = float(source.get("duration_sec") or meta.audio.duration_sec or 0.0)

        prompt_path, template = load_clean_prompt(self.config.paths.project_root)
        prompt_sha = sha256_file(prompt_path)
        course = self.courses.get(meta.course.key)
        glossary = load_glossary(
            self.config.glossary_dir, course.glossary, include_common=True
        )
        plans = build_chunk_plan(
            segments,
            duration_sec=duration,
            chunk_minutes=self.config.clean.chunk_minutes,
            overlap_seconds=self.config.clean.overlap_seconds,
        )
        if chunk is not None and chunk not in {plan.index for plan in plans}:
            raise LLMError(
                f"--chunk {chunk} 不存在；可选：{', '.join(str(p.index) for p in plans)}"
            )
        fingerprint = self._fingerprint(
            source_sha=source_sha,
            source_layer=source_layer,
            prompt_sha=prompt_sha,
            glossary_terms=glossary.terms,
        )
        output_json = session_dir / "analysis" / CLEAN_JSON
        output_md = session_dir / "analysis" / CLEAN_MD

        if dry_run:
            selected = [plan for plan in plans if chunk is None or plan.index == chunk]
            return CleanOutcome(
                session_id=session_id,
                source_layer=source_layer,
                chunks_planned=len(plans),
                dry_run=True,
                partial=chunk is not None,
                elapsed_sec=time.monotonic() - started,
                message=f"计划处理 {len(selected)}/{len(plans)} 个清洗块",
                chunks=[plan.to_dict() for plan in selected],
            )

        if chunk is None and not force and self._valid_output(
            output_json, fingerprint, source_sha
        ):
            data = json.loads(output_json.read_text(encoding="utf-8"))
            self.sessions.mark_step(
                meta,
                STEP_CLEAN,
                "done",
                elapsed_sec=0.0,
                provider=data.get("provider"),
                model=data.get("model"),
            )
            return CleanOutcome(
                session_id=session_id,
                source_layer=source_layer,
                chunks_planned=len(plans),
                chunks_processed=len(data.get("chunks") or []),
                boundaries_processed=len(data.get("boundaries") or []),
                reused=True,
                output_json=str(output_json),
                output_md=str(output_md),
                elapsed_sec=time.monotonic() - started,
                message="复用已有清洗产物",
            )

        if chunk is None:
            self.sessions.mark_step(meta, STEP_CLEAN, "running")
        try:
            client = self._get_client()
            selected_plans = [plan for plan in plans if chunk is None or plan.index == chunk]
            chunk_records = [
                self._clean_chunk(
                    plan,
                    segments,
                    template,
                    meta.course.name,
                    glossary.terms,
                    fingerprint,
                    session_dir,
                    client,
                    force,
                )
                for plan in selected_plans
            ]
            if chunk is not None:
                return CleanOutcome(
                    session_id=session_id,
                    source_layer=source_layer,
                    chunks_planned=len(plans),
                    chunks_processed=1,
                    partial=True,
                    elapsed_sec=time.monotonic() - started,
                    message=f"已生成 chunk {chunk} 缓存；未组装最终 CLEANED 产物",
                    chunks=[self._record_audit(record) for record in chunk_records],
                )

            boundaries = self._reconcile_boundaries(
                chunk_records,
                segments,
                template,
                meta.course.name,
                glossary.terms,
                fingerprint,
                session_dir,
                client,
                force,
            )
            cleaned = self._assemble(
                segments,
                chunk_records,
                boundaries,
                source_layer=source_layer,
                source_sha=source_sha,
                source_file=source_path.name,
            )
            calls = chunk_records + boundaries
            elapsed = time.monotonic() - started
            payload = self._result_payload(
                source=source,
                source_path=source_path,
                source_layer=source_layer,
                source_sha=source_sha,
                prompt_sha=prompt_sha,
                fingerprint=fingerprint,
                glossary_sources=glossary.sources,
                cleaned=cleaned,
                chunks=chunk_records,
                boundaries=boundaries,
                elapsed_sec=elapsed,
                client=client,
            )
            atomic_write_text(output_json, json.dumps(payload, ensure_ascii=False, indent=2))
            atomic_write_text(output_md, render_clean_markdown(payload))
            self.sessions.mark_step(
                meta,
                STEP_CLEAN,
                "done",
                elapsed_sec=elapsed,
                provider=payload["provider"],
                model=payload["model"],
            )
            return CleanOutcome(
                session_id=session_id,
                source_layer=source_layer,
                chunks_planned=len(plans),
                chunks_processed=len(chunk_records),
                boundaries_processed=len(boundaries),
                output_json=str(output_json),
                output_md=str(output_md),
                elapsed_sec=elapsed,
                message=(
                    f"清洗 {len(chunk_records)} 块，协调 {len(boundaries)} 个边界，"
                    f"共 {len(cleaned)} segments"
                ),
                chunks=[self._record_audit(record) for record in calls],
            )
        except Exception as exc:
            if chunk is None:
                self.sessions.mark_step(meta, STEP_CLEAN, "failed", error=str(exc))
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Phase 2A 清洗失败：{exc}") from exc

    @staticmethod
    def _select_source(session_dir: Path) -> tuple[Path, str]:
        repaired = session_dir / "transcript" / REPAIRED_JSON
        if repaired.exists():
            return repaired, "REPAIRED"
        raw = session_dir / "transcript" / TRANSCRIPT_JSON
        if raw.exists():
            return raw, "RAW"
        raise LLMError("找不到 REPAIRED 或 RAW 转录，无法清洗")

    @staticmethod
    def _read_source(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"清洗输入无法读取：{exc}") from exc
        if not isinstance(data.get("segments"), list) or not data["segments"]:
            raise LLMError("清洗输入没有有效 segments")
        return data

    @staticmethod
    def _normalize_segments(items: list[dict]) -> list[dict]:
        normalized = []
        for index, item in enumerate(items):
            normalized.append(
                {
                    "id": int(item.get("id", index)),
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                    "text": str(item.get("text") or "").strip(),
                    "uncertain": list(item.get("uncertain") or []),
                    "visual_references": list(item.get("visual_references") or []),
                    "no_speech_prob": item.get("no_speech_prob"),
                    "avg_logprob": item.get("avg_logprob"),
                }
            )
        ids = [item["id"] for item in normalized]
        if len(ids) != len(set(ids)):
            raise LLMError("清洗输入包含重复 segment id")
        return normalized

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = build_llm_client(self.config)
        return self._client

    def _fingerprint(
        self,
        *,
        source_sha: str,
        source_layer: str,
        prompt_sha: str,
        glossary_terms: list[str],
    ) -> str:
        data = {
            "schema_version": CLEAN_SCHEMA_VERSION,
            "source_sha256": source_sha,
            "source_layer": source_layer,
            "prompt_sha256": prompt_sha,
            "glossary_terms": glossary_terms,
            "clean": asdict(self.config.clean),
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "response_schema": CLEAN_RESPONSE_SCHEMA,
        }
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_output(path: Path, fingerprint: str, source_sha: str) -> bool:
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            data.get("clean", {}).get("fingerprint") == fingerprint
            and data.get("source", {}).get("sha256") == source_sha
            and isinstance(data.get("segments"), list)
        )

    def _clean_chunk(
        self,
        plan: ChunkPlan,
        segments: list[dict],
        template: str,
        course_name: str,
        glossary: list[str],
        fingerprint: str,
        session_dir: Path,
        client: LLMClient,
        force: bool,
    ) -> dict[str, Any]:
        lookup = {item["id"]: item for item in segments}
        inputs = [lookup[segment_id] for segment_id in plan.segment_ids]
        prompt = render_clean_prompt(
            template,
            mode="clean_chunk",
            course_name=course_name,
            glossary=glossary,
            segments=inputs,
        )
        key = self._call_key(fingerprint, "chunk", plan.index, inputs)
        path = session_dir / "analysis" / "clean_cache" / f"chunk_{plan.index:03d}.json"
        record = self._cached_or_call(path, key, prompt, inputs, client, force)
        record["stage"] = "chunk"
        record["index"] = plan.index
        record["plan"] = plan.to_dict()
        return record

    def _reconcile_boundaries(
        self,
        chunks: list[dict[str, Any]],
        source: list[dict],
        template: str,
        course_name: str,
        glossary: list[str],
        fingerprint: str,
        session_dir: Path,
        client: LLMClient,
        force: bool,
    ) -> list[dict[str, Any]]:
        source_lookup = {item["id"]: item for item in source}
        records: list[dict[str, Any]] = []
        for left, right in zip(chunks, chunks[1:]):
            left_map = {item["id"]: item for item in left["result"]}
            right_map = {item["id"]: item for item in right["result"]}
            shared = [segment_id for segment_id in left_map if segment_id in right_map]
            if not shared:
                continue
            inputs = []
            for segment_id in shared:
                original = source_lookup[segment_id]
                inputs.append(
                    {
                        "id": segment_id,
                        "start": original["start"],
                        "end": original["end"],
                        "text": left_map[segment_id]["text"],
                        "left_text": left_map[segment_id]["text"],
                        "right_text": right_map[segment_id]["text"],
                        "uncertain": sorted(set(
                            left_map[segment_id]["uncertain"]
                            + right_map[segment_id]["uncertain"]
                        )),
                        "visual_references": sorted(set(
                            left_map[segment_id]["visual_references"]
                            + right_map[segment_id]["visual_references"]
                        )),
                    }
                )
            left_index, right_index = int(left["index"]), int(right["index"])
            prompt = render_clean_prompt(
                template,
                mode="reconcile_boundary",
                course_name=course_name,
                glossary=glossary,
                segments=inputs,
                boundary_context=f"chunk {left_index} 与 chunk {right_index} 的重叠区",
            )
            key = self._call_key(
                fingerprint, "boundary", left_index * 10000 + right_index, inputs
            )
            path = (
                session_dir / "analysis" / "clean_cache"
                / f"boundary_{left_index:03d}_{right_index:03d}.json"
            )
            record = self._cached_or_call(path, key, prompt, inputs, client, force)
            record["stage"] = "boundary"
            record["index"] = f"{left_index}-{right_index}"
            record["left_chunk"] = left_index
            record["right_chunk"] = right_index
            record["segment_ids"] = shared
            records.append(record)
        return records

    def _cached_or_call(
        self,
        path: Path,
        key: str,
        prompt: str,
        expected: list[dict],
        client: LLMClient,
        force: bool,
    ) -> dict[str, Any]:
        if path.exists() and not force:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("cache_key") == key:
                    cached["cache_hit"] = True
                    return cached
            except (OSError, json.JSONDecodeError):
                pass

        last_error: Exception | None = None
        for attempt in range(self.config.clean.max_retries + 1):
            try:
                response = client.complete(
                    prompt,
                    json_schema=CLEAN_RESPONSE_SCHEMA,
                    max_tokens=self.config.llm.max_tokens,
                    temperature=self.config.llm.temperature,
                )
                parsed = json.loads(response.text)
                result = validate_clean_response(parsed, expected, self.config.clean)
                record = {
                    "schema_version": CLEAN_SCHEMA_VERSION,
                    "cache_key": key,
                    "cache_hit": False,
                    "provider": response.provider,
                    "model": response.model,
                    "request_id": response.request_id,
                    "usage": response.usage,
                    "retries": attempt,
                    "created_at": to_iso(now_local()),
                    "result": result,
                }
                atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2))
                return record
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.clean.max_retries:
                    break
                self._sleep(self.config.clean.retry_base_seconds * (2 ** attempt))
        raise LLMError(
            f"LLM 结构化清洗在 {self.config.clean.max_retries + 1} 次尝试后失败："
            f"{last_error}"
        ) from last_error

    @staticmethod
    def _call_key(
        fingerprint: str, stage: str, index: int, inputs: list[dict]
    ) -> str:
        data = {"fingerprint": fingerprint, "stage": stage, "index": index, "input": inputs}
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _assemble(
        source: list[dict],
        chunks: list[dict[str, Any]],
        boundaries: list[dict[str, Any]],
        *,
        source_layer: str,
        source_sha: str,
        source_file: str,
    ) -> list[dict[str, Any]]:
        versions: dict[int, list[tuple[ChunkPlan, dict]]] = {}
        for record in chunks:
            plan_data = record["plan"]
            plan = ChunkPlan(
                index=int(plan_data["index"]),
                core_start=float(plan_data["core_start"]),
                core_end=float(plan_data["core_end"]),
                window_start=float(plan_data["window_start"]),
                window_end=float(plan_data["window_end"]),
                segment_ids=tuple(plan_data["segment_ids"]),
            )
            for item in record["result"]:
                versions.setdefault(int(item["id"]), []).append((plan, item))
        boundary_values = {
            int(item["id"]): item
            for record in boundaries
            for item in record["result"]
        }

        cleaned: list[dict[str, Any]] = []
        for item in source:
            segment_id = int(item["id"])
            candidates = versions.get(segment_id)
            if not candidates:
                raise LLMError(f"组装时缺少 segment {segment_id} 的清洗结果")
            midpoint = (float(item["start"]) + float(item["end"])) / 2
            selected_plan, selected = next(
                (
                    pair for pair in candidates
                    if pair[0].core_start <= midpoint < pair[0].core_end
                ),
                candidates[0],
            )
            boundary = boundary_values.get(segment_id)
            final = boundary or selected
            cleaned.append(
                {
                    "id": segment_id,
                    "start": item["start"],
                    "end": item["end"],
                    "text": final["text"],
                    "uncertain": sorted(set(
                        list(item.get("uncertain") or []) + final["uncertain"]
                    )),
                    "visual_references": sorted(set(
                        list(item.get("visual_references") or [])
                        + final["visual_references"]
                    )),
                    "provenance": {
                        "source_layer": source_layer,
                        "source_file": source_file,
                        "source_sha256": source_sha,
                        "source_segment_id": segment_id,
                        "chunk_ids": sorted(plan.index for plan, _ in candidates),
                        "primary_chunk_id": selected_plan.index,
                        "boundary_reconciled": boundary is not None,
                        "source_no_speech_prob": item.get("no_speech_prob"),
                        "source_avg_logprob": item.get("avg_logprob"),
                    },
                }
            )
        return cleaned

    @staticmethod
    def _record_audit(record: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "stage", "index", "cache_hit", "provider", "model", "request_id",
            "usage", "retries", "plan", "left_chunk", "right_chunk", "segment_ids",
        )
        return {key: record[key] for key in keys if key in record}

    def _result_payload(
        self,
        *,
        source: dict[str, Any],
        source_path: Path,
        source_layer: str,
        source_sha: str,
        prompt_sha: str,
        fingerprint: str,
        glossary_sources: list[str],
        cleaned: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        boundaries: list[dict[str, Any]],
        elapsed_sec: float,
        client: LLMClient,
    ) -> dict[str, Any]:
        calls = chunks + boundaries
        actual = [record for record in calls if not record.get("cache_hit")]
        usage = {
            name: sum(int(record.get("usage", {}).get(name, 0) or 0) for record in actual)
            for name in ("input_tokens", "output_tokens", "total_tokens")
        }
        usage["api_calls"] = len(actual)
        usage["cache_hits"] = len(calls) - len(actual)
        provider = next((record.get("provider") for record in calls if record.get("provider")), client.provider)
        model = next((record.get("model") for record in calls if record.get("model")), client.model)
        return {
            "schema_version": CLEAN_SCHEMA_VERSION,
            "layer": "CLEANED",
            "session_id": source.get("session_id"),
            "course": source.get("course"),
            "date": source.get("date"),
            "audio_start": source.get("audio_start"),
            "duration_sec": source.get("duration_sec"),
            "created_at": to_iso(now_local()),
            "provider": provider,
            "model": model,
            "source": {
                "layer": source_layer,
                "file": source_path.name,
                "sha256": source_sha,
            },
            "clean": {
                "fingerprint": fingerprint,
                "prompt": "prompts/transcript_clean.md",
                "prompt_sha256": prompt_sha,
                "config": asdict(self.config.clean),
                "glossary_sources": glossary_sources,
                "elapsed_sec": round(elapsed_sec, 2),
            },
            "usage": usage,
            "chunks": [self._record_audit(record) for record in chunks],
            "boundaries": [self._record_audit(record) for record in boundaries],
            "segment_count": len(cleaned),
            "segments": cleaned,
        }


def render_clean_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "---",
        f"session_id: {payload.get('session_id', '')}",
        f"course: {payload.get('course', '')}",
        f"date: {payload.get('date', '')}",
        "type: transcript_clean",
        f"source_layer: {payload['source']['layer']}",
        f"clean_fingerprint: {payload['clean']['fingerprint']}",
        "---",
        "",
        f"# 忠实清洗转录 · {payload.get('course') or payload.get('session_id')}",
        "",
        "> 本文件只做 ASR 纠错、标点与断句，不是课堂摘要。",
        "",
    ]
    for segment in payload["segments"]:
        suffix = ""
        if segment["uncertain"]:
            suffix = "  ⚠ " + "；".join(segment["uncertain"])
        lines.extend((f"`[{hhmmss(float(segment['start']))}]` {segment['text']}{suffix}", ""))
    return "\n".join(lines)
