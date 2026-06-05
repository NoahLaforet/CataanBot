# -*- mode: python ; coding: utf-8 -*-
"""Self-contained CatanBot menu-bar .app.

Builds ONE windowed binary from bin/tray_entry.py that is both the rumps
menu-bar tray and (re-run with --run-bridge) the FastAPI bridge. Bundles
catanbot + catanatron + uvicorn + rumps/pyobjc so a friend can drag the
.app to /Applications and launch it with no repo, no venv, no Python.

  PYTHON=.venv/bin/python ./bin/build-app-bundle.sh   # wrapper that also
                                                      # makes the .icns
  # or directly:
  .venv/bin/pyinstaller --noconfirm --clean catanbot-tray.spec
"""
import glob

from PyInstaller.utils.hooks import collect_all, collect_submodules

# uvicorn resolves its loop/http/ws implementations at runtime via
# conditional imports PyInstaller's static analysis misses (same list as
# catanbot-bridge.spec), so they are declared explicitly.
hiddenimports = [
    'uvicorn.logging',
    'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.protocols.websockets.wsproto_impl',
    'uvicorn.lifespan.on',
    # rumps + the pyobjc frameworks it pulls in for the menu-bar item.
    'rumps', 'objc', 'Foundation', 'AppKit', 'CoreFoundation',
    'PyObjCTools', 'PyObjCTools.AppHelper',
]
hiddenimports += collect_submodules('catanbot')
hiddenimports += collect_submodules('catanatron')
hiddenimports += collect_submodules('rumps')

datas = []
binaries = []
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Menu-bar status icons (catanbot/tray/assets/*.png), loaded at runtime by
# app.py via Path(__file__).parent/"assets". collect_data_files('catanbot')
# silently finds nothing under an editable install (the .pth gets skipped by
# site.py on macOS), which is why the frozen app fell back to the emoji dot;
# glob the source tree explicitly so the frames always land in the bundle.
datas += [(p, 'catanbot/tray/assets')
          for p in glob.glob('src/catanbot/tray/assets/*.png')]


a = Analysis(
    ['bin/tray_entry.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CatanBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # windowed: no terminal, menu-bar agent
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CatanBot',
)

app = BUNDLE(
    coll,
    name='CatanBot.app',
    icon='build/CatanBot.icns',
    bundle_identifier='io.colonist.catanbot.tray',
    info_plist={
        'CFBundleName': 'CatanBot',
        'CFBundleDisplayName': 'CatanBot',
        'CFBundleShortVersionString': '0.44.1',
        'CFBundleVersion': '0.44.1',
        # Menu-bar agent: shows + double-clicks in Finder, no Dock icon.
        'LSUIElement': True,
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
    },
)
