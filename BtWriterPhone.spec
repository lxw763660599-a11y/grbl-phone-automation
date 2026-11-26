# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
# Optional: an external phone_automation package that replaces the bundled camera.
CAMERA_ENV = os.environ.get("PHONE_AUTOMATION_HOME", "").strip()
CAMERA_REPO = Path(CAMERA_ENV) if CAMERA_ENV else None
datas = []
binaries = []
hiddenimports = [
    "serial.tools.list_ports_windows",
    "mcp.server.mcpserver",
    "mcp.server.streamable_http",
]
pathex = [str(ROOT)]
if CAMERA_REPO and CAMERA_REPO.is_dir():
    pathex.append(str(CAMERA_REPO))
    hiddenimports.append("phone_automation.vision")

for package in ("mcp", "mcp_types", "uvicorn", "cv2", "PIL", "yaml"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [str(ROOT / "btwriter_app.py")],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["rapidocr", "onnxruntime", "torch"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BtWriterPhone",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BtWriterPhone",
)
