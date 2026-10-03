# GPU 色彩还原（OpenCL 版）

把 dashcam 的彩色色度还原从 numpy CPU（约 1.2 s / 帧 @1280×720）搬到
**NVIDIA GT 730（CC 3.5）的 CUDA OpenCL 平台**上跑，稳态约 15 ms / 帧（~65 fps）。

## 为什么走 OpenCL

- PyTorch / TensorRT 路线已验证走不通：GT 730 的 compute capability 是 3.5，
  所有预编译 wheel（torch ≥ 1.8）的 CUDA kernel 都不含 sm_35，一跑就报
  `no kernel image is available for execution on the device`。
- 但 NVIDIA 驱动（475.14）提供 **CUDA OpenCL 平台**，GT 730 以 `[GPU]` 设备
  被 pyopencl 正确检测到，能编译并运行这个 kernel。
- 显示仍由 Intel HD 处理（主适配器），GPU 只负责计算，互不干扰。

## 文件

| 文件 | 作用 |
|---|---|
| `chroma_restore_opencl.py` | OpenCL kernel + Python 封装（`apply` / `apply_pil` / `list_devices` / `chroma_restore_opencl_selftest`） |
| `boot_ocl.py` | 启动入口：先 monkeypatch pytools 的缓存目录（绕开 DSH 沙箱对 AppData 的写限制），再跑 `chroma_restore_opencl` |
| `ocl_env/`（需自建） | GPU 版专用虚拟环境：Python 3.12 + pyopencl + numpy + Pillow，见下文「自建 ocl_env」|`ocl_env/`（需自建） | GPU 版专用虚拟环境：Python 3.12 + pyopencl + numpy + Pillow，见下文「自建 ocl_env」 |
| `.pytools_cache\` | pytools kernel 缓存落盘目录（workspace 内，可写） |

## 用法

所有命令都用 `ocl_env` 里的 Python：

```powershell
$py = <你的 ocl_env 路径>\Scripts\python.exe

# 列出 OpenCL 设备
& $py "<包根>\tools\boot_ocl.py" --list

# 处理单张 JPEG（输出 _chroma_ocl.jpg）
& $py "<包根>\tools\boot_ocl.py" <input.jpg>

# 对比 numpy 参考版（像素级差异 + 耗时）
& $py "<包根>\tools\boot_ocl.py" --compare <input.jpg>
```

也可以直接 import（记得先 patch pytools）：

```python
import pytools.persistent_dict as pd
import os
fallback = r"<包根>\.pytools_cache"
os.makedirs(fallback, exist_ok=True)
_orig = pd._PersistentDictBase.__init__
def _patched(self, *a, **kw):
    kw["container_dir"] = fallback
    _orig(self, *a, **kw)
pd._PersistentDictBase.__init__ = _patched

import chroma_restore_opencl as clm
out, dt = clm.apply(jpeg_bytes)        # → JPEG bytes
out_pil = clm.apply_pil(pil_img)       # → PIL Image
```

## Kernel 管线（单趟 1D，每像素一次）

```
sRGB (uchar/255)
  → 线性 RGB        （256 项 LUT，__global float buffer）
  → XYZ (D65)       （线性 RGB → XYZ 矩阵）
  → Lab             （f(t)=t^(1/3) 或 7.787t+16/116，阈值 0.008856）
  → M2 色度矩阵     （a' b' 按 M2 重映射，L 不变）
  → Lab⁻¹ → XYZ⁻¹ → 线性 RGB⁻¹
  → sRGB 编码      （v≤0.0031308 ? 12.92v : 1.055·v^(1/2.4)−0.055）
  → 8×8 Bayer 有序抖动（±0.5 LSB）
  → clamp [0,255] → round → uchar
