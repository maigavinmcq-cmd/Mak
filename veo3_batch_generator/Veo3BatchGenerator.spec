# -*- mode: python ; coding: utf-8 -*-

excluded_optional_modules = [
    "av",
    "bokeh",
    "cv2",
    "fsspec",
    "IPython",
    "jinja2",
    "llvmlite",
    "matplotlib",
    "numba",
    "onnxruntime",
    "PIL",
    "pyarrow",
    "PyQt5",
    "PyQt6",
    "pytest",
    "scipy",
    "sklearn",
    "sqlalchemy",
    "sympy",
    "tables",
    "tensorflow",
    "tkinter",
    "torch",
    "torchaudio",
    "torchvision",
]

a = Analysis(
    ["main_qt.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/app_icon.ico", "assets"),
        ("assets/app_icon.png", "assets"),
        ("assets/app_icon.svg", "assets"),
    ],
    hiddenimports=[
        "openpyxl.cell._writer",
        "openpyxl.styles",
        "openpyxl.worksheet._reader",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_optional_modules,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Veo3BatchGenerator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/app_icon.ico",
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
    name="Veo3BatchGenerator",
)
