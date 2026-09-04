"""本地 WebUI 的接口契约与安全边界。

重点验证两件容易出事的：
1. API 令牌只在内存里，任何接口都不回显它；
2. WebUI 不是绕过 REPAIRED 硬要求和幂等保护的后门。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from lecture_ai.session import SessionManager, load_courses
from lecture_ai.web import (
    DEFAULT_PORT,
    AppState,
    KeyStore,
    bind_error_advice,
    is_loopback,
    make_server,
)

START = datetime(2026, 9, 3, 14, 0)
SECRET = "sk-test-do-not-leak-0123456789"


def _session(config, db, *, repaired: bool = True):
    manager = SessionManager(config, db)
    course = load_courses(config.courses_path).get("quantum_mechanics")
    meta = manager.create(course, START)
    if repaired:
        path = manager.session_dir(meta.session_id) / "transcript" / "transcript_repaired.md"
        path.write_text("# 修复后转录\n\n老师讲了归一化条件。\n", encoding="utf-8")
    return manager, meta


@pytest.fixture
def server(config):
    """真起一个服务器，端口由系统分配，测试结束关掉。"""
    config.llm.provider = "fake"        # 走 FakeLLMClient，不联网
    srv = make_server(config, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def post(base: str, path: str, payload: dict):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ----------------------------------------------------------------- 令牌不落盘


def test_keystore_normalizes_and_forgets():
    store = KeyStore()
    assert store.has() is False
    store.set("  " + SECRET + "  ")
    assert store.get() == SECRET
    store.set("   ")                     # 空白等于清空，不是存一个空串
    assert store.has() is False
    assert store.get() is None


def test_runtime_never_returns_the_key(config):
    state = AppState(config)
    state.keys.set(SECRET)
    payload = state.runtime()
    assert payload["api_key_set"] is True
    assert SECRET not in json.dumps(payload)


def test_key_endpoint_reports_status_without_echoing(server):
    status, payload = post(server, "/api/key", {"api_key": SECRET})
    assert status == 200
    assert payload["api_key_set"] is True
    assert SECRET not in json.dumps(payload, ensure_ascii=False)

    _status, runtime = get(server, "/api/runtime")
    assert SECRET not in json.dumps(runtime, ensure_ascii=False)


def test_key_is_never_written_to_any_file(server, config):
    post(server, "/api/key", {"api_key": SECRET})
    root = Path(config.paths.project_root)
    hits = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml", ".json", ".md", ".log", ".env", ""}
        and SECRET in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == [], f"令牌泄漏到文件：{hits}"


# ----------------------------------------------------------------- 读接口


def test_page_is_served(server):
    with urllib.request.urlopen(server + "/", timeout=10) as res:
        body = res.read().decode("utf-8")
    assert res.status == 200
    assert "lecture-ai" in body
    assert "<script" in body            # 页面自带脚本，不依赖任何外部 CDN
    assert "http://" not in body.replace("http://www.w3.org", "")


def test_sessions_list_and_detail(server, config, db):
    _manager, meta = _session(config, db)
    status, payload = get(server, "/api/sessions")
    assert status == 200
    row = next(s for s in payload["sessions"] if s["session_id"] == meta.session_id)
    assert row["course"] == "量子力学"
    assert row["has_repaired"] is True
    assert row["has_note"] is False

    _status, detail = get(server, "/api/sessions/" + meta.session_id)
    assert detail["transcript_chars"] > 0
    assert detail["board_count"] == 0


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/nope")
    assert exc.value.code == 404


def test_reading_missing_note_is_a_clean_error(server, config, db):
    _manager, meta = _session(config, db)
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, f"/api/sessions/{meta.session_id}/note")
    assert exc.value.code == 400
    assert "还没有成稿" in json.loads(exc.value.read().decode("utf-8"))["error"]


# ----------------------------------------------------------------- 写接口


def test_note_generation_through_http(server, config, db):
    _manager, meta = _session(config, db)
    status, payload = post(server, f"/api/sessions/{meta.session_id}/note", {})
    assert status == 200
    assert payload["provider"] == "fake"
    assert Path(payload["output_path"]).is_file()

    _status, note = get(server, f"/api/sessions/{meta.session_id}/note")
    assert note["markdown"].startswith("---\n")      # 程序补的 front-matter


def test_webui_does_not_bypass_repaired_requirement(server, config, db):
    _manager, meta = _session(config, db, repaired=False)
    status, payload = post(server, f"/api/sessions/{meta.session_id}/note", {})
    assert status == 400
    assert "transcript_repaired" in payload["error"]


def test_webui_does_not_bypass_idempotence(server, config, db):
    _manager, meta = _session(config, db)
    assert post(server, f"/api/sessions/{meta.session_id}/note", {})[0] == 200

    status, payload = post(server, f"/api/sessions/{meta.session_id}/note", {})
    assert status == 400
    assert "--force" in payload["error"]

    assert post(server, f"/api/sessions/{meta.session_id}/note", {"force": True})[0] == 200


def test_oversized_body_is_rejected(server):
    """必须回 413 且客户端读得到。

    曾经的 bug：服务端不读 body 就直接回 400 并关连接，客户端还在写请求体，
    于是撞上连接重置（Windows: WinError 10053），根本看不到状态码。
    修法是拒绝前先排空 body，并显式 Connection: close。
    """
    req = urllib.request.Request(
        server + "/api/key",
        data=json.dumps({"api_key": "x" * 200_000}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 413
    assert "请求体过大" in json.loads(exc.value.read().decode("utf-8"))["error"]


def test_server_survives_oversized_body_and_still_serves(server):
    """拒绝之后连接状态必须是干净的 —— 下一个请求要能正常处理。"""
    req = urllib.request.Request(
        server + "/api/key",
        data=b'{"api_key":"' + b"x" * 100_000 + b'"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req, timeout=10)

    status, payload = get(server, "/api/runtime")
    assert status == 200
    assert payload["api_key_set"] is False        # 超大请求没有污染状态


# ----------------------------------------------------------------- 绑定地址


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True),
    ("0.0.0.0", False), ("192.168.1.5", False),
])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


# ----------------------------------------------------------------- 端口冲突


def test_default_port_is_not_8765():
    """8765 被百度输入法占用（0.0.0.0），在中文 Windows 上极常见。

    对方绑通配地址时 Windows 报 WinError 10013 而不是 10048，
    错误信息会把人误导到防火墙方向，所以默认端口必须避开它。
    """
    assert DEFAULT_PORT != 8765


def test_bind_error_advice_is_actionable():
    """端口冲突不该只丢一个 traceback，得给出能直接照做的命令。"""
    exc = OSError("[WinError 10013] An attempt was made to access a socket ...")
    lines = bind_error_advice("127.0.0.1", 8477, exc)
    text = "\n".join(lines)

    assert "127.0.0.1:8477" in text
    assert str(exc) in text
    assert "--port 8478" in text          # 建议换的下一个端口
    assert "--port 0" in text             # 让系统分配
    assert "Get-NetTCPConnection" in text  # 怎么查是谁占的
    assert "excludedportrange" in text     # Hyper-V/WSL 预留段


def test_bind_failure_surfaces_as_oserror(config):
    """make_server 在端口不可用时抛 OSError，交给调用方翻译。"""
    import socket

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    try:
        with pytest.raises(OSError):
            make_server(config, "127.0.0.1", taken)
    finally:
        holder.close()
