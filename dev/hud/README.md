# HUD dev harness

Render the real `extension/panel.js` against captured game snapshots,
headless, with no live colonist game and no bridge. Used to iterate on
HUD layout with before/after screenshots.

## Usage

1. Generate fixtures from the committed WS captures (one time, or after
   the snapshot shape changes):

   ```
   PYTHONPATH=src python3 dev/hud/dump_hud_fixtures.py
   ```

   This replays the captures through the bridge's own
   `_build_advisor_snapshot` and writes `dev/hud/fixtures/<scenario>.json`
   (gitignored).

2. Serve the repo with no-store caching so edits show on reload:

   ```
   python3 dev/hud/serve.py 8771
   ```

3. Open `http://127.0.0.1:8771/dev/hud/harness.html?fixture=midgame`
   (or `?fixture=early`). The harness stubs the small `chrome.*` surface
   and `fetch`, then loads the unmodified panel; the `/advisor` poll
   resolves to the chosen fixture.

Everything here is dev-only and is not part of the shipped extension.
