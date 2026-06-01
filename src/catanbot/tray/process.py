"""Bridge process control for the CatanBot menu-bar launcher.

The lifecycle logic the tray app drives: start/stop the local FastAPI
bridge as a subprocess, track it with a pidfile, and report status by
probing the port. Deliberately free of any GUI (rumps) import so it is
unit-testable headless, and it reuses bin/catanbot under the hood so the
venv bootstrap and PYTHONPATH handling stay in one place.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

BRIDGE_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# src/catanbot/tray/process.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]


def state_dir() -> Path:
    """Directory for the launcher's pidfile. Honors CATANBOT_STATE_DIR
    (used by tests), else the macOS Application Support convention.
    Created on demand."""
    base = Path(
        os.environ.get("CATANBOT_STATE_DIR")
        or (Path.home() / "Library" / "Application Support" / "CatanBot")
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def pidfile_path() -> Path:
    return state_dir() / "bridge.pid"


def read_pid() -> int | None:
    try:
        return int(pidfile_path().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_pid(pid: int) -> None:
    pidfile_path().write_text(str(int(pid)))


def clear_pid() -> None:
    try:
        pidfile_path().unlink()
    except FileNotFoundError:
        pass


def port_open(port: int = DEFAULT_PORT, host: str = BRIDGE_HOST,
              timeout: float = 0.4) -> bool:
    """True when something is listening on host:port (the bridge, or
    anything else holding the port)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user; treat as alive.
        return True
    return True


def _pid_is_bridge(pid: int | None) -> bool:
    """True when ``pid`` is alive AND its process command looks like our
    bridge. Guards against a stale pidfile whose PID the OS recycled onto
    an unrelated process: we never signal a PID we cannot confirm is a
    catanbot bridge. Matches the actual launch forms (the python
    ``-m catanbot.cli ... live/bridge`` invocation, or the bin/catanbot
    bash wrapper before it execs) and deliberately does NOT match on the
    repo path alone, which contains 'CatanBot'."""
    if not _pid_alive(pid):
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True, text=True, timeout=2,
        ).stdout.lower()
    except Exception:  # noqa: BLE001
        return False
    if "catanbot.cli" in out:
        return True
    return "bin/catanbot" in out and ("live" in out or "bridge" in out)


def bridge_command(port: int = DEFAULT_PORT) -> list[str]:
    """argv to launch the bridge with the live advisor, via the
    repo-local launcher (which owns the venv bootstrap + PYTHONPATH)."""
    launcher = REPO_ROOT / "bin" / "catanbot"
    return [str(launcher), "live", "--port", str(port)]


def status(port: int = DEFAULT_PORT) -> str:
    """One of 'running' (port answers), 'starting' (we spawned a pid
    that is alive but the port is not up yet), or 'stopped'."""
    if port_open(port):
        return "running"
    if _pid_is_bridge(read_pid()):
        return "starting"
    return "stopped"


def start(port: int = DEFAULT_PORT,
          env: dict[str, str] | None = None) -> str:
    """Start the bridge if it is not already up, and return the
    resulting status. If the port is already answering (a bridge the
    user launched by hand), adopt it instead of spawning a duplicate."""
    if port_open(port):
        return "running"
    # Already spawned and still binding the port: do not double-spawn (a
    # second process would fail to bind and orphan the pidfile from the
    # live one).
    if _pid_is_bridge(read_pid()):
        return "starting"
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(
        bridge_command(port), cwd=str(REPO_ROOT), env=full_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    write_pid(proc.pid)
    return status(port)


def stop(timeout: float = 5.0) -> None:
    """Terminate the launcher-spawned bridge (SIGTERM, then SIGKILL on
    timeout) and clear the pidfile. No-op when nothing is tracked."""
    pid = read_pid()
    if pid and _pid_is_bridge(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    clear_pid()
