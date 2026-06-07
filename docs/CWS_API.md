# Publishing the extension to the Chrome Web Store from the command line

The web dev console cannot be automated (Chrome blocks scripting any page
in the extensions gallery), but the Chrome Web Store **API** is not blocked.
After a one-time setup you can ship a release with:

```bash
bin/build-extension-zip.sh
PYTHONPATH=src .venv/bin/python scripts/cws_publish.py
```

That uploads the packaged zip as a new draft and submits it for review, no
dev-console clicking. All credentials live in the gitignored `.env`.

---

## One-time setup (about 10 minutes, your Google account)

You need three things in `.env`: an OAuth **client id + secret** and a
**refresh token**. The store **item id** is already filled in.

### 1. Make a Google Cloud project and enable the API

1. Go to <https://console.cloud.google.com/> and create a project (any
   name, e.g. `catanbot-cws`).
2. APIs & Services > Library > search **Chrome Web Store API** > **Enable**.

### 2. Configure the OAuth consent screen

1. APIs & Services > **OAuth consent screen**.
2. User type **External** (or Internal if you have Workspace) > Create.
3. Fill the required app name + your email, Save and Continue through the
   scopes and test-users steps.
4. On **Test users**, add your own Google account (the one that owns the
   CatanBot store listing). With the app in "Testing" status that is all
   you need; you do not have to verify the app.

### 3. Create the OAuth client

1. APIs & Services > **Credentials** > Create Credentials > **OAuth client
   ID**.
2. Application type: **Desktop app**. Name it anything.
3. Copy the **Client ID** and **Client secret** into `.env`:
   ```
   CWS_CLIENT_ID="...apps.googleusercontent.com"
   CWS_CLIENT_SECRET="..."
   ```
   (Desktop clients auto-allow `http://localhost` redirects, so the helper
   script in step 4 needs no extra redirect-URI config.)

### 4. Get the refresh token

```bash
PYTHONPATH=src .venv/bin/python scripts/cws_get_refresh_token.py
```

It opens a Google consent page, you approve, it captures the redirect on a
local port and prints a line like `CWS_REFRESH_TOKEN=...`. Paste that into
`.env`. The refresh token is long-lived; you only redo this if you revoke
access or rotate the client secret.

---

## Each release

```bash
# bump versions (manifest + pyproject + __init__ + CHANGELOG must agree),
# then:
bin/build-extension-zip.sh
PYTHONPATH=src .venv/bin/python scripts/cws_publish.py
```

- Uploads `dist/catanbot-extension-v{version}.zip` (override with `--zip`).
- Submits for review. A `status` of `OK` or `ITEM_PENDING_REVIEW` means it
  was accepted into review.
- `--no-publish` uploads the draft without submitting. Useful because the
  store will not accept a new submission while a previous version is still
  in review: upload the draft now, then run publish again once the prior
  review clears.

Visibility (unlisted vs public) and the listing fields are set in the dev
console, not by this script; it only ships the package.

## Notes

- `scripts/cws_publish.py` and `scripts/cws_get_refresh_token.py` are stdlib
  only; the only network calls are the standard Google OAuth exchange and
  the CWS upload/publish endpoints.
- Nothing secret is committed: credentials live only in `.env` (gitignored).
- The Mac/Windows app release is separate and still goes through
  `scripts/sign_and_notarize.sh` + a GitHub release (see `SIGNING.md`).
