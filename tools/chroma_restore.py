# -*- coding: utf-8 -*-
"""色彩还原模块（方案 A，离线）—— 严格按 色彩还原/色彩还原公式.md 第 4 节实现。

数值管线与文档复现代码逐位等价（同样的 255 归一、同样的舍入 int(v+0.5)），
仅把逐像素 for 循环改成 numpy 向量化：
    纯 Python 复现代码  ~10.5 s/帧
    本模块              ~1.2 s/帧   （离线拍照/录完视频处理够用，勿套实时预览）

常量全部来自 色彩还原公式.md：
    M2   : (1.540858, -0.069475; -0.126528, 1.371780)   det=2.10493
    M_FWD: sRGB(D65) → XYZ
    M_INV: XYZ → sRGB
    γ    : sRGB 标准曲线（0.04045 / 0.0031308 分段）
    Lab  : f(t) 标准 CIE 分段（0.008856），白点 0.95047/1.0/1.08883
    Bayer : 文档 8×8 表，/64 - 0.5

用法：
    import chroma_restore
    out = chroma_restore.apply(jpeg_bytes)       # bytes → bytes（q95）
    out = chroma_restore.apply_pil(img)          # PIL → PIL
"""
import io
import time
import numpy as np

# ───────────────────────── 常数（色彩还原公式.md） ─────────────────────────
M2 = ((1.540858, -0.069475), (-0.126528, 1.371780))

M_FWD = ((0.4124564, 0.3575761, 0.1804375),
         (0.2126729, 0.7151522, 0.0721750),
         (0.0193339, 0.1191920, 0.9503041))

M_INV = (( 3.2409699419045226, -1.537383177570094, -0.4986107602930034),
         (-0.9692436362808796,  1.8759675015077202,  0.04155505740717559),
         ( 0.05563007969699366, -0.20397695888897652,  1.0569715142428786))

_v = np.arange(256) / 255.0
S2L = np.where(_v <= 0.04045, _v / 12.92, ((_v + 0.055) / 1.055) ** 2.4)

BAYER8 = np.array([[0,32,8,40,2,34,10,42],
                   [48,16,56,24,50,18,58,26],
                   [12,44,4,36,14,46,6,38],
                   [60,28,52,20,62,30,54,22],
                   [3,35,11,43,1,33,9,41],
                   [51,19,59,27,49,17,57,25],
                   [15,47,7,39,13,45,5,37],
                   [63,31,55,23,61,29,53,21]], dtype=np.float64) / 64.0 - 0.5


def _f(t):
    """CIE Lab f(t)：t>0.008856 ? t^(1/3) : 7.787 t + 16/116"""
    return np.where(t > 0.008856, t ** (1/3), 7.787 * t + 16.0/116.0)


def _fi(u):
    """f⁻¹(u)：u³>0.008856 ? u³ : (u-16/116)/7.787"""
    v = u ** 3
    return np.where(v > 0.008856, v, (u - 16.0/116.0) / 7.787)


def apply_pil(img):
    """PIL.Image(RGB) → PIL.Image(RGB)，方案 A（锁 L* + M2 + 通道截断 + Bayer）。

    数值等价于 色彩还原公式.md 第四节 process() 的逐像素实现。"""
    arr = np.asarray(img.convert("RGB"), dtype=np.int64)   # 保持 0..255，给 LUT 索引用

    # sRGB → 线性 RGB（查表，等价于逐值算 (v/255) 的 γ 分段）
    lin = np.empty(arr.shape, dtype=np.float64)
    lin[..., 0] = S2L[arr[..., 0]]
    lin[..., 1] = S2L[arr[..., 1]]
    lin[..., 2] = S2L[arr[..., 2]]

    # 线性 RGB → XYZ(D65)
    x = M_FWD[0][0]*lin[...,0] + M_FWD[0][1]*lin[...,1] + M_FWD[0][2]*lin[...,2]
    y = M_FWD[1][0]*lin[...,0] + M_FWD[1][1]*lin[...,1] + M_FWD[1][2]*lin[...,2]
    z = M_FWD[2][0]*lin[...,0] + M_FWD[2][1]*lin[...,1] + M_FWD[2][2]*lin[...,2]

    # XYZ → Lab
    fx = _f(x / 0.95047)
    fy = _f(y)
    fz = _f(z / 1.08883)
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)

    # 色度矩阵 M2（L* 严格不变）
    a2 = M2[0][0] * a + M2[0][1] * b
    b2 = M2[1][0] * a + M2[1][1] * b

    # Lab → XYZ → 线性 RGB
    fy2 = (L + 16.0) / 116.0
    fx2 = fy2 + a2 / 500.0
    fz2 = fy2 - b2 / 200.0
    x2 = _fi(fx2) * 0.95047
    y2 = _fi(fy2)
    z2 = _fi(fz2) * 1.08883

    r = M_INV[0][0]*x2 + M_INV[0][1]*y2 + M_INV[0][2]*z2
    g = M_INV[1][0]*x2 + M_INV[1][1]*y2 + M_INV[1][2]*z2
    bl = M_INV[2][0]*x2 + M_INV[2][1]*y2 + M_INV[2][2]*z2

    # 线性 RGB → sRGB 编码（同文档 _l2s：v<=0.0031308 ? v*12.92 : 1.055 v^(1/2.4)−0.055）
    def enc(v):
        v = np.maximum(v, 0.0)
        return 255.0 * np.where(v <= 0.0031308, v*12.92,
                                1.055 * v ** (1/2.4) - 0.055)
    out = np.empty(arr.shape, dtype=np.float64)
    out[..., 0] = enc(r)
    out[..., 1] = enc(g)
    out[..., 2] = enc(bl)

    # 8×8 Bayer 有序抖动，±0.5 LSB（同文档 dither）
    h, w = out.shape[:2]
    d = BAYER8[np.mod(np.arange(h)[:, None], 8), np.mod(np.arange(w)[None, :], 8)]
    out = out + np.stack([d, d, d], axis=-1)

    # 通道截断 + 四舍五入（同文档 int(v + 0.5) 与 clamp）
    out = np.clip(np.rint(out), 0.0, 255.0)
    from PIL import Image
    return Image.fromarray(out.astype(np.uint8))


def apply(jpeg_bytes, quality=95):
    """JPEG bytes → JPEG bytes（方案 A，q95）。"""
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg_bytes))
    buf = io.BytesIO()
    apply_pil(img).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def time_apply(jpeg_bytes, quality=95):
    """同 apply，返回 (bytes, 秒)。"""
    t0 = time.time()
    out = apply(jpeg_bytes, quality)
    return out, time.time() - t0


if __name__ == "__main__":
    import sys, os
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, "rb") as f:
            data = f.read()
        out, dt = time_apply(data)
        out_path = os.path.splitext(path)[0] + "_chroma.jpg"
        with open(out_path, "wb") as f:
            f.write(out)
        print("OK  %s  (%.2f s, %d KB -> %d KB)"
              % (out_path, dt, len(data)//1024, len(out)//1024))
