# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


rapid_datas, rapid_binaries, rapid_hiddenimports = collect_all(
    "rapidocr_onnxruntime"
)


a = Analysis(
    ["rename_gui.py"],
    pathex=[],
    binaries=rapid_binaries,
    datas=rapid_datas,
    hiddenimports=rapid_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "onnx",
        "onnxruntime.datasets",
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "onnxruntime.transformers",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CHATIBE_Rename_Tool",
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CHATIBE_Rename_Tool",
)
