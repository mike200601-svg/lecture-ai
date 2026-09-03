"""GPT 网页清洗的批量作业包与无人值守收件箱。

程序只自动化本地可控的部分：生成整包、监听返回 ZIP、严格校验、缓存和续跑。
ChatGPT 网页本身仍由用户上传/下载，避免依赖浏览器登录状态或非公开接口。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from lecture_ai.cleaning.pipeline import CleanPipeline
from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LLMError
from lecture_ai.knowledge.pipeline import KnowledgePipeline
from lecture_ai.logging_setup import get_logger
from lecture_ai.session import SessionManager
from lecture_ai.structure.pipeline import StructurePipeline
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text, ensure_dir, unique_path
from lecture_ai.utils.timefmt import now_local, to_iso

BATCH_SCHEMA_VERSION = 1
BATCH_ROOT = "clean_web_batch"
STATE_FILE = "state.json"

log = get_logger(__name__)


@dataclass
class WebBatchOutcome:
    session_id: str
    status: str
    message: str
    task_ids: list[str] = field(default_factory=list)
    package_dir: str | None = None
    package_zip: str | None = None
    accepted: int = 0
    rejected: int = 0
    output_json: str | None = None
    output_md: str | None = None


class CleanWebBatchService:
    """把逐块网页交换封装成可长期运行的 outbox/inbox 协议。"""

    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        *,
        pipeline: CleanPipeline | None = None,
    ) -> None:
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)
        self.pipeline = pipeline or CleanPipeline(config, self.db)

    def prepare(self, session_id: str) -> WebBatchOutcome:
        """续跑清洗并把当前所有待网页处理项合并成一个幂等作业包。"""
        outcome = self.pipeline.run(session_id)
        if not outcome.partial:
            result = WebBatchOutcome(
                session_id=session_id,
                status="ready_for_phase2a_qa",
                message="全部网页任务已通过，正式 CLEANED 已组装，等待 Phase 2A QA",
                output_json=outcome.output_json,
                output_md=outcome.output_md,
            )
            self._write_state(result)
            return result

        waiting = [item for item in outcome.chunks if item.get("waiting")]
        if not waiting:
            raise LLMError("清洗仍为 partial，但没有可打包的网页任务")
        return self._package_waiting(session_id, waiting)

    def prepare_structure(self, session_id: str) -> WebBatchOutcome:
        """续跑 Phase 2B，并把待返回的 outline 任务送入同一手机交换区。"""
        outcome = StructurePipeline(self.config, self.db).run(session_id)
        if not outcome.partial:
            result = WebBatchOutcome(
                session_id=session_id,
                status="ready_for_phase2b_qa",
                message="课堂结构已生成，等待 Phase 2B QA",
                output_json=outcome.output_json,
            )
            self._write_state(result)
            return result
        waiting = [item for item in outcome.tasks if item.get("waiting")]
        if not waiting:
            raise LLMError("结构识别仍为 partial，但没有可打包的网页任务")
        return self._package_waiting(session_id, waiting)

    def prepare_knowledge(self, session_id: str) -> WebBatchOutcome:
        """续跑 Phase 2C，并把待返回的知识抽取任务送入同一手机交换区。"""
        outcome = KnowledgePipeline(self.config, self.db).run(session_id)
        if not outcome.partial:
            result = WebBatchOutcome(
                session_id=session_id,
                status="ready_for_phase2c_qa",
                message="可追溯知识与视觉疑点队列已生成，等待 Phase 2C QA",
                output_json=outcome.output_json,
            )
            self._write_state(result)
            return result
        waiting = [item for item in outcome.tasks if item.get("waiting")]
        if not waiting:
            raise LLMError("知识抽取仍为 partial，但没有可打包的网页任务")
        return self._package_waiting(session_id, waiting)

    def _package_waiting(
        self, session_id: str, waiting: list[dict[str, Any]]
    ) -> WebBatchOutcome:
        tasks = [self._task_manifest(item) for item in waiting]
        immutable = {
            "schema_version": BATCH_SCHEMA_VERSION,
            "session_id": session_id,
            "tasks": tasks,
        }
        batch_id = "web-" + _json_sha(immutable)[:16]
        root = self._root(session_id)
        package_dir = root / "outbox" / batch_id
        shared_root = self._shared_root(session_id)
        package_zip = shared_root / "to_phone" / f"{batch_id}.zip"
        existing_manifest = package_dir / "manifest.json"
        if existing_manifest.is_file() and package_zip.is_file():
            existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
            if all(existing.get(key) == value for key, value in immutable.items()):
                result = WebBatchOutcome(
                    session_id=session_id,
                    status="waiting_for_web",
                    message=f"{len(tasks)} 个待处理项仍在等待手机/GPT 返回",
                    task_ids=[str(task["task_id"]) for task in tasks],
                    package_dir=str(package_dir),
                    package_zip=str(package_zip),
                )
                self._write_state(result)
                return result
        for previous in (shared_root / "to_phone").glob("*.zip"):
            if previous.name == package_zip.name:
                continue
            archived = unique_path(ensure_dir(root / "superseded") / previous.name)
            shutil.move(str(previous), str(archived))
        ensure_dir(package_dir / "tasks")
        ensure_dir(package_dir / "responses")

        for task, waiting_item in zip(tasks, waiting):
            source_dir = Path(str(waiting_item["prompt"])).parent
            target_dir = package_dir / "tasks" / task["task_id"]
            ensure_dir(target_dir)
            for name in ("prompt.md", "schema.json", "request.json"):
                shutil.copy2(source_dir / name, target_dir / name)

        manifest = {
            **immutable,
            "batch_id": batch_id,
            "created_at": to_iso(now_local()),
            "return_filename": f"{batch_id}_responses.zip",
        }
        atomic_write_text(
            package_dir / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        atomic_write_text(package_dir / "README.md", _render_readme(manifest))
        atomic_write_text(
            package_dir / "responses" / "PUT_RESULTS_HERE.txt",
            "GPT 应把每项结果保存为 manifest.json 指定的 response_file。\n",
        )
        _write_zip(package_dir, package_zip)
        ensure_dir(shared_root / "from_phone")

        result = WebBatchOutcome(
            session_id=session_id,
            status="waiting_for_web",
            message=f"已自动打包 {len(tasks)} 个待处理项，正在监听 inbox",
            task_ids=[str(task["task_id"]) for task in tasks],
            package_dir=str(package_dir),
            package_zip=str(package_zip),
        )
        self._write_state(result)
        return result

    def receive(self, session_id: str, returned_package: Path) -> WebBatchOutcome:
        """验签返回包、投递各响应，然后调用统一 pipeline 校验并自动续跑。"""
        package = Path(returned_package)
        reader = _PackageReader(package)
        returned_manifest = reader.read_json("manifest.json")
        batch_id = str(returned_manifest.get("batch_id") or "")
        if not batch_id:
            raise LLMError("返回包 manifest.json 缺少 batch_id")
        local_manifest_path = self._root(session_id) / "outbox" / batch_id / "manifest.json"
        if not local_manifest_path.is_file():
            raise LLMError(f"找不到本机原始批次：{batch_id}")
        local_manifest = json.loads(local_manifest_path.read_text(encoding="utf-8"))
        self._validate_return_manifest(session_id, returned_manifest, local_manifest)

        staged = 0
        for task in local_manifest["tasks"]:
            task_id = str(task["task_id"])
            exchange_dir = self._exchange_dir(session_id, task)
            self._validate_current_request(exchange_dir, task)
            cache_path = self._cache_path(session_id, task)
            if cache_path.exists():
                continue
            response_text = reader.read_text(str(task["response_file"])).strip()
            if not response_text:
                raise LLMError(f"返回结果为空：{task['response_file']}")
            response_path = exchange_dir / "response.json"
            if response_path.exists():
                current = response_path.read_text(encoding="utf-8").strip()
                if current != response_text:
                    raise LLMError(f"{task_id} 已有不同的待校验响应，拒绝覆盖")
            else:
                atomic_write_text(response_path, response_text)
            staged += 1

        before = {
            str(task["task_id"]): self._cache_path(session_id, task).exists()
            for task in local_manifest["tasks"]
        }
        pipelines = {str(task.get("pipeline") or "clean") for task in local_manifest["tasks"]}
        if len(pipelines) != 1:
            raise LLMError("一个网页返回包不能混合多个 pipeline")
        pipeline_name = next(iter(pipelines))
        if pipeline_name == "clean":
            outcome = self.pipeline.run(session_id)
        elif pipeline_name == "structure":
            outcome = StructurePipeline(self.config, self.db).run(session_id)
        elif pipeline_name == "knowledge":
            outcome = KnowledgePipeline(self.config, self.db).run(session_id)
        else:
            raise LLMError(f"返回包包含未知 pipeline：{pipeline_name}")
        accepted = sum(
            not before[str(task["task_id"])]
            and self._cache_path(session_id, task).exists()
            for task in local_manifest["tasks"]
        )
        rejected = max(0, staged - accepted)

        if not outcome.partial:
            status = {
                "clean": "ready_for_phase2a_qa",
                "structure": "ready_for_phase2b_qa",
                "knowledge": "ready_for_phase2c_qa",
            }[pipeline_name]
            message = {
                "clean": "返回包全部处理完成；正式 CLEANED 已组装，等待 Phase 2A QA",
                "structure": "返回包全部处理完成；课堂结构已生成，等待 Phase 2B QA",
                "knowledge": "返回包全部处理完成；知识与视觉疑点队列已生成，等待 Phase 2C QA",
            }[pipeline_name]
            result = WebBatchOutcome(
                session_id=session_id,
                status=status,
                message=message,
                accepted=accepted,
                rejected=rejected,
                output_json=outcome.output_json,
                output_md=getattr(outcome, "output_md", None),
            )
            self._write_state(result)
            return result

        followup = {
            "clean": self.prepare,
            "structure": self.prepare_structure,
            "knowledge": self.prepare_knowledge,
        }[pipeline_name](session_id)
        followup.accepted = accepted
        followup.rejected = rejected
        followup.message = (
            f"本批接受 {accepted}、拒绝 {rejected}；"
            f"已自动生成下一包 {len(followup.task_ids)} 项"
        )
        self._write_state(followup)
        return followup

    def run_once(self) -> list[WebBatchOutcome]:
        """供总 watcher 调用：处理稳定返回 ZIP，并维护所有活动清洗批次。"""
        results: list[WebBatchOutcome] = []
        for session_id in self._active_session_ids():
            root = self._root(session_id)
            inbox = ensure_dir(self._shared_root(session_id) / "from_phone")
            returned = sorted(inbox.glob("*.zip"), key=lambda path: path.stat().st_mtime)
            processed_response = False
            for package in returned:
                if time.time() - package.stat().st_mtime < self.config.processing.quiet_seconds:
                    continue
                try:
                    result = self.receive(session_id, package)
                except (OSError, ValueError, zipfile.BadZipFile, LLMError) as exc:
                    target = unique_path(ensure_dir(root / "rejected") / package.name)
                    shutil.move(str(package), str(target))
                    atomic_write_text(target.with_suffix(target.suffix + ".error.txt"), str(exc))
                    log.error("网页返回包被拒绝：%s：%s", package, exc)
                    continue
                target = unique_path(ensure_dir(root / "processed") / package.name)
                shutil.move(str(package), str(target))
                results.append(result)
                processed_response = True
                log.info("网页返回包已处理：%s · %s", session_id, result.message)
            if not processed_response:
                try:
                    previous = self._read_state(session_id)
                    session_dir = self.sessions.session_dir(session_id)
                    if not (session_dir / "analysis" / "transcript_clean.json").is_file():
                        maintained = self.prepare(session_id)
                    elif not (session_dir / "analysis" / "outline.json").is_file():
                        maintained = self.prepare_structure(session_id)
                    else:
                        maintained = self.prepare_knowledge(session_id)
                    if self._state_signature(previous) != self._state_signature(
                        maintained.__dict__
                    ):
                        results.append(maintained)
                except LLMError as exc:
                    log.error("维护网页批次失败：%s：%s", session_id, exc)
        return results

    def _active_session_ids(self) -> list[str]:
        active: list[str] = []
        for session_id in self.sessions.list_ids():
            analysis = self.sessions.session_dir(session_id) / "analysis"
            cleaning = (analysis / "clean_web").is_dir() and not (
                analysis / "transcript_clean.json"
            ).is_file()
            structuring = (analysis / "structure_web").is_dir() and not (
                analysis / "outline.json"
            ).is_file()
            extracting = (analysis / "knowledge_web").is_dir() and not (
                analysis / "knowledge.json"
            ).is_file()
            if cleaning or structuring or extracting:
                active.append(session_id)
        return active

    def _root(self, session_id: str) -> Path:
        return self.sessions.session_dir(session_id) / "analysis" / BATCH_ROOT

    def _shared_root(self, session_id: str) -> Path:
        return self.config.paths.web_exchange / session_id

    def _write_state(self, outcome: WebBatchOutcome) -> None:
        text = json.dumps(outcome.__dict__, ensure_ascii=False, indent=2)
        for path in (
            self._root(outcome.session_id) / STATE_FILE,
            self._shared_root(outcome.session_id) / STATE_FILE,
        ):
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                atomic_write_text(path, text)

    def _read_state(self, session_id: str) -> dict[str, Any]:
        path = self._root(session_id) / STATE_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _state_signature(data: dict[str, Any]) -> tuple[Any, ...]:
        return (
            data.get("status"),
            tuple(data.get("task_ids") or []),
            data.get("package_zip"),
            data.get("output_json"),
        )

    def _task_manifest(self, waiting: dict[str, Any]) -> dict[str, Any]:
        exchange_dir = Path(str(waiting["prompt"])).parent
        request_path = exchange_dir / "request.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        pipeline_name = str(request.get("pipeline") or waiting.get("pipeline") or "clean")
        task_id = (
            exchange_dir.name if pipeline_name == "clean"
            else f"{pipeline_name}_{exchange_dir.name}"
        )
        return {
            "task_id": task_id,
            "pipeline": pipeline_name,
            "artifact": request.get("artifact"),
            "stage": request.get("stage"),
            "index": request.get("index"),
            "prompt_sha256": request.get("prompt_sha256"),
            "schema_sha256": request.get("schema_sha256"),
            "source_layer": request.get("source_layer"),
            "source_sha256": request.get("source_sha256"),
            "clean_fingerprint": request.get("clean_fingerprint"),
            "clean_schema_version": request.get("clean_schema_version"),
            "fingerprint": request.get("fingerprint"),
            "schema_version": request.get("schema_version"),
            "exchange_name": exchange_dir.name,
            "prompt_file": f"tasks/{task_id}/prompt.md",
            "schema_file": f"tasks/{task_id}/schema.json",
            "request_file": f"tasks/{task_id}/request.json",
            "response_file": f"responses/{task_id}.json",
        }

    @staticmethod
    def _validate_return_manifest(
        session_id: str,
        returned: dict[str, Any],
        local: dict[str, Any],
    ) -> None:
        if returned.get("session_id") != session_id:
            raise LLMError("返回包 session_id 与收件箱不匹配")
        keys = ("schema_version", "session_id", "batch_id", "tasks")
        if any(returned.get(key) != local.get(key) for key in keys):
            raise LLMError("返回包 manifest 与本机原始批次不一致")

    @staticmethod
    def _validate_current_request(exchange_dir: Path, task: dict[str, Any]) -> None:
        request_path = exchange_dir / "request.json"
        if not request_path.is_file():
            raise LLMError(f"当前网页任务不存在：{exchange_dir.name}")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        for key in (
            "pipeline", "artifact", "stage", "index", "prompt_sha256", "schema_sha256",
            "source_layer", "source_sha256", "clean_fingerprint", "clean_schema_version",
            "fingerprint", "schema_version",
        ):
            if request.get(key) != task.get(key):
                raise LLMError(f"{exchange_dir.name} 的 {key} 已变化，返回包已过期")
        if sha256_file(exchange_dir / "prompt.md") != task["prompt_sha256"]:
            raise LLMError(f"{exchange_dir.name} 当前 prompt 文件哈希不匹配")
        if sha256_file(exchange_dir / "schema.json") != task["schema_sha256"]:
            raise LLMError(f"{exchange_dir.name} 当前 schema 文件哈希不匹配")

    def _exchange_dir(self, session_id: str, task: dict[str, Any]) -> Path:
        pipeline_name = str(task.get("pipeline") or "clean")
        folder = {
            "clean": "clean_web",
            "structure": "structure_web",
            "knowledge": "knowledge_web",
        }.get(pipeline_name)
        if folder is None:
            raise LLMError(f"未知 pipeline：{pipeline_name}")
        return self.sessions.session_dir(session_id) / "analysis" / folder / str(
            task.get("exchange_name") or task["task_id"]
        )

    def _cache_path(self, session_id: str, task: dict[str, Any]) -> Path:
        index = task["index"]
        pipeline_name = str(task.get("pipeline") or "clean")
        if pipeline_name == "structure":
            name = "structure_cache.json"
        elif pipeline_name == "knowledge":
            name = "knowledge_cache.json"
        elif task["stage"] == "chunk":
            name = f"chunk_{int(index):03d}.json"
        else:
            left, right = (int(value) for value in str(index).split("-", 1))
            name = f"boundary_{left:03d}_{right:03d}.json"
        analysis = self.sessions.session_dir(session_id) / "analysis"
        return (
            analysis / name
            if pipeline_name in {"structure", "knowledge"}
            else analysis / "clean_cache" / name
        )


class _PackageReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.exists():
            raise LLMError(f"返回包不存在：{path}")
        self._is_zip = path.is_file()
        self._prefix = self._find_prefix()

    def _find_prefix(self) -> str:
        names = self._names()
        matches = [name for name in names if PurePosixPath(name).name == "manifest.json"]
        if len(matches) != 1:
            raise LLMError("返回包必须且只能包含一个 manifest.json")
        parent = str(PurePosixPath(matches[0]).parent)
        return "" if parent == "." else parent.rstrip("/") + "/"

    def _names(self) -> list[str]:
        if self._is_zip:
            with zipfile.ZipFile(self.path) as archive:
                return [name.replace("\\", "/") for name in archive.namelist()]
        return [
            path.relative_to(self.path).as_posix()
            for path in self.path.rglob("*") if path.is_file()
        ]

    def read_text(self, relative: str) -> str:
        normalized = PurePosixPath(relative).as_posix()
        if normalized.startswith("../") or normalized.startswith("/"):
            raise LLMError(f"返回包包含非法路径：{relative}")
        target = self._prefix + normalized
        try:
            if self._is_zip:
                with zipfile.ZipFile(self.path) as archive:
                    info = archive.getinfo(target)
                    if info.file_size > 10 * 1024 * 1024:
                        raise LLMError(f"返回文件异常过大：{relative}")
                    data = archive.read(info)
                if len(data) > 10 * 1024 * 1024:
                    raise LLMError(f"返回文件异常过大：{relative}")
                return data.decode("utf-8")
            return (self.path / Path(target)).read_text(encoding="utf-8")
        except (KeyError, FileNotFoundError, UnicodeDecodeError) as exc:
            raise LLMError(f"返回包缺少或无法读取：{relative}") from exc

    def read_json(self, relative: str) -> dict[str, Any]:
        try:
            data = json.loads(self.read_text(relative))
        except json.JSONDecodeError as exc:
            raise LLMError(f"返回包中的 {relative} 不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise LLMError(f"返回包中的 {relative} 必须是 JSON object")
        return data


def _json_sha(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_zip(source_dir: Path, target: Path) -> None:
    ensure_dir(target.parent)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir).as_posix())
    os.replace(temporary, target)


def _render_readme(manifest: dict[str, Any]) -> str:
    task_lines = "\n".join(
        f"- `{task['task_id']}` → `{task['response_file']}`"
        for task in manifest["tasks"]
    )
    return f"""# ChatGPT 网页批量课堂处理作业

请完整处理本压缩包中的 {len(manifest['tasks'])} 个独立任务。

## 必须执行

1. 逐项读取 `tasks/<task_id>/prompt.md` 和同目录 `schema.json`。
2. 严格按 prompt 处理；每项只生成一个完整 JSON，不要相互合并。
3. 把结果保存到下列精确路径：

{task_lines}

4. 原样保留根目录 `manifest.json`，不要修改任何字段。
5. 最终返回一个 ZIP，根目录必须包含 `manifest.json` 与 `responses/`。
6. ZIP 建议命名为 `{manifest['return_filename']}`。

不要只写说明文字，也不要遗漏某个任务。程序会逐项校验 schema、segment 拓扑、
prompt/source 指纹与长度异常；不合格项目不会被自动猜测或修补。
"""
