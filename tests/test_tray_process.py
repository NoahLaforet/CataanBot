"""Headless tests for the menu-bar launcher's bridge-process control.

No GUI (rumps) import and no real bridge spawned: we exercise the
pidfile round-trip, the port probe, command construction, and the
status state machine in isolation.
"""
from __future__ import annotations

import socket


def test_bridge_command_uses_repo_launcher_and_live():
    from catanbot.tray import process
    cmd = process.bridge_command(port=8765)
    assert cmd[0].endswith("/bin/catanbot")
    assert "live" in cmd
    assert "8765" in cmd


def test_port_open_false_on_closed_port():
    from catanbot.tray import process
    # Grab a free port, then close it so the probe almost certainly
    # finds nothing listening.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert process.port_open(port, timeout=0.2) is False


def test_port_open_true_when_listening():
    from catanbot.tray import process
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert process.port_open(port, timeout=0.5) is True
    finally:
        srv.close()


def test_pidfile_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CATANBOT_STATE_DIR", str(tmp_path))
    from catanbot.tray import process
    assert process.read_pid() is None
    process.write_pid(4242)
    assert process.read_pid() == 4242
    process.clear_pid()
    assert process.read_pid() is None


def test_status_state_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("CATANBOT_STATE_DIR", str(tmp_path))
    from catanbot.tray import process
    # stopped: nothing listening and no tracked bridge pid.
    monkeypatch.setattr(process, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(process, "_pid_is_bridge", lambda pid: False)
    assert process.status() == "stopped"
    # running: the port answers.
    monkeypatch.setattr(process, "port_open", lambda *a, **k: True)
    assert process.status() == "running"
    # starting: a tracked bridge pid is alive but the port is not up yet.
    monkeypatch.setattr(process, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(process, "read_pid", lambda: 4242)
    monkeypatch.setattr(process, "_pid_is_bridge", lambda pid: True)
    assert process.status() == "starting"


def test_tray_status_icons_present():
    """The menu-bar app ships its four status-icon frames and _icon
    resolves each. Guards against the generated assets being dropped from
    the package (which would leave the tray with no icon). _icon is
    defined before the rumps import guard, so this runs without rumps."""
    import os

    from catanbot.tray.app import _icon
    for name in ("stopped", "running", "starting_a", "starting_b"):
        p = _icon(name)
        assert p and p.endswith(f"tray_{name}.png"), name
        assert os.path.exists(p), p


def test_stop_never_kills_a_non_bridge_pid(tmp_path, monkeypatch):
    """stop() must never signal a PID it cannot confirm is the bridge,
    guarding against a stale pidfile whose PID the OS recycled onto an
    unrelated process. Point the pidfile at THIS pytest process (clearly
    not a catanbot bridge) and confirm stop() leaves it running and only
    clears the pidfile."""
    import os
    monkeypatch.setenv("CATANBOT_STATE_DIR", str(tmp_path))
    from catanbot.tray import process
    # Isolate: never resolve a real listener (the adopt-aware stop() path
    # would otherwise run lsof against a live bridge on the default port).
    monkeypatch.setattr(process, "pid_on_port", lambda *a, **k: None)
    process.write_pid(os.getpid())
    process.stop()
    os.kill(os.getpid(), 0)  # raises if stop() had signalled us; it must not
    assert process.read_pid() is None


def test_status_running_skips_ps(monkeypatch):
    """The running fast path must never spawn `ps` (the source of menu-bar
    lag): when the port answers, status() returns 'running' without
    touching _pid_is_bridge."""
    from catanbot.tray import process
    monkeypatch.setattr(process, "port_open", lambda *a, **k: True)
    calls = {"n": 0}

    def boom(pid):
        calls["n"] += 1
        return True

    monkeypatch.setattr(process, "_pid_is_bridge", boom)
    assert process.status() == "running"
    assert calls["n"] == 0


def test_pid_is_bridge_caches_ps(monkeypatch):
    """_pid_is_bridge caches its `ps` result per pid for a short TTL so the
    2s poll and click handlers don't spawn `ps` on every probe."""
    from catanbot.tray import process
    process._BRIDGE_CACHE.clear()
    monkeypatch.setattr(process, "_pid_alive", lambda pid: True)
    runs = {"n": 0}

    class _R:
        stdout = "python -m catanbot.cli live --port 8765"

    def fake_run(*a, **k):
        runs["n"] += 1
        return _R()

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    try:
        assert process._pid_is_bridge(4242) is True
        assert process._pid_is_bridge(4242) is True  # served from cache
        assert runs["n"] == 1
    finally:
        process._BRIDGE_CACHE.clear()


def test_pid_on_port_parses_lsof(monkeypatch):
    from catanbot.tray import process

    class _R:
        stdout = "5123\n"

    monkeypatch.setattr(process.subprocess, "run", lambda *a, **k: _R())
    assert process.pid_on_port(8765) == 5123


def test_stop_kills_adopted_bridge_by_port(tmp_path, monkeypatch):
    """With no tracked pidfile, stop() resolves the listener on the port,
    verifies it is our bridge, and signals it (the externally-started /
    nohup case). It returns True and clears the pidfile. Fully mocked so it
    never touches a real port or a real process."""
    monkeypatch.setenv("CATANBOT_STATE_DIR", str(tmp_path))
    from catanbot.tray import process
    process.clear_pid()
    monkeypatch.setattr(process, "pid_on_port", lambda *a, **k: 7777)
    monkeypatch.setattr(process, "_pid_is_bridge", lambda pid: pid == 7777)
    alive = {"v": True}
    monkeypatch.setattr(process, "_pid_alive", lambda pid: alive["v"])
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        alive["v"] = False  # dies on the first SIGTERM

    monkeypatch.setattr(process.os, "kill", fake_kill)
    assert process.stop(port=8765) is True
    assert killed and killed[0][0] == 7777
    assert process.read_pid() is None


def test_stop_returns_false_when_nothing_running(tmp_path, monkeypatch):
    """No pidfile and no listener on the port: stop() signals nothing and
    reports False so the tray can say 'nothing to stop'."""
    monkeypatch.setenv("CATANBOT_STATE_DIR", str(tmp_path))
    from catanbot.tray import process
    process.clear_pid()
    monkeypatch.setattr(process, "pid_on_port", lambda *a, **k: None)
    assert process.stop(port=8765) is False
    assert process.read_pid() is None
