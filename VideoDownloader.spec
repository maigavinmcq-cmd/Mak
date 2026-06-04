# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = []
hiddenimports = ['brotli', 'mutagen', 'websockets', 'charset_normalizer', 'idna', 'urllib3']
datas += collect_data_files('yt_dlp')
datas += collect_data_files('certifi')
hiddenimports += collect_submodules('yt_dlp')


a = Analysis(
    ['vedio.py'],
    pathex=[],
    binaries=[('C:\\ProgramApply\\ffmpeg-7.1.1-essentials_build\\bin\\ffmpeg.exe', 'ffmpeg'), ('C:\\ProgramApply\\ffmpeg-7.1.1-essentials_build\\bin\\ffmpeg.exe', 'ffmpeg')],
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
    name='VideoDownloader',
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
    name='VideoDownloader',
)
