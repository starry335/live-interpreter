# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


ROOT = Path(SPECPATH)
ANACONDA_BIN = Path(sys.base_prefix) / 'Library' / 'bin'


a = Analysis(
    ['live_interpreter_launcher.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# PyInstaller may find Tcl/Tk DLLs from unrelated directories on PATH.
# Keep them matched with E:\ANACONDA\Library\lib\tcl8.6 and tk8.6.
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
    [],
    name='LiveInterpreter',
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
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
    name='LiveInterpreter',
)
