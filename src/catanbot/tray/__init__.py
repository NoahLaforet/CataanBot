"""macOS menu-bar launcher for the CatanBot bridge.

`process` holds the GUI-free start/stop/status logic (unit-tested);
`app` is the rumps menu-bar shell that drives it. Launch via
`bin/catanbot-tray`. `bin/catanbot` stays the canonical cross-platform
path; this package is a mac convenience wrapper over the same bridge.
"""