```

## 实测（GT 730，1280×720）

| 指标 | 数值 |
|---|---|
| 稳态单帧 | **~15.6 ms**（~64 fps） |
| 首次（含 kernel 编译） | ~2.5 s |
| numpy CPU 参考 | ~1.5 s |
| 相对加速 | **~100×** |
| 与 numpy 版像素差 | mean=0.000, max=2 LSB（Bayer ±0.5 范围内） |

## 与 numpy 版 `chroma_restore.py` 的关系

- 算法完全一致（同一套矩阵、同一 LUT、同一 Bayer 表），只是把循环搬到了 GPU。
- numpy 版仍保留作为参考 / CPU 兜底；OpenCL 版通过 `force_cpu=True` 可切回
  Intel CPU 设备（仍走 OpenCL，只是不用 NVIDIA GPU）。
- 两版输出在 Bayer 抖动的 ±1 LSB 内一致（实测 max diff = 2，源于
  float↔int 量化路径的舍入顺序差异，视觉无差）。

## 拍照处理时间拆解（2026-10-03 优化记录）

`live_view.py` 拍照模式的流水线：采集 N 秒（实时）→ JPEG 解码全部帧 →
逐像素平均合成 → JPEG 编码 →（可选）色彩还原。

### 2s 曝光（~40 帧）

| 阶段 | 优化前 | 优化后 | 说明 |
|---|---|---|---|
| JPEG 解码 ×40 | 559 ms | ~600 ms | Pillow，不变 |
| 逐像素平均 | **~23 s（纯 Python 循环）** | **~0.7 s（numpy 分块累加）** | `_average_photos` 改分块 `sum(axis=0)` + uint64 累加 |
| JPEG 编码 | 12 ms | 12 ms | 不变 |
| 色彩还原（勾选时） | 1430 ms（numpy CPU） | **47 ms（OpenCL GPU，稳态）** | `enable_chroma` 自动选 OpenCL，回退 numpy |
| **处理阶段合计** | **~24 s** | **~0.8 s（GPU）/ ~2.3 s（CPU 回退）** | |

### 30s 曝光（~600 帧）

| 阶段 | 优化前 | 优化后 | 说明 |
|---|---|---|---|
| 采集窗口 | 30 s | 30 s | 实时，不可压缩 |
| JPEG 解码 ×600 | ~10 s | ~10 s | Pillow 单线程解码，主要耗时 |
| 逐像素平均 ×600 | **~52 s（全量 np.stack，1.7 GB 临时分配）** | **~9 s（分块 uint64 累加）** | 块 256 帧，块内 `sum(axis=0, dtype=uint64)` 累加到 3×H×W 的 uint64 平面，与 f64 全量平均**逐位一致**（实测 max diff = 0） |
| JPEG 编码 | 14 ms | 14 ms | |
| 色彩还原 | 2.7 s（numpy CPU） | **47 ms（OpenCL GPU）** | 同上 |
| **处理阶段合计** | **~73 s** | **~19 s（CPU 回退）/ ~9 s（GPU）** | |

注：当前 8081 服务跑在系统 Python 3.14（无 pyopencl），走 numpy 回退路径，
30s 照片实测**处理阶段 ~22 s**（采集 30s + 处理 22s ≈ 墙钟 52s）。
用 `ocl_env` 解释器启动服务可把色彩还原降到 47 ms，处理阶段 ~19 s。

`live_view.py` 的 `enable_chroma()` 会自动尝试 OpenCL 路径（import 前 patch
pytools 缓存目录），失败才回退 numpy。`chroma_fix_jpeg_bytes` 按
`_CHROMA_ENGINE` 分发。

## 注意事项

1. **pytools 缓存**：pyopencl 2026 的 invoker 在 import 时会构造一个
   `WriteOncePersistentDict`（sqlite），默认写 `%LOCALAPPDATA%\pytools`。
   DSH 沙箱下 AppData 对 Python 进程不可写，所以 `boot_ocl.py` 在 import
   pyopencl 之前先 monkeypatch `pytools.persistent_dict._PersistentDictBase`
   把 `container_dir` 指到 workspace 内的 `.pytools_cache\`。直接 import
   `chroma_restore_opencl` 时要自己先做同样的 patch。
2. **pyopencl 2026 API 变化**：
   - `cl.get_devices()` → `cl.get_platforms()` + `p.get_devices()`
   - `cl.memf.Allocator(ctx)` 已移除 → `cl.Buffer(ctx, flags, size)` + `cl.enqueue_copy`
   - `q.enqueue_nd_range_for_kernel` → 模块级 `cl.enqueue_nd_range_kernel(q, kernel, gl, local, ...)`
   - `q.enqueue_read_buffer / write_buffer` → `cl.enqueue_copy(q, buf, host_array)`
   - `kernel.set_args(buf, ...)` 传 `np.int32` 而不是 Python `int`
3. **GT 730 的 ptxas 限制**：
   - 不认 `powf` / `cbrtf` 外链（报 `ptxas: Unresolved extern function`）→
     kernel 里用 `exp(log(v)/3)` 和 `exp(log(v)/2.4)` 替代。
   - 不支持 `-cl-fast-math`。
   - 隐式声明 `min/max` 会编译失败，要用显式 `if` 比较。
4. **环境**：必须用 `ocl_env`（Python 3.12）。系统其他 Python 版本没装
   pyopencl（系统 Python 3.14 下 `live_view.py` 会自动回退 numpy 色彩还原）。


## 自建 ocl_env（GPU 版色彩还原）

`ocl_env` 不随包发布（体积太大、平台相关）。要跑 GPU 版色彩还原，自己建一个：

```powershell
py -3.12 -m venv ocl_env
& ocl_env\Scripts\python.exe -m pip install pyopencl numpy pillow
```

然后从包根目录运行：

```powershell
& ocl_env\Scripts\python.exe tools\boot_ocl.py --list
& ocl_env\Scripts\python.exe tools\boot_ocl.py <任意.jpg>
```

> `boot_ocl.py` 会自动 patch pytools 的 kernel 缓存目录到包内
> `.pytools_cache/`（绕开沙箱/权限问题）。
