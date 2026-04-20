"""Smoke tests — confirms the package imports and the doctor command runs."""
from __future__ import annotations

from cataanbot.cli import cmd_doctor


def test_doctor_runs() -> int:
    # If catanatron is installed, doctor returns 0.
    # If not, it returns 1 with a helpful message — both are acceptable as a
    # smoke test; we only care that it doesn't crash.
    rc = cmd_doctor()
    assert rc in (0, 1)
