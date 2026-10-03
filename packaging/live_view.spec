# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: 单文件 exe（双击即用，全部功能）
# 用法（在 01-dashcam\03-工具 目录下，用 ocl_env 的 python）：
#   & ocl_env\Scripts\python.exe -m PyInstaller ..\dist\worldcamera-dashcam\packaging\live_view.spec --distpath ..\dist\worldcamera-dashcam\dist
# 产物: ..\dist\worldcamera-dashcam\dist\live_view.exe

import os
HERE = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)) + '/..')  # 03-工具

a = Analysis(
    [r'C:\Users\MarbleVessel\3D Objects\DSH\01-dashcam\03-工具\live_view.py'],
    pathex=[],
    binaries=[
        # chroma / crop / 平均合成 依赖
        ('C:\\Users\\MarbleVessel\\3D Objects\\DSH\\01-dashcam\\03-工具\\chroma_restore.py',),
        ('C:\\Users\\MarbleVessel\\3D Objects\\DSH\\01-dashcam\\03-工具\\usb_desc.py',),
    ],
    datas=[],
    hiddenimports=[
        'numpy',
        'PIL',
        'PIL.Image',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # OpenCL 版太大（pyopencl + numpy 编译产物），不塞进 exe；
        # GPU 加速留给源码版（用 ocl_env 跑 .py 即可）。
        'pyopencl',
        'pytools',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.zipped_data,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='live_view',
    debug=False,
    strip=False,
    upx=False,          # GT730 机器上 UPX 偶尔触发杀软误报，关掉最稳
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # 保留控制台：能看到帧率/错误信息；GUI 静默模式可改 False
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
