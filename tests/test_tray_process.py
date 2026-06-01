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


def test_stop_never_kills_a_non_bridge_pid(tmp_path, monkeypatch):
    """stop() must never signal a PID it cannot confirm is the bridge,
    guarding against a stale pidfile whose PID the OS recycled onto an
    unrelated process. Point the pidfile at THIS pytest process (clearly
    not a catanbot bridge) and confirm stop() leaves it running and only
    clears the pidfile."""
    import os
    monkeypatch.setenv("CATANBOT_STATE_DIR", str(tmp_path))
    from catanbot.tray import process
    process.write_pid(os.getpid())
    process.stop()
    os.kill(os.getpid(), 0)  # raises if stop() had signalled us; it must not
    assert process.read_pid() is None
