# Bridge distribution — how to make "install extension" mean
# "extension works"

The current install story is two steps:

1. Install the Chrome extension (one click once it's on the CWS).
2. `git clone` + `python3 -m venv .venv` + `pip install -e .` + `./bin/catanbot live` to run the Python bridge on `127.0.0.1:8765`.

Step 2 is a wall. Anyone who installs the extension without
already being able to run a Python venv from the command line
gets a broken HUD and probably uninstalls. This doc enumerates
the integration paths and recommends a sequence.

---

## The constraint that shapes everything

The bridge does **real work** every poll:

- Maintains a catanatron `Game` object replaying every
  WebSocket frame.
- Runs the recommender (1-ply state-eval search × 4-10
  candidates per snap, ~10ms each).
- Runs the strategy selector + pivot triggers.
- Runs robber-target scoring across every land tile.
- Renders post-mortems with embedded matplotlib charts.

This is **CPU-and-memory-meaningful Python**, not a thin proxy.
The catanatron dependency alone is a ~30MB install with C
extensions. There's no realistic way to make this "free" for a
user without either (a) running it on the user's machine, or
(b) running it on someone else's machine and paying the bill.

---

## Five paths, ranked

### Option A — Cloud-host the bridge ❌

Run a hosted instance (Fly.io, Railway, AWS Lambda) that every
extension user connects to. Each browser tab gets a session;
the extension forwards frames to the hosted endpoint instead of
`127.0.0.1`.

**Pros:** zero install for users.

**Cons (any one is disqualifying):**
- **Privacy story breaks.** The current README + privacy
  policy lead with "your game state never leaves your
  machine." Cloud-hosting torpedoes that claim. Streamers
  using the bot would be sending live game state to a
  third party.
- **Cost.** A Catan game runs 30-90 minutes with frequent
  WS frames. Recommender CPU is 5-15ms per poll × 1Hz polls
  × 60 minutes × concurrent users. At even 50 concurrent
  users this is real CPU spend ($20-50/mo on Fly.io
  shared-CPU instances; more if anyone uses it seriously).
- **Per-user state isolation.** Every user's
  `sessions/active.jsonl`, postmortems, autosaves now have
  to live in some per-user storage with a session token. The
  bridge code currently writes to local file paths; would
  need a refactor to S3 or per-user temp dirs.
- **Operational responsibility.** If the cloud bridge goes
  down at 11pm on a Saturday, every active user loses their
  HUD mid-game. This is a hobbyist project; on-call is not
  a thing we're going to do.
- **Compliance overhead.** Hosting third-party game state,
  even pseudonymously, opens GDPR / privacy-of-others
  questions (the opp usernames in the WS frames).

**Verdict:** No. This breaks the value proposition.

---

### Option B — Chrome Native Messaging ✅ (long-term right answer)

Chrome's official path for "extension talks to a process on the
user's machine." See:
https://developer.chrome.com/docs/apps/nativeMessaging

How it works:
- User runs a one-time installer (the bundle from Option F).
- The installer writes a tiny manifest JSON to an OS-specific
  path (e.g. `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.catanbot.bridge.json`)
  pointing at the bundled native binary.
- The extension declares `nativeMessaging` permission and
  calls `chrome.runtime.connectNative('com.catanbot.bridge')`.
- Chrome spawns the binary, communication goes over
  stdin/stdout as JSON length-prefixed messages.
- The binary is a one-shot per session — Chrome manages the
  lifecycle.

**Pros:**
- Single install: one binary, one manifest write. No more
  `git clone` + venv.
- Privacy preserved — bridge runs on user's machine.
- No cloud cost.
- No port-conflict worries (`127.0.0.1:8765` could clash
  with other software).
- Chrome can spawn it on demand instead of the user
  remembering to start it.

**Cons:**
- Engineering work: refactor the bridge from FastAPI/HTTP to
  stdin/stdout JSON-RPC. ~3-5 days of focused work.
- Cross-platform binary packaging (PyInstaller for the
  Python bridge, code-signed mac binary, signed Windows exe).
- The manifest's `allowed_origins` field has to list the
  extension ID, so the extension ID needs to be stable
  (publish-once-then-fix).
- Updates to the bridge require the user to download the
  new installer. (Could be auto-handled inside the binary
  itself, but more work.)

**Verdict:** This is the right answer for v2. Not v1 — too
much work to ship before the CWS submission lands.

---

### Option C — Port the bridge to JavaScript ❌

Re-implement everything (catanatron, recommender, strategy
selector, postmortem renderer) in JS so it runs in the
extension's service worker.

**Pros:** zero install beyond the extension.

**Cons:**
- This is a multi-month rewrite. catanatron alone is a
  decent Python codebase; our recommender / strategy_select
  / advisor stack adds another ~3000 lines of Python.
- Loses the testing infrastructure (pytest, 688 tests).
- Service workers have memory limits (~200MB) and aggressive
  termination — a Game object replaying a long match might
  not survive.
- We're a 1-developer project. This is several quarters of
  full-time work.

**Verdict:** No. Project would be in maintenance mode for
the duration.

---

### Option D — Pyodide WASM ⚠️

Run the existing Python bridge in WebAssembly via Pyodide
inside the extension's service worker.

**Pros:** keeps Python code, runs in browser.

**Cons:**
- Pyodide is a 10-20MB initial download. Acceptable for a
  one-time install but real friction.
- catanatron has C extensions (probably — its `.pyx` files);
  needs a Pyodide-compatible wheel build.
- Pyodide-Python is 2-10× slower than CPython. Recommender
  cycles per snap could exceed the 1Hz poll budget.
- Service worker memory pressure; catanatron's Game state
  is ~5-20MB per instance.
- Pyodide doesn't play nicely with FastAPI/uvicorn's
  asyncio model — would need a major refactor anyway.

**Verdict:** Maybe in 2 years when WASM Python perf and
Pyodide ecosystem mature. Not now.

---

### Option E — One-click installers (no architecture change) ✅ (short-term right answer)

Don't change the architecture. Make the install painless.

PyInstaller can package the Python bridge + venv + catanatron +
all deps into a single self-contained binary per OS. Wrap it
in a platform-native installer:

- **macOS:** `.pkg` installer that drops the binary in
  `/Applications/CatanBot.app`, registers it as a launch
  agent so it starts on login.
- **Windows:** `.exe` installer (NSIS or Inno Setup) that
  installs to `%LOCALAPPDATA%\CatanBot\` and adds a
  startup-folder shortcut.
- **Linux:** `.deb` + `.AppImage` for the two main paths.

The extension UI on first launch detects "no bridge at
127.0.0.1:8765" and shows a friendly modal:

> CatanBot needs the local bridge to work. Download it for
> your platform: [macOS] [Windows] [Linux]

Includes a "test connection" button that verifies the bridge
is reachable.

**Pros:**
- Zero architecture change. Same FastAPI bridge as today.
- Install is 30MB download + double-click. Same effort as
  installing Discord.
- Privacy story unchanged — bridge runs on user's machine.
- Updates: bridge can self-update from GitHub releases via
  a built-in updater (or just have the install be infrequent).
- Ship-able in 1-2 days of build engineering.

**Cons:**
- Two installs (extension + bridge). Still more friction
  than zero-install.
- Cross-platform code signing (mac requires Apple Developer
  certificate, Windows benefits from EV certificate to
  avoid SmartScreen warnings). Mac certs are $99/yr,
  Windows EV is $300+/yr.
- Auto-launch-on-login can feel intrusive; need a clear
  uninstall path.

**Verdict:** This is the right next step. Ships fast, makes
the existing extension actually usable for non-technical
users, doesn't lock us out of moving to Native Messaging
later.

---

### Option F — Better docs only ⚠️ (default, not a real answer)

Polish the README install section, add troubleshooting, ship
the bridge as a homebrew formula and a pip package on PyPI.

**Pros:** truly zero engineering work.

**Cons:**
- Still requires the user to know what a terminal is.
- Doesn't solve the "I clicked Install on CWS and nothing
  happens" UX cliff.

**Verdict:** Worth doing alongside Option E (good docs make
the installer story tighter), but not a substitute.

---

## Recommended sequence

1. **Now → CWS submission (this week):** ship the extension
   unlisted, install instructions point at the manual
   `git clone` + `pip install -e .` route. Audience is
   limited to technical users who can follow that —
   acceptable for an unlisted launch.

2. **Phase 1 (1-2 weeks):** Option E — PyInstaller bundles +
   platform installers. Update the extension to detect
   missing bridge and link to the installer for the user's
   OS. This is the threshold that lets you flip the listing
   public.

3. **Phase 2 (1-2 months):** Option B — refactor bridge to
   Native Messaging. Single install, no port conflict, Chrome
   manages lifecycle. The bundled binary from phase 1 becomes
   the native messaging host.

Phases 1 and 2 are both clean upgrades from the current state;
each step doesn't lock out the next one.

---

## Cost / effort summary

| Path | Eng effort | Recurring cost | Privacy impact | UX win |
|------|------------|----------------|----------------|--------|
| A. Cloud | 1-2 weeks | $20-200/mo + scale risk | major regression | huge |
| B. Native Messaging | 3-5 days | $99/yr (mac signing) | none | huge |
| C. JS port | 3+ months | $0 | none | huge |
| D. Pyodide | 2-4 weeks | $0 | none | meaningful |
| E. One-click installers | 1-2 days | $99/yr (mac signing) | none | large |
| F. Docs only | hours | $0 | none | small |

**The actual call:** E now, B later. Skip A/C/D.
