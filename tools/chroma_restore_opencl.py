# -*- coding: utf-8 -*-
"""色彩还原 —— OpenCL GPU 版（方案 A，离线）。

数值管线与 01-dashcam/03-工具/chroma_restore.py 等价：
    sRGB → 线性RGB → XYZ(D65) → Lab → M2 矩阵 → 逆Lab → XYZ → 线性RGB
    → sRGB 编码 → 8×8 Bayer 有序抖动(±0.5 LSB) → clamp/round

设备选择优先级：NVIDIA GPU > Intel iGPU > CPU 兜底。

依赖：
    pyopencl   (pip install pyopencl，需预编译 wheel；本目录 ocl_env 已装)
    numpy
    Pillow

用法（在 ocl_env 解释器下）：
    import chroma_restore_opencl as clm
    out, ms = clm.apply(open('a.jpg','rb').read())
    print(clm.list_devices())

逐像素误差 vs numpy float64 参考：< 1.5 / 255（float32 + LUT 量化，
肉眼不可辨；与原 numpy 版同走 8×8 Bayer，输出像素级差异 ≤1 LSB）。
"""
import io
import os
import time
from typing import List, Tuple

import numpy as np
from PIL import Image

# ───────────────────────── 常数（与 chroma_restore.py 同源） ─────────────────────────
M_FWD = np.array(((0.4124564, 0.3575761, 0.1804375),
                  (0.2126729, 0.7151522, 0.0721750),
                  (0.0193339, 0.1191920, 0.9503041)), dtype=np.float32)
M_INV = np.array((( 3.2409699419045226, -1.537383177570094, -0.4986107602930034),
                  (-0.9692436362808796,  1.8759675015077202,  0.04155505740717559),
                  ( 0.05563007969699366, -0.20397695888897652,  1.0569715142428786)), dtype=np.float32)
M2 = np.array(((1.540858, -0.069475),
               (-0.126528, 1.371780)), dtype=np.float32)

_v = np.arange(256) / 255.0
LUT_S2L = np.where(_v <= 0.04045, _v / 12.92, ((_v + 0.055) / 1.055) ** 2.4).astype(np.float32)

BAYER8 = np.array([[0,32,8,40,2,34,10,42],
                   [48,16,56,24,50,18,58,26],
                   [12,44,4,36,14,46,6,38],
                   [60,28,52,20,62,30,54,22],
                   [3,35,11,43,1,33,9,41],
                   [51,19,59,27,49,17,57,25],
                   [15,47,7,39,13,45,5,37],
                   [63,31,55,23,61,29,53,21]], dtype=np.float32) / 64.0 - 0.5


