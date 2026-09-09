"""机器与同步链路的健康探测。

存在的理由是 2026-09-08/09 连续两天的两次事故：

- 09-08：ffmpeg 每 15 秒弹一次窗，面板上完全看不见，靠肉眼发现，拖了一上午；
- 09-09：手机 Syncthing 的核心假死（App 显示"运行中"、端口却不监听），
  电脑早上 8:54 又蓝屏重启过一次，两件事叠在一起，直到中午才被发现，
  上午两节课的录音一直躺在手机里。

共同点都不是"修不好"，而是**没人知道它坏了**。所以这里探测的全是
「链路断了但没人报错」的那类状态，而不是程序自己的异常。

三条硬约束：

- **绝不抛异常。** 这是给面板用的旁路信息，探测失败就少显示一块，
  不能让面板打不开。
- **绝不阻塞。** 面板每几秒轮询一次，昂贵的探测（读事件日志要 ~1 秒）
  一律带缓存。
- **子进程绝不弹窗。** 见 audio/ffmpeg.py 的教训：serve 有可能被 pythonw
  拉起，那时每次探测都会闪一个黑框。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from lecture_ai.config import Config
from lecture_ai.logging_setup import get_logger

log = get_logger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 事件日志要起子进程、约 1 秒，缓存久一点。开机时间不会变，崩溃更是罕见。
MACHINE_TTL_SEC = 300.0
#: Syncthing 是本地 HTTP，便宜，但也没必要每次轮询都打。
SYNC_TTL_SEC = 20.0

#: 距上一次收到新录音超过这个小时数，就认为同步链路可疑。
#: 取 6 小时：一天最早的课 8 点、最晚 21:55，正常上课日不会连续 6 小时没动静；
#: 而周末/假期本来就没有课，那时的"陈旧"不是故障，所以还要看当天有没有课。
DEFAULT_STALE_HOURS = 6.0


@dataclass
class _Cached:
    """一格带 TTL 的缓存。探测失败时沿用上一次的好结果，避免面板忽亮忽灭。"""

    ttl: float
    value: Any = None
    at: float = 0.0

    def fresh(self) -> bool:
        return self.value is not None and (time.monotonic() - self.at) < self.ttl

    def put(self, value: Any) -> Any:
        self.value = value
        self.at = time.monotonic()
        return value


_machine_cache = _Cached(MACHINE_TTL_SEC)
_sync_cache = _Cached(SYNC_TTL_SEC)


def _run(cmd: list[str], timeout: float) -> str:
    """跑一个只读命令拿 stdout。任何失败都返回空串。"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("健康探测命令失败（忽略）：%s", exc)
        return ""
    return result.stdout or ""


# --------------------------------------------------------------------- 机器


_EVT_TIME_RE = re.compile(r"SystemTime='([0-9T:.\-]+)Z?'")


def machine_health() -> dict:
    """上次开机时间、运行时长、最近的异常关机。

    异常关机走 Windows 事件日志的 Kernel-Power 41 —— 蓝屏和掉电都会记它。
    非 Windows 直接返回 available=False，不做任何猜测。
    """
    if _machine_cache.fresh():
        return _machine_cache.value

    import os

    if os.name != "nt":
        return _machine_cache.put({"available": False})

    info: dict = {"available": True, "boot_time": None, "uptime_sec": None,
                  "last_crash": None, "crash_count_30d": 0}

    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')"], 20)
    boot = _parse_iso(out.strip())
    if boot is not None:
        info["boot_time"] = boot.isoformat(timespec="seconds")
        info["uptime_sec"] = max(0.0, (datetime.now() - boot).total_seconds())

    crashes = _unexpected_shutdowns()
    if crashes:
        info["last_crash"] = crashes[0].isoformat(timespec="seconds")
        cutoff = datetime.now() - timedelta(days=30)
        info["crash_count_30d"] = sum(1 for c in crashes if c >= cutoff)

    return _machine_cache.put(info)


