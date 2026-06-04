# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata, collect_all


project_root = Path.cwd()
veo_root = project_root / "Veo3"
sora_root = project_root / "Sora2-mission"

datas = [
    (str(veo_root / "Veo3Generated.py"), "Veo3"),
    (str(sora_root / "xv_gui" / "providers" / "oss_uploader.py"), "Sora2-mission/xv_gui/providers"),
]

optional_data_files = [
    (sora_root / "oss_config.py", "Sora2-mission"),
    (project_root / ".env", "."),
]

for full_path, target_dir in optional_data_files:
    if full_path.exists():
        datas.append((str(full_path), target_dir))

datas += collect_data_files("streamlit")
datas += copy_metadata("streamlit")

# 收集 PySide6 和 WebEngine 的所有依赖（DLL、资源文件等）
pyside6_datas, pyside6_binaries, pyside6_hiddenimports = collect_all("PySide6")
datas += pyside6_datas

# 收集 shiboken6（PySide6 的底层绑定库）
shiboken_datas, shiboken_binaries, shiboken_hiddenimports = collect_all("shiboken6")
datas += shiboken_datas

binaries = pyside6_binaries + shiboken_binaries

hiddenimports = [
    "streamlit.web.bootstrap",
    "streamlit.web.cli",
    "streamlit.components.v1.components",
    "streamlit.runtime.scriptrunner.magic_funcs",
    "streamlit.web.server.websocket_headers",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtNetwork",
    "PySide6.QtOpenGL",
    "PySide6.QtPositioning",
    "PySide6.QtPrintSupport",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtWebChannel",
    "shiboken6",
    "requests",
    "tenacity",
    "oss2",
    "dotenv",
    "certifi",
    "urllib3",
]
hiddenimports += pyside6_hiddenimports + shiboken_hiddenimports

excludes = [
    "torch",
    "torchaudio",
    "torchvision",
    "tensorflow",
    "onnxruntime",
    "scipy",
    "numba",
    "matplotlib",
    "sympy",
    "pyarrow",
    "av",
    "cv2",
    "sklearn",
    "IPython",
    "jupyter",
    "webview",
    "pythonnet",
    "clr_loader",
]

a = Analysis(
    ["Veo3\\Veo3Launcher.py"],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Veo3Portable",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    upx=True,
    upx_exclude=[],
    name="Veo3Portable",
)
