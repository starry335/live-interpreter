# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


ROOT = Path(SPECPATH)
ANACONDA_BIN = Path(sys.base_prefix) / 'Library' / 'bin'


a = Analysis(
    ['live_interpreter_backend.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=collect_data_files('certifi'),
    hiddenimports=['certifi', 'websocket', 'tkinter', 'tkinter.font'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['funasr', 'torch', 'transformers'],
    noarchive=False,
    optimize=0,
)

if (ANACONDA_BIN / 'tcl86t.dll').exists() and (ANACONDA_BIN / 'tk86t.dll').exists():
    a.binaries = [
        item for item in a.binaries
        if item[0].lower() not in {'tcl86t.dll', 'tk86t.dll'}
    ]
    a.binaries += [
        ('tcl86t.dll', str(ANACONDA_BIN / 'tcl86t.dll'), 'BINARY'),
        ('tk86t.dll', str(ANACONDA_BIN / 'tk86t.dll'), 'BINARY'),
    ]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LiveInterpreterBackend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