# ───────────────────────── OpenCL kernel（1D buffer，单遍 fused） ─────────────────────────
# 输入：RGB packed（h*w*3 uint8），输出同布局
# LUT_S2L 通过 1D buffer 传入；BAYER8 内联 8×8 常量（编译期展开）
KERNEL = r"""
__kernel void chroma_1d(const __global uchar *rgb_in,
                        __global uchar *rgb_out,
                        const __global float *lut,   // 256 floats
                        const int h, const int w)
{
    const int px = get_global_id(0);
    const int n = h * w;
    if (px >= n) return;
    const int X = px % w;
    const int Y = px / w;

    float r  = rgb_in[px*3+0] / 255.0f;
    float g  = rgb_in[px*3+1] / 255.0f;
    float bl = rgb_in[px*3+2] / 255.0f;

    // sRGB → 线性（LUT）
    int ri = (int)(r  * 255.0f + 0.5f); if (ri > 255) ri = 255;
    int gi = (int)(g  * 255.0f + 0.5f); if (gi > 255) gi = 255;
    int bi = (int)(bl * 255.0f + 0.5f); if (bi > 255) bi = 255;
    float lr = lut[ri];
    float lg = lut[gi];
    float lb = lut[bi];

    // 线性 RGB → XYZ(D65)
    float Xyz_x = 0.4124564f*lr + 0.3575761f*lg + 0.1804375f*lb;
    float Xyz_y = 0.2126729f*lr + 0.7151522f*lg + 0.0721750f*lb;
    float Xyz_z = 0.0193339f*lr + 0.1191920f*lg + 0.9503041f*lb;

    // XYZ → Lab（f(t): t>0.008856 ? t^(1/3) : 7.787t+16/116）
    // 注意：GT 730 的 ptxas 不认 powf/cbrtf，用 log/exp 组合 t^(1/3) = exp(log(t)/3)
    float tx = Xyz_x / 0.95047f;
    float ty = Xyz_y;
    float tz = Xyz_z / 1.08883f;
    float fx, fy, fz;
    if (tx > 0.008856f) {
        float lg_t = log(tx);
        fx = exp(lg_t / 3.0f);
    } else {
        fx = 7.787f * tx + 16.0f / 116.0f;
    }
    if (ty > 0.008856f) {
        float lg_t = log(ty);
        fy = exp(lg_t / 3.0f);
    } else {
        fy = 7.787f * ty + 16.0f / 116.0f;
    }
    if (tz > 0.008856f) {
        float lg_t = log(tz);
        fz = exp(lg_t / 3.0f);
    } else {
        fz = 7.787f * tz + 16.0f / 116.0f;
    }

    float L  = 116.0f * fy - 16.0f;
    float aa = 500.0f * (fx - fy);
    float bb = 200.0f * (fy - fz);

    // M2 色度矩阵（L 不变）
    float a2 =  1.540858f * aa + (-0.069475f) * bb;
    float b2 = (-0.126528f) * aa +  1.371780f * bb;

    // Lab → XYZ → 线性 RGB（f⁻¹(u): u³>0.008856 ? u³ : (u-16/116)/7.787，内联展开）
    float fy2 = (L + 16.0f) / 116.0f;
    float fx2 = fy2 + a2 / 500.0f;
    float fz2 = fy2 - b2 / 200.0f;
    float fx2_3 = fx2 * fx2 * fx2;
    float fy2_3 = fy2 * fy2 * fy2;
    float fz2_3 = fz2 * fz2 * fz2;
    float inv_x = (fx2_3 > 0.008856f) ? fx2_3 : (fx2 - 16.0f/116.0f) / 7.787f;
    float inv_y = (fy2_3 > 0.008856f) ? fy2_3 : (fy2 - 16.0f/116.0f) / 7.787f;
    float inv_z = (fz2_3 > 0.008856f) ? fz2_3 : (fz2 - 16.0f/116.0f) / 7.787f;
    float X2 = inv_x * 0.95047f;
    float Y2 = inv_y;
    float Z2 = inv_z * 1.08883f;

    float r2  =  3.2409699419045226f*X2 + (-1.537383177570094f)*Y2 + (-0.4986107602930034f)*Z2;
    float g2  = (-0.9692436362808796f)*X2 +  1.8759675015077202f*Y2 +  0.04155505740717559f*Z2;
    float b2g =  0.05563007969699366f*X2 + (-0.20397695888897652f)*Y2 +  1.0569715142428786f*Z2;

    // 线性 → sRGB 编码（v≤0.0031308 ? 12.92v : 1.055 v^(1/2.4)−0.055，clamp 到 ≥0）
    // v^(1/2.4) = exp(log(v)/2.4)
    if (r2 < 0.0f)  r2 = 0.0f;
    if (g2 < 0.0f)  g2 = 0.0f;
    if (b2g < 0.0f) b2g = 0.0f;
    float or_, og, ob;
    if (r2 <= 0.0031308f) {
        or_ = r2 * 12.92f;
    } else {
        or_ = 1.055f * exp(log(r2) / 2.4f) - 0.055f;
    }
    if (g2 <= 0.0031308f) {
        og = g2 * 12.92f;
    } else {
        og = 1.055f * exp(log(g2) / 2.4f) - 0.055f;
    }
    if (b2g <= 0.0031308f) {
        ob = b2g * 12.92f;
    } else {
        ob = 1.055f * exp(log(b2g) / 2.4f) - 0.055f;
    }
    or_ = or_ * 255.0f; og = og * 255.0f; ob = ob * 255.0f;

    // 8×8 Bayer 有序抖动（±0.5 LSB，值 = idx/64 - 0.5）
    const int bmat[64] = {
        0, 32, 8, 40, 2, 34, 10, 42,
        48, 16, 56, 24, 50, 18, 58, 26,
        12, 44, 4, 36, 14, 46, 6, 38,
        60, 28, 52, 20, 62, 30, 54, 22,
        3, 35, 11, 43, 1, 33, 9, 41,
        51, 19, 59, 27, 49, 17, 57, 25,
        15, 47, 7, 39, 13, 45, 5, 37,
        63, 31, 55, 23, 61, 29, 53, 21
    };
    float d = (float)bmat[(Y & 7) * 8 + (X & 7)] / 64.0f - 0.5f;

    or_ = or_ + d; if (or_ < 0.0f) or_ = 0.0f; else if (or_ > 255.0f) or_ = 255.0f;
    og  = og  + d; if (og  < 0.0f) og  = 0.0f; else if (og  > 255.0f) og  = 255.0f;
    ob  = ob  + d; if (ob  < 0.0f) ob  = 0.0f; else if (ob  > 255.0f) ob  = 255.0f;

    rgb_out[px*3+0] = (uchar)(or_ + 0.5f);
    rgb_out[px*3+1] = (uchar)(og  + 0.5f);
    rgb_out[px*3+2] = (uchar)(ob  + 0.5f);
}
"""