def _unexpected_shutdowns(limit: int = 12) -> list[datetime]:
    """Kernel-Power 41 的时间列表，新的在前。

    用 wevtutil 而不是 Get-WinEvent：后者在这台机器上渲染某些 provider 的
    消息时会抛 "The specified resource type cannot be found in the image file"，
    整个查询直接失败。wevtutil 输出原始 XML，不碰消息模板，稳。
    """
    out = _run(["wevtutil", "qe", "System",
                "/q:*[System[(EventID=41)]]", f"/c:{limit}", "/rd:true", "/f:xml"], 25)
    stamps = []
    for raw in _EVT_TIME_RE.findall(out):
        parsed = _parse_iso(raw, utc=True)
        if parsed is not None:
            stamps.append(parsed)
    return stamps


def _parse_iso(text: str, *, utc: bool = False) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        try:
            parsed = parsed.astimezone().replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            # Syncthing 的"从未连接"哨兵值 0001-01-01 在 Windows 上会让
            # astimezone 直接抛 OSError(22)。这类时间本来就没有意义，丢掉。
            return None
    elif utc:
        # 事件日志里的 SystemTime 是 UTC，但不带时区标记
        parsed = (parsed.replace(tzinfo=None)
                  + (datetime.now() - datetime.utcnow())).replace(microsecond=0)
    return parsed


# --------------------------------------------------------------------- 同步


def sync_health(config: Config) -> dict:
    """Syncthing 各对端的连接状态。

    直接问本机 Syncthing 的 REST API。找不到它的配置就返回 available=False ——
    手动拷贝录音的人本来就没有 Syncthing，那不是故障。
    """
    if _sync_cache.fresh():
        return _sync_cache.value

    home = _syncthing_home(config)
    if home is None:
        return _sync_cache.put({"available": False, "reason": "未找到 Syncthing 配置"})

    creds = _syncthing_creds(home)
    if creds is None:
        return _sync_cache.put({"available": False, "reason": f"无法解析 {home}/config.xml"})
    address, api_key = creds

    conns = _syncthing_get(address, api_key, "/rest/system/connections")
    devices_cfg = _syncthing_get(address, api_key, "/rest/config/devices")
    if conns is None:
        return _sync_cache.put({"available": False, "reason": "Syncthing 没有响应（没在跑？）"})

    names = {}
    for dev in devices_cfg or []:
        if isinstance(dev, dict) and dev.get("deviceID"):
            names[dev["deviceID"]] = dev.get("name") or dev["deviceID"][:7]

    devices = []
    for dev_id, info in (conns.get("connections") or {}).items():
        if not isinstance(info, dict):
            continue
        last = _parse_iso(str(info.get("at") or ""))
        # Syncthing 用 0001-01-01 表示"本进程启动以来从未连上"
        if last is not None and last.year < 1970:
            last = None
        devices.append({
            "name": names.get(dev_id, dev_id[:7]),
            "connected": bool(info.get("connected")),
            "address": info.get("address") or None,
            "last_seen": last.isoformat(timespec="seconds") if last else None,
            "offline_hours": (
                None if info.get("connected") or last is None
                else round((datetime.now() - last).total_seconds() / 3600, 1)
            ),
        })

    devices.sort(key=lambda d: (d["connected"], d["name"]))
    return _sync_cache.put({"available": True, "devices": devices})


