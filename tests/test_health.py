"""健康探测。

这一层的全部价值在于「坏情况下仍然出得来结果」——它是给面板用的旁路信息，
探测失败只该少显示一块，绝不能让面板打不开，更不能拖慢每几秒一次的轮询。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from lecture_ai import health


@pytest.fixture(autouse=True)
def _clear_caches():
    """缓存是模块级的，测试之间必须清掉，否则互相污染。"""
    health._machine_cache.value = None
    health._sync_cache.value = None
    yield
    health._machine_cache.value = None
    health._sync_cache.value = None


# ------------------------------------------------------------------ 录音新鲜度


def _drop(config, name: str, *, age_hours: float) -> None:
    path = config.paths.incoming_audio / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 16)
    when = time.time() - age_hours * 3600
    import os

    os.utime(path, (when, when))


def test_incoming_reports_the_newest_recording(config):
    _drop(config, "old.m4a", age_hours=50)
    _drop(config, "new.m4a", age_hours=1)

    got = health.incoming_health(config, stale_hours=6)
    assert got["latest_name"] == "new.m4a"
    assert got["stale"] is False
    assert 0.9 < got["age_hours"] < 1.1


def test_incoming_goes_stale_past_the_threshold(config):
    _drop(config, "old.m4a", age_hours=9)
    assert health.incoming_health(config, stale_hours=6)["stale"] is True
    assert health.incoming_health(config, stale_hours=12)["stale"] is False


def test_incoming_ignores_non_audio_files(config):
    _drop(config, "notes.txt", age_hours=1)
    _drop(config, "real.m4a", age_hours=3)
    assert health.incoming_health(config)["latest_name"] == "real.m4a"


def test_empty_incoming_is_not_an_alert(config):
    """一个空的收件目录只是还没开始用，不是故障。"""
    got = health.incoming_health(config)
    assert got["available"] is True
    assert got["latest_name"] is None
    assert got["stale"] is False


# ------------------------------------------------------------------ Syncthing


def _write_syncthing_home(tmp_path, address="127.0.0.1:8384", api_key="KEY"):
    home = tmp_path / "st"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.xml").write_text(
        '<configuration>'
        f'<gui enabled="true"><address>{address}</address>'
        f'<apikey>{api_key}</apikey></gui>'
        '</configuration>',
        encoding="utf-8",
    )
    return home


def test_syncthing_credentials_are_parsed(tmp_path):
    home = _write_syncthing_home(tmp_path, "127.0.0.1:9999", "secret-key")
    assert health._syncthing_creds(home) == ("127.0.0.1:9999", "secret-key")


def test_missing_syncthing_is_reported_not_raised(config, tmp_path):
    """手动拷贝录音的人没有 Syncthing —— 那不是故障，只是没这块信息。"""
    config.monitoring.syncthing_home = str(tmp_path / "nowhere")
    got = health.sync_health(config)
    assert got["available"] is False
    assert "reason" in got


def test_sync_health_marks_a_never_connected_peer(config, tmp_path, monkeypatch):
    """Syncthing 用 0001-01-01 表示"本次启动以来从未连上"，不能当成真实时间显示。"""
    config.monitoring.syncthing_home = str(_write_syncthing_home(tmp_path))

    def fake_get(_addr, _key, path):
        if path.endswith("connections"):
            return {"connections": {"DEV1": {"connected": False, "at": "0001-01-01T00:00:00Z"}}}
        return [{"deviceID": "DEV1", "name": "手机"}]

    monkeypatch.setattr(health, "_syncthing_get", fake_get)
    dev = health.sync_health(config)["devices"][0]
    assert dev["name"] == "手机"
    assert dev["connected"] is False
    assert dev["last_seen"] is None
    assert dev["offline_hours"] is None


def test_sync_health_computes_offline_hours(config, tmp_path, monkeypatch):
    config.monitoring.syncthing_home = str(_write_syncthing_home(tmp_path))
    seen = (datetime.now() - timedelta(hours=3)).isoformat()

    def fake_get(_addr, _key, path):
        if path.endswith("connections"):
            return {"connections": {"D": {"connected": False, "at": seen}}}
        return [{"deviceID": "D", "name": "phone"}]

    monkeypatch.setattr(health, "_syncthing_get", fake_get)
    dev = health.sync_health(config)["devices"][0]
    assert 2.8 < dev["offline_hours"] < 3.2


def test_sync_health_survives_a_dead_syncthing(config, tmp_path, monkeypatch):
    config.monitoring.syncthing_home = str(_write_syncthing_home(tmp_path))
    monkeypatch.setattr(health, "_syncthing_get", lambda *a: None)
    assert health.sync_health(config)["available"] is False


# ------------------------------------------------------------------ 告警


def test_no_alerts_when_everything_is_fine():
    alerts = health.build_alerts(
        machine={"available": True, "boot_time": None, "last_crash": None},
        sync={"available": True, "devices": [{"name": "phone", "connected": True}]},
        incoming={"available": True, "stale": False, "latest_name": "a.m4a"},
        watch={"running": True},
    )
    assert alerts == []


def test_watch_down_is_an_error_not_a_warning():
    alerts = health.build_alerts({}, {}, {}, {"running": False})
    assert alerts[0]["level"] == "error"
    assert "watch" in alerts[0]["text"]


def test_disconnected_peer_raises_a_warning():
    alerts = health.build_alerts(
        {}, {"devices": [{"name": "手机", "connected": False, "offline_hours": 4.0}]},
        {}, {"running": True},
    )
    assert len(alerts) == 1
    assert "手机" in alerts[0]["text"]
    assert "4.0" in alerts[0]["text"]


def test_crash_alert_only_fires_when_this_boot_followed_a_crash():
    """Event 41 是崩溃后下一次开机时写的，所以它必须紧挨着 boot_time。"""
    boot = datetime(2026, 9, 9, 8, 54, 50)

    near = health.build_alerts(
        {"boot_time": boot.isoformat(), "last_crash": (boot + timedelta(seconds=4)).isoformat(),
         "crash_count_30d": 3},
        {}, {}, {"running": True},
    )
    assert len(near) == 1
    assert "3 次" in near[0]["text"]

    # 上个月崩过一次、这次是正常重启 —— 不该报警
    far = health.build_alerts(
        {"boot_time": boot.isoformat(), "last_crash": (boot - timedelta(days=20)).isoformat(),
         "crash_count_30d": 1},
        {}, {}, {"running": True},
    )
    assert far == []


def test_stale_incoming_raises_a_warning():
    alerts = health.build_alerts(
        {}, {}, {"stale": True, "age_hours": 9.5, "latest_name": "x.m4a"}, {"running": True},
    )
    assert "9.5" in alerts[0]["text"]


# ------------------------------------------------------------------ 汇总与缓存


def test_snapshot_never_raises_even_when_everything_fails(config, monkeypatch):
    monkeypatch.setattr(health, "machine_health", lambda: {"available": False})
    monkeypatch.setattr(health, "sync_health", lambda _c: {"available": False})
    got = health.snapshot(config, {"running": True})
    assert set(got) == {"machine", "sync", "incoming", "alerts"}


def test_machine_probe_is_cached(monkeypatch):
    """事件日志查询约 1 秒，面板每 5 秒轮询一次，不缓存会把机器拖垮。"""
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(health, "_run", fake_run)
    monkeypatch.setattr("os.name", "nt", raising=False)

    import os

    if os.name != "nt":
        pytest.skip("机器探测只在 Windows 上做")

    health.machine_health()
    first = len(calls)
    health.machine_health()
    assert len(calls) == first, "第二次调用应该命中缓存"


def test_parse_iso_tolerates_junk():
    for junk in ["", "   ", "not a date", None]:
        assert health._parse_iso(junk) is None