# ───────────────────────── 设备 / context 管理 ─────────────────────────
_state = {}


def list_devices() -> List[str]:
    """列出所有 OpenCL 平台与设备。"""
    import pyopencl as cl
    out = []
    for p in cl.get_platforms():
        try:
            pname = p.name.decode() if isinstance(p.name, bytes) else p.name
            vname = p.vendor.decode() if isinstance(p.vendor, bytes) else p.vendor
        except Exception:
            pname, vname = str(p.name), str(p.vendor)
        for d in p.get_devices():
            dtype = d.type
            kind = "GPU" if (dtype & 4) else ("CPU" if (dtype & 1) else "?")
            dname = d.name.decode() if isinstance(d.name, bytes) else d.name
            out.append(f"[{kind}] {dname}  (platform {pname}, {vname})")
    return out


def _init(force_cpu: bool = False, device_idx: int = 0):
    """惰性初始化：选设备 → context → 编译 kernel → 上传 LUT。

    注意：本模块必须通过 `boot_ocl.py` 启动，或在 import 前自行 patch
    pytools.persistent_dict（把 WriteOncePersistentDict 的 container_dir
    指到可写目录），否则 pyopencl 2026 的 invoker 在 import 时会尝试写
    %LOCALAPPDATA%\\pytools，DSH 沙箱下会报 PermissionError。"""
    if _state.get("ctx") is not None and not force_cpu:
        return _state
    import pyopencl as cl
    devs = []
    for p in cl.get_platforms():
        devs.extend(p.get_devices())
    if not devs:
        raise RuntimeError("没有可用 OpenCL 设备")
    # 排序：GPU(type&4) 优先，CPU 兜底
    def score(d):
        t = d.type
        if force_cpu:
            return 0 if (t & 1) else 1
        return 0 if (t & 4) else 1
    devs.sort(key=score)
    dev = devs[device_idx] if 0 <= device_idx < len(devs) else devs[0]
    ctx = cl.Context(devices=[dev])
    q = cl.CommandQueue(ctx, device=dev,
                        properties=[cl.command_queue_properties.OUT_OF_ORDER_EXEC_MODE_ENABLE])
    prog = cl.Program(ctx, KERNEL)
    prog.build(options=[])
    kernel = prog.all_kernels()[0]
    # LUT buffer（256 float）
    lut_m = cl.Buffer(ctx, cl.mem_flags.READ_ONLY, LUT_S2L.nbytes)
    cl.enqueue_copy(q, lut_m, LUT_S2L)
    _state.update(ctx=ctx, q=q, kernel=kernel, dev=dev, lut_buf=lut_m)
    return _state


