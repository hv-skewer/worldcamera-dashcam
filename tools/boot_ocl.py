# -*- coding: utf-8 -*-
"""OpenCL 启动引导：先把 pytools 的缓存目录指到一个可写位置，再导入 chroma_restore_opencl。

用法：
    python boot_ocl.py <input.jpg>          # 处理单张
    python boot_ocl.py --list               # 列 OpenCL 设备
    python boot_ocl.py --compare <a.jpg>    # 对比 numpy 参考版
"""
import os
import sys


def _patch_pytools_cache():
    """让 pytools 的 WriteOncePersistentDict 落到 workspace 内可写的目录。

    DSH 沙箱限制 Python 进程无法写 AppData，所以 monkeypatch
    pytools.persistent_dict._PersistentDictBase.__init__，强制把
    container_dir 指到模块目录下的 .pytools_cache。
    必须在 import pyopencl 之前调用。
    """
    import pytools.persistent_dict as pd
    mod_dir = os.path.dirname(os.path.abspath(__file__))
    fallback = os.path.join(mod_dir, ".pytools_cache")
    os.makedirs(fallback, exist_ok=True)
    _orig = pd._PersistentDictBase.__init__

    def _patched(self, *args, **kwargs):
        kwargs["container_dir"] = fallback
        _orig(self, *args, **kwargs)

    pd._PersistentDictBase.__init__ = _patched
    return fallback


def main():
    _patch_pytools_cache()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import chroma_restore_opencl as clm

    args = [a for a in sys.argv[1:]]
    if not args:
        print("用法: python boot_ocl.py <input.jpg> | --list | --compare <a.jpg>")
        sys.exit(0)

    if args[0] == "--list":
        for line in clm.list_devices():
            print(line)
        return

    if args[0] == "--compare":
        if len(args) < 2:
            print("用法: python boot_ocl.py --compare <input.jpg>")
            sys.exit(1)
        import io
        import chroma_restore
        import numpy as np
        from PIL import Image
        path = args[1]
        with open(path, "rb") as f:
            data = f.read()
        ref, t_ref = chroma_restore.time_apply(data)
        out, t_cl = clm.apply(data)
        a = np.asarray(Image.open(io.BytesIO(ref)).convert("RGB"), dtype=np.int16)
        b = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"), dtype=np.int16)
        d = np.abs(a - b)
        print(f"输入  : {path}")
        print(f"参考  : {len(ref)//1024} KB, {t_ref*1000:.0f} ms (numpy CPU)")
        print(f"OpenCL: {len(out)//1024} KB, {t_cl*1000:.0f} ms")
        print(f"像素差: mean={d.mean():.3f}  max={d.max()}  "
              f"(>0 占比 {100*(d>0).mean():.1f}%)")
        return

    # 普通处理
    path = args[0]
    with open(path, "rb") as f:
        data = f.read()
    out, dt = clm.apply(data)
    out_path = os.path.splitext(path)[0] + "_chroma_ocl.jpg"
    with open(out_path, "wb") as f:
        f.write(out)
    dev = clm._init()
    d = dev["dev"]
    dname = d.name.decode() if isinstance(d.name, bytes) else d.name
    print("OK  %s  (%.0f ms, %d KB -> %d KB)"
          % (out_path, dt * 1000, len(data) // 1024, len(out) // 1024))
    print("device:", dname)


if __name__ == "__main__":
    main()
