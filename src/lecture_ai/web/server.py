"""本地 WebUI：看 session 状态、一键出稿。

为什么用标准库 ``http.server`` 而不是 FastAPI：本项目的运行时依赖只有 PyYAML 和
python-dotenv 两个。一个单用户、只绑 localhost、六个接口的面板，不值得为它引入
一整套 Web 框架依赖树 —— 那会让「装上试一下」变得更贵。

安全边界（每一条都在测试里）：

- 默认只绑 ``127.0.0.1``；绑到其他地址会打印显眼警告，因为本面板**没有任何认证**，
  而它能触发按 token 计费的 API 调用。
- API 令牌在页面上输入后**只存在本进程内存里**：不写 config、不写 .env、不进日志、
  接口也永不回显，只回一个「有没有设置」的布尔值。进程退出即消失。
- 出稿仍然走 :class:`~lecture_ai.note.NoteBuilder`，因此 REPAIRED 硬要求、
  provider 检查、隐私闸门、幂等保护全部照旧生效 —— WebUI 不是绕过它们的后门。

**不在本版范围内**：上传录音与触发转录。转录一节课要 45–65 分钟，塞进 HTTP 请求
需要一套后台任务机制；而录音接入本来已经由 ``watch`` 自动化了。WebUI 先解决
「看状态 + 出稿」这段，那是命令行最啰嗦、而 API 又足够快的部分。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LectureAIError
from lecture_ai.repair import REPAIRED_MD
from lecture_ai.session import SessionManager
from lecture_ai.utils.naming import final_note_name, identity_prefix

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass
class KeyStore:
    """进程内存里的 API 令牌。只有 set / has / get，没有任何持久化路径。"""

    _key: str | None = None
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def set(self, key: str | None) -> None:
        with self._lock:
            self._key = (key or "").strip() or None

    def get(self) -> str | None:
        with self._lock:
            return self._key

    def has(self) -> bool:
        return self.get() is not None


class AppState:
    """服务端共享状态。每个请求现取 SessionManager，避免跨线程共用连接。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.keys = KeyStore()

    def sessions(self) -> SessionManager:
        return SessionManager(self.config, Database(self.config.paths.database))

    # ------------------------------------------------------------------ 读

    def list_sessions(self) -> list[dict]:
        manager = self.sessions()
        rows = []
        for session_id in sorted(manager.list_ids(), reverse=True):
            try:
                rows.append(self._summary(manager, session_id))
            except LectureAIError as exc:  # 单个坏 session 不该让整个面板打不开
                rows.append({"session_id": session_id, "error": str(exc)})
        return rows

    def _summary(self, manager: SessionManager, session_id: str) -> dict:
        meta = manager.load(session_id)
        session_dir = manager.session_dir(session_id)
        note_path = session_dir / "note" / final_note_name(identity_prefix(meta))
        return {
            "session_id": session_id,
            "course": meta.course.name,
            "course_key": meta.course.key,
            "date": meta.date,
            "start_time": meta.start_time,
            "state": str(meta.state),
            "duration_sec": meta.audio.duration_sec,
            "has_repaired": (session_dir / "transcript" / REPAIRED_MD).is_file(),
            "has_note": note_path.is_file(),
            "note_name": note_path.name,
            "steps": {name: status.status for name, status in meta.steps.items()},
        }

    def session_detail(self, session_id: str) -> dict:
        manager = self.sessions()
        detail = self._summary(manager, session_id)
        session_dir = manager.session_dir(session_id)
        transcript = session_dir / "transcript" / REPAIRED_MD
        detail["transcript_chars"] = (
            len(transcript.read_text(encoding="utf-8")) if transcript.is_file() else 0
        )
        detail["board_count"] = sum(
            1 for p in (session_dir / "images").glob("**/*") if p.is_file()
        )
        detail["slide_count"] = sum(
            1 for p in (session_dir / "slides").glob("**/*") if p.is_file()
        )
        return detail

    def read_note(self, session_id: str) -> str:
        manager = self.sessions()
        meta = manager.load(session_id)
        path = manager.session_dir(session_id) / "note" / final_note_name(identity_prefix(meta))
        if not path.is_file():
            raise LectureAIError(f"session {session_id} 还没有成稿。")
        return path.read_text(encoding="utf-8")

    def runtime(self) -> dict:
        """注意：只回「令牌是否已设置」，永不回令牌本身。"""
        return {
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "api_key_set": self.keys.has(),
            "needs_api_key": (self.config.llm.provider or "").strip().lower() == "openai",
            "allow_cloud_transcript": self.config.privacy.allow_cloud_transcript,
            "export_dir": str(self.config.paths.export_dir),
            "max_output_tokens": self.config.note.max_output_tokens,
        }

    # ------------------------------------------------------------------ 写

    def build_note(self, session_id: str, *, force: bool) -> dict:
        from lecture_ai.note import NoteBuilder

        outcome = NoteBuilder(
            self.config, self.sessions().db, api_key=self.keys.get()
        ).build(session_id, force=force)
        return {
            "session_id": outcome.session_id,
            "output_path": str(outcome.output_path),
            "note_name": outcome.output_path.name,
            "provider": outcome.provider,
            "model": outcome.model,
            "usage": outcome.usage,
            "warnings": outcome.warnings,
        }

    def build_package(self, session_id: str) -> dict:
        from lecture_ai.export_package import ExportPackageBuilder

        outcome = ExportPackageBuilder(self.config, self.sessions().db).build(session_id)
        return {
            "session_id": outcome.session_id,
            "output_dir": str(outcome.output_dir),
            "board_count": outcome.board_count,
            "slide_count": outcome.slide_count,
            "unassigned_count": outcome.unassigned_count,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "lecture-ai-web"

    def __init__(self, *args, state: AppState, **kwargs) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------ 工具

    def log_message(self, fmt: str, *args) -> None:
        log.debug("web %s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 面板不该被任何页面跨源读取，也不该被缓存住旧状态。
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 64 * 1024:  # 面板不接收大 body
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _serve_page(self) -> None:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"缺少页面文件：{page}")
            return
        self._send(HTTPStatus.OK, page.read_bytes(), "text/html; charset=utf-8")

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._serve_page()
            elif path == "/api/runtime":
                self._json(self.state.runtime())
            elif path == "/api/sessions":
                self._json({"sessions": self.state.list_sessions()})
            elif path.startswith("/api/sessions/"):
                rest = path[len("/api/sessions/"):]
                if rest.endswith("/note"):
                    session_id = unquote(rest[: -len("/note")])
                    self._json({"markdown": self.state.read_note(session_id)})
                else:
                    self._json(self.state.session_detail(unquote(rest)))
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知路径：{path}")
        except LectureAIError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # 面板不该因为一个坏请求整体挂掉
            log.exception("web GET %s 失败", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/key":
                self.state.keys.set(payload.get("api_key"))
                self._json(self.state.runtime())  # 只回状态，不回令牌
            elif path.startswith("/api/sessions/") and path.endswith("/note"):
                session_id = unquote(path[len("/api/sessions/"): -len("/note")])
                self._json(self.state.build_note(session_id, force=bool(payload.get("force"))))
            elif path.startswith("/api/sessions/") and path.endswith("/export-package"):
                session_id = unquote(path[len("/api/sessions/"): -len("/export-package")])
                self._json(self.state.build_package(session_id))
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知路径：{path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except LectureAIError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            log.exception("web POST %s 失败", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))


def make_server(config: Config, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """构造但不启动服务器。port=0 时由系统分配（测试用）。"""
    state = AppState(config)
    return ThreadingHTTPServer((host, port), partial(Handler, state=state))


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS
