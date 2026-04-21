"""Pytest bootstrap: put `src/` on sys.path so tests run without -e install.

The editable-install .pth file is flaky on this macOS setup (UF_HIDDEN on
pip-written files), so we inject the import path here the same way the
`bin/cataanbot` launcher does.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
