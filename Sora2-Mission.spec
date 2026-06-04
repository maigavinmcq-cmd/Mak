# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path.cwd()
sora_root = project_root / 'Sora2-mission'

datas = [
    (str(sora_root / 'xv_gui' / 'provider_models.json'), 'xv_gui'),
]

optional_data_files = [
    ('config.enc', '.'),
    ('config.salt', '.'),
    ('tasks.json', '.'),
    ('task_history.json', '.'),
    ('machine.allow', '.'),
    ('.key_saved.marker', '.'),
]

for rel_path, target_dir in optional_data_files:
    full_path = sora_root / rel_path
    if full_path.exists():
        datas.append((str(full_path), target_dir))

a = Analysis(
    ['Sora2-mission\\main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['xv_gui.ui_app', 'xv_gui.settings', 'xv_gui.utils', 'xv_gui.billing', 'xv_gui.downloader', 'xv_gui.gate', 'xv_gui.models', 'xv_gui.persistence', 'xv_gui.crypto_store', 'providers.apiyi', 'providers.lingke', 'providers.xintian', 'providers.baoyouhuyu', 'xv_gui.providers.baoyouhuyu', 'PIL', 'PIL.Image', 'PIL.ImageOps'],
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
    name='Sora2-Mission',
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
    name='Sora2-Mission',
)
