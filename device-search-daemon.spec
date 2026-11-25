# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

datas = []
binaries = []
hiddenimports = ['device_search', 'device_search.server', 'device_search.cli', 'device_search.store', 'device_search.semantic', 'device_search.vectors', 'device_search.crawl', 'device_search.extract', 'device_search.agent', 'device_search.metadata', 'device_search.runtime']
datas += collect_data_files('fastembed')
datas += collect_data_files('onnxruntime')
binaries += collect_dynamic_libs('onnxruntime')
binaries += collect_dynamic_libs('pypdfium2_raw')
hiddenimports += collect_submodules('pdftext')


a = Analysis(
    ['/var/folders/4b/l1gvzw19249bk8qgnkwgh1cw0000gn/T/ds_entry.0AWvlGKn.py'],
    pathex=['/Users/eseudom/WebstormProjects/device-search'],
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
    a.binaries,
    a.datas,
    [],
    name='device-search-daemon',
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