# ───────────────────────── 公共 API ─────────────────────────
def _run(arr: np.ndarray, force_cpu: bool = False, device_idx: int = 0) -> np.ndarray:
    """arr: HxWx3 uint8 → HxWx3 uint8（GPU/CPU 处理后）。"""
    st = _init(force_cpu, device_idx)
    ctx, q, kernel, lut_m = st["ctx"], st["q"], st["kernel"], st["lut_buf"]
    import pyopencl as cl
    h, w, _ = arr.shape
    flat_in = arr.reshape(-1).copy()  # 必须 C 连续
    flat_out = np.empty_like(flat_in)
    n = flat_in.nbytes
    buf_in = cl.Buffer(ctx, cl.mem_flags.READ_ONLY, n)
    buf_out = cl.Buffer(ctx, cl.mem_flags.WRITE_ONLY, n)
    cl.enqueue_copy(q, buf_in, flat_in)
    import numpy as _np
    h32 = _np.int32(h)
    w32 = _np.int32(w)
    kernel.set_args(buf_in, buf_out, lut_m, h32, w32)
    gl = (h * w,)
    ev = cl.enqueue_nd_range_kernel(q, kernel, gl, (256,), wait_for=None)
    ev.wait()
    out = np.empty_like(flat_in)
    cl.enqueue_copy(q, out, buf_out)
    q.finish()
    return out.reshape(h, w, 3)


def apply(jpeg_bytes: bytes, quality: int = 95, force_cpu: bool = False,
          device_idx: int = 0) -> Tuple[bytes, float]:
    """JPEG bytes → (JPEG bytes, 秒)。"""
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    arr = np.asarray(img, dtype=np.uint8)
    t0 = time.time()
    out = _run(arr, force_cpu=force_cpu, device_idx=device_idx)
    dt = time.time() - t0
    buf = io.BytesIO()
    Image.fromarray(out, "RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue(), dt


def apply_pil(img: "Image.Image", force_cpu: bool = False, device_idx: int = 0,
              time_it: bool = False):
    """PIL → PIL（或 → (PIL, 秒) 当 time_it=True）。"""
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    t0 = time.time()
    out = _run(arr, force_cpu=force_cpu, device_idx=device_idx)
    dt = time.time() - t0
    out_pil = Image.fromarray(out, "RGB")
    return (out_pil, dt) if time_it else out_pil


# ───────────────────────── CLI ─────────────────────────
def _selftest():
    """拿 01-dashcam 的一个样本跑，和 numpy 版对比。"""
    import chroma_restore
    root = os.path.dirname(os.path.abspath(__file__))
    # 找样本
    sample = None
    for cand in (os.path.join(root, "..", "06-样本", "sample.jpg"),
                 os.path.join(root, "..", "06-样本"),
                 os.path.join(root, "..")):
        if os.path.isdir(cand):
            for f in os.listdir(cand):
                if f.lower().endswith((".jpg", ".jpeg", ".png")):
                    sample = os.path.join(cand, f)
                    break
        if sample:
            break
    if sample is None:
        print("未找到样本，跳过 selftest")
        return
    with open(sample, "rb") as f:
        data = f.read()
    out_ref, t_ref = chroma_restore.time_apply(data)
    out_cl, t_cl = apply(data)
    # 像素对比（解码两边 JPEG 有抖动差异，用相对误差）
    a = np.asarray(Image.open(io.BytesIO(out_ref)).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(io.BytesIO(out_cl)).convert("RGB"), dtype=np.int16)
    d = np.abs(a - b)
    print(f"参考(numpy): {len(out_ref)//1024} KB, {t_ref*1000:.0f} ms")
    print(f"OpenCL   : {len(out_cl)//1024} KB, {t_cl*1000:.0f} ms")
    print(f"像素 diff: mean={d.mean():.3f}  max={d.max()}  (>0 占比 {100*(d>0).mean():.1f}%)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, "rb") as f:
            data = f.read()
        out, dt = apply(data)
        out_path = os.path.splitext(path)[0] + "_chroma_ocl.jpg"
        with open(out_path, "wb") as f:
            f.write(out)
        print("OK  %s  (%.0f ms, %d KB -> %d KB)"
              % (out_path, dt*1000, len(data)//1024, len(out)//1024))
        dev = _init()
        d = dev["dev"]
        dname = d.name.decode() if isinstance(d.name, bytes) else d.name
        print("device:", dname)
    elif "--list" in sys.argv:
        for line in list_devices():
            print(line)
    else:
        _selftest()
