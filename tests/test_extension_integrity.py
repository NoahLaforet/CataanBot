"""Static-integrity guards on the Chrome extension sources.

Cheap source-level assertions (no JS runtime, so they run in CI without
a Node harness) that lock in invariants the 2026-05-31 audit surfaced:
the push-refresh hook stays wired, the manifest only requests
permissions it actually uses, and the three version strings agree.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
EXT = ROOT / "extension"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_trigger_advisor_refresh_is_wired():
    """Bug 1 regression. The debounced push-refresh hook
    triggerAdvisorRefresh was declared (let triggerAdvisorRefresh = ...)
    and assigned inside startAdvisorPoll but never CALLED, so the HUD
    only updated on the 500ms poll and the robber-target list lagged up
    to a full poll behind a 7 or a knight. Assert the call form (name
    immediately followed by parens, no '=' in between) actually appears,
    i.e. the hook is invoked at least once, not merely defined."""
    src = _read("extension/panel.js")
    calls = re.findall(r"triggerAdvisorRefresh\s*\(\s*\)", src)
    assert calls, (
        "triggerAdvisorRefresh() is never called: the push-refresh hook "
        "is dead, so the HUD only updates on the periodic poll and the "
        "robber list lags behind a 7 / knight")


def test_manifest_drops_unused_permissions():
    """'scripting' (the page-world hook ships as a static content_scripts
    entry, not via the scripting API) and the 'localhost:8765' host grant
    (every client URL is 127.0.0.1) were both requested but never used.
    Unused permissions widen the trust surface and draw Chrome Web Store
    review scrutiny, so keep them out."""
    manifest = json.loads(_read("extension/manifest.json"))
    perms = manifest.get("permissions", [])
    assert "scripting" not in perms, (
        "the 'scripting' permission is unused; drop it for least privilege")
    hosts = manifest.get("host_permissions", [])
    assert not any("localhost" in h for h in hosts), (
        "the localhost host permission is unused; the bridge URLs are "
        "all 127.0.0.1")


def test_manifest_description_within_cws_limit():
    """The Chrome Web Store rejects an upload whose manifest 'description'
    exceeds 132 characters (the field doubles as the listing summary). A
    243-char description slipped through once and bounced the first upload,
    so pin the limit here."""
    desc = json.loads(_read("extension/manifest.json"))["description"]
    assert len(desc) <= 132, (
        f"manifest description is {len(desc)} chars; CWS caps it at 132")


def test_no_console_log_in_page_and_content_scripts():
    """inject.js runs in the colonist.io page's own console and content.js
    in the content-script context; an unconditional console.log there is
    release noise the user sees. Error reporting via console.warn/error is
    fine; keep these two files quiet otherwise."""
    for name in ("inject.js", "content.js"):
        src = _read(f"extension/{name}")
        assert "console.log" not in src, (
            f"{name} has an unconditional console.log (release noise in a "
            f"user-visible console); use console.warn/error or remove it")


def test_versions_are_in_sync():
    """extension/manifest.json, pyproject.toml, and the top CHANGELOG
    heading must all carry the same version. They drifted badly
    (0.37.36 / 0.37.0 / 0.38.0) before this guard, so a zip built from
    the manifest shipped a different number than the changelog
    documented."""
    manifest_v = json.loads(_read("extension/manifest.json"))["version"]

    m = re.search(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"),
                  re.MULTILINE)
    assert m, "no version in pyproject.toml"
    pyproject_v = m.group(1)

    m2 = re.search(r'^##\s*v([0-9]+\.[0-9]+\.[0-9]+)', _read("CHANGELOG.md"),
                   re.MULTILINE)
    assert m2, "no version heading in CHANGELOG.md"
    changelog_v = m2.group(1)

    # The bridge reports catanbot.__version__ in its /advisor snapshot so
    # the extension can flag an outdated bridge app, so it must match too.
    m3 = re.search(r'__version__\s*=\s*"([^"]+)"',
                   _read("src/catanbot/__init__.py"))
    assert m3, "no __version__ in src/catanbot/__init__.py"
    package_v = m3.group(1)

    assert manifest_v == pyproject_v == changelog_v == package_v, (
        f"version desync: manifest={manifest_v}, "
        f"pyproject={pyproject_v}, changelog={changelog_v}, "
        f"package={package_v}")


def test_inpage_hud_contract_is_wired():
    """The in-page HUD (loghud.js) and content.js share one isolated world
    via window.__catanbot: content.js owns the selector + re-anchor hooks,
    loghud.js owns the renderer. This seam is easy to break in a later edit
    (rename a hook on one side, forget the other), so lock it statically."""
    manifest = json.loads(_read("extension/manifest.json"))
    colonist = next(
        e for e in manifest["content_scripts"]
        if "content.js" in e.get("js", []))
    assert colonist["js"][:3] == ["content.js", "overlay.js", "loghud.js"], (
        "colonist content_scripts must load content.js, overlay.js, "
        f"loghud.js in that order (one shared world); got {colonist['js']}")

    content = _read("extension/content.js")
    loghud = _read("extension/loghud.js")

    # content.js owns + exposes the selectors and calls the re-anchor hook.
    assert "function findLogContainer" in content
    assert "window.__catanbot.findLogContainer" in content
    assert "window.__catanbot.ensureHudAttached" in content, (
        "content.js must call the HUD's ensureHudAttached re-anchor hook "
        "from its observer/interval")

    # loghud.js sets the hook and reads the selectors content.js exposes.
    assert "window.__catanbot.ensureHudAttached" in loghud
    assert "window.__catanbot.findLogContainer" in loghud

    # both sides MUST agree on the streamer skip flag, or content.js's
    # username sweep would rewrite the HUD's opponent name pills.
    assert "'cataanonymized'" in content
    assert "'cataanonymized'" in loghud, (
        "loghud must stamp its nodes with content.js's streamer flag "
        "'cataanonymized' so the username sweep skips them")

    # opt-in, default off (it must not disrupt until enabled).
    assert "catanbot.log_hud" in loghud


def test_loghud_fetches_via_background_worker():
    """loghud.js must NOT fetch the bridge directly: a content script's
    http://127.0.0.1 request from the https colonist page is blocked in some
    browsers (Comet: ERR_BLOCKED_BY_CLIENT). It routes through the background
    service worker's 'get-advisor' handler, which has the host permission and
    isn't page-blocked. Lock both ends of that contract."""
    loghud = _read("extension/loghud.js")
    background = _read("extension/background.js")
    assert "get-advisor" in loghud, (
        "loghud must request the snapshot via the 'get-advisor' message")
    assert "chrome.runtime.sendMessage" in loghud
    assert "get-advisor" in background, (
        "background.js must handle the 'get-advisor' message and fetch "
        "/advisor itself (the in-page fetch is blocked in Comet)")
    assert "http://127.0.0.1:8765" not in loghud, (
        "loghud must not fetch the bridge directly; route through background")


def test_histogram_scale_excludes_seven():
    """Dice-stats regression. The roll histogram's bar-scaling max must
    exclude the 7 column. 7 is the single most probable total (6/36), so
    leaving it in max pinned the scale and squashed every other bar into
    the bottom of the chart. The fix skips n===7 in the max loop and
    clamps the now-possibly-overflowing 7 bar to 100%."""
    src = _read("extension/panel.js")
    assert re.search(r"if \(n === 7\) continue;", src), (
        "renderHistogram's max loop must skip n===7 so the 7 column "
        "does not pin the bar scale")
    assert "Math.min(100, pct)" in src, (
        "the histogram bar height must be clamped to 100% (with 7 out "
        "of max, the 7 count can exceed it)")