def _syncthing_home(config: Config) -> Path | None:
    """定位 Syncthing 的 home。配置里指定优先，否则按常见位置探。"""
    configured = (getattr(config.monitoring, "syncthing_home", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if (path / "config.xml").is_file() else None

    import os

    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += [Path(local) / "LectureAI" / "Syncthing", Path(local) / "Syncthing"]
    candidates += [
        Path.home() / ".local" / "state" / "syncthing",
        Path.home() / ".config" / "syncthing",
        Path.home() / "Library" / "Application Support" / "Syncthing",
    ]
    for path in candidates:
        if (path / "config.xml").is_file():
            return path
    return None


_GUI_RE = re.compile(r"<gui\b.*?</gui>", re.S)
_ADDR_RE = re.compile(r"<address>([^<]+)</address>")
_KEY_RE = re.compile(r"<apikey>([^<]+)</apikey>")


def _syncthing_creds(home: Path) -> tuple[str, str] | None:
    try:
        text = (home / "config.xml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    gui = _GUI_RE.search(text)
    if gui is None:
        return None
    address = _ADDR_RE.search(gui.group(0))
    api_key = _KEY_RE.search(gui.group(0))
    if address is None or api_key is None:
        return None
    return address.group(1).strip(), api_key.group(1).strip()


def _syncthing_get(address: str, api_key: str, path: str) -> Any:
    req = urllib.request.Request(
        f"http://{address}{path}", headers={"X-API-Key": api_key}
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as res:
            return json.loads(res.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        log.debug("Syncthing 查询失败（忽略）：%s", exc)
        return None


# --------------------------------------------------------------------- 录音新鲜度


def incoming_health(config: Config, *, stale_hours: float | None = None) -> dict:
    """最近一次收到新录音是什么时候。

    这是最不依赖具体同步方案的一条信号 —— 不管你用 Syncthing、SFTP 还是
    手动拷贝，录音进不来就是链路断了。
    """
    threshold = stale_hours if stale_hours is not None else getattr(
        config.monitoring, "sync_stale_hours", DEFAULT_STALE_HOURS
    )
    exts = {e.lower() for e in config.audio.extensions}
    latest_path, latest_mtime = None, 0.0
    try:
        for item in config.paths.incoming_audio.iterdir():
            if not item.is_file() or item.suffix.lower() not in exts:
                continue
            mtime = item.stat().st_mtime
            if mtime > latest_mtime:
                latest_path, latest_mtime = item, mtime
    except OSError as exc:
        return {"available": False, "reason": str(exc)}

    if latest_path is None:
        return {"available": True, "latest_name": None, "age_hours": None, "stale": False}

    age_hours = max(0.0, (time.time() - latest_mtime) / 3600)
    return {
        "available": True,
        "latest_name": latest_path.name,
        "latest_at": datetime.fromtimestamp(latest_mtime).isoformat(timespec="seconds"),
        "age_hours": round(age_hours, 1),
        "stale": age_hours > threshold,
        "stale_hours": threshold,
    }


# --------------------------------------------------------------------- 汇总


def build_alerts(machine: dict, sync: dict, incoming: dict, watch: dict) -> list[dict]:
    """把上面几块揉成人话告警，最要紧的排前面。

    面板顶部只显示这个列表 —— 一切正常时它是空的，那本身就是最好的状态。
    """
    alerts: list[dict] = []

    if not watch.get("running"):
        alerts.append({
            "level": "error",
            "text": "watch 没在运行，新录音不会被自动处理。",
            "hint": "启动它：`python -m lecture_ai watch`",
        })

    for dev in sync.get("devices") or []:
        if dev["connected"]:
            continue
        hours = dev.get("offline_hours")
        when = f"已断开 {hours} 小时" if hours is not None else "本次启动以来从未连上"
        alerts.append({
            "level": "warn",
            "text": f"同步设备「{dev['name']}」{when}，录音可能还堵在那一端。",
            "hint": "先确认对端 Syncthing 真的在跑；App 显示「运行中」不代表核心还活着，"
                    "端口不监听时重启它（菜单 → Restart）即可。",
        })

    if incoming.get("stale"):
        alerts.append({
            "level": "warn",
            "text": f"已经 {incoming['age_hours']} 小时没有新录音进来"
                    f"（最近一个：{incoming.get('latest_name') or '无'}）。",
            "hint": "今天没课就忽略；有课的话多半是同步断了。",
        })

    # Event 41 是在崩溃后的**下一次开机**时写的，所以它紧挨着 boot_time
    # 就说明「这次开机是从一次崩溃里爬起来的」，而不是一次正常重启。
    crash = _parse_iso(machine.get("last_crash") or "")
    boot = _parse_iso(machine.get("boot_time") or "")
    if crash and boot and abs((crash - boot).total_seconds()) < 600:
        count = machine.get("crash_count_30d") or 0
        suffix = f"，30 天内已发生 {count} 次" if count > 1 else ""
        alerts.append({
            "level": "warn",
            "text": f"这台机器上次是异常关机重启的（{crash:%m-%d %H:%M}）{suffix}。",
            "hint": "崩溃会带走所有正在跑的任务，包括没转完的课。"
                    "转储在 C:\\Windows\\Minidump。",
        })

    return alerts


def snapshot(config: Config, watch: dict) -> dict:
    """面板要的那一整块。任何一部分探测失败都只是少一块，不影响其余。"""
    machine = machine_health()
    sync = sync_health(config)
    incoming = incoming_health(config)
    return {
        "machine": machine,
        "sync": sync,
        "incoming": incoming,
        "alerts": build_alerts(machine, sync, incoming, watch),
    }
