# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['Sora2_video_downloader.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\ProgramApply\\ffmpeg-7.1.1-essentials_build\\bin\\ffmpeg.exe', 'ffmpeg')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'tkinter.test'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SoraDownloaderPro',
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
    name='SoraDownloaderPro',
)
