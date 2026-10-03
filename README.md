# WORLDCAMERA-MSDC 行车记录仪 Windows 取流工具（XCDVR PC 移植）

把一台 `WORLDCAMERA-MSDC`（VID:1B3F）行车记录仪接成 U 盘之后，**不用装驱动、
不用管理员权限、零第三方依赖**，直接在 Windows 上：

- 实时看画面（浏览器 MJPEG 直播页）
- 拍照（多帧逐像素平均合成，降噪）
- 录视频（MJPEG 封装 MP4，手写 ftyp/moov/mdat，零依赖）
- 离线色彩还原（锁 L\* + M2 色度矩阵 + 通道截断 + 8×8 Bayer 抖动，
  numpy CPU 版 ~1.5s/帧，OpenCL GPU 版 ~15ms/帧）

## 来源声明

本工具是 **XCDVR（安卓官方 App）的 PC 版移植**。

- 官方安卓 App `uCarDvr.apk` 的工作方式是：等 `MEDIA_MOUNTED` 广播 →
  在挂载卷上找 `size == 307200` 的槽位文件 `elen1` → `open() + read()` →
  剥 16 字节私有头 → JPEG 解码。它**不碰 USB 传输层**（逆向证据：
  `libxufscamera.so` 的全部导入符号里没有一条写路径）。
- 本工具复刻了同一条路径：`elen1` 每完成一次「打开 → 读满 → 关闭」，
  设备就现做一帧 1280×720 JPEG，所以它天然就是一路视频流。
- 与 App 的唯一区别：Windows 侧必须用 `FILE_FLAG_NO_BUFFERING` 直读
  绕过文件缓存（App 在 Android 上没有这个问题），且读块大小需按卷扇区
  对齐（工具自动探测 65536 → 512）。

## 设备

| 项 | 值 |
|---|---|
| 型号 | WORLDCAMERA-MSDC（U 盘形态行车记录仪） |
| VID:PID | 1B3F:8301 |
| 槽位文件 | U 盘根目录 `elen1`，固定 **307200** 字节 |
| 帧格式 | 1280×720 MJPEG，单帧 50–97 KB |
| 稳定帧率 | **20 fps**（瓶颈在设备端造帧，≈50ms/帧；读取侧只要不拖后腿就行） |
| 输出目录 | `captured/photo/`、`captured/video/`（运行时自动创建） |

## 系统要求

- Windows 10/11（纯 ctypes 调 `CreateFileW`/`ReadFile`，未测试 Linux/macOS）
- 核心功能（探测 / 直播 / 拍照 / 录视频）：
  - **方案 A（双击即用）**：`bin/live_view/live_view.exe`，已内置 Python + numpy +
    Pillow + pyopencl，**零安装、零依赖、无需 Python 环境**
  - **方案 B（源码跑）**：Python 3.7+，纯标准库即可
- 可选功能（`--crop` 裁 OSD、`--chroma` 色彩还原、多帧平均合成）：
  - 方案 A：已内置，无需操作
  - 方案 B：`pip install pillow numpy`
- 可选加速（OpenCL GPU 色彩还原，需 NVIDIA/AMD GPU + OpenCL 驱动）：
  - 方案 A：已内置（exe 自动探测 GPU，无 GPU 时回退 numpy CPU）
  - 方案 B：`pip install pyopencl`，见 `docs/chroma_restore_opencl_README.md`

## 三步跑起来

### 方式一：双击 exe（推荐，零安装）

```powershell
# 直接运行（bin 目录里自带 Python + 全部依赖）
bin\live_view\live_view.exe --probe     # 自检
bin\live_view\live_view.exe --serve     # 起网页 → http://127.0.0.1:8081
```

> exe 是 PyInstaller onedir 产物（`bin\live_view\` 整目录 = 一个程序），
> 不能只拷 `live_view.exe` 一个文件走，要把 `live_view\` 整个目录一起带。
> 输出文件落在 `bin\live_view\captured\`（运行时自动创建）。

### 方式二：源码跑（Python 环境）

```powershell
# 1. 装可选依赖（核心功能可跳过）
pip install pillow numpy

# 2. 自检：确认设备在位、帧在变
python tools\live_view.py --probe

# 3. 起网页实时画面
python tools\live_view.py --serve
#  然后浏览器打开 http://127.0.0.1:8081
```

盘符自动探测 E~Z（跳过 C 系统盘、D 光驱），认不出时手动指定：

```powershell
python tools/live_view.py -d H:\elen1 --probe
```

## 功能清单

| 功能 | 命令 | 依赖 |
|---|---|---|
| 自检（连读 8 次看帧是否变化） | `python tools/live_view.py --probe` | 纯标准库 |
| 测帧率（10 秒采样） | `python tools/live_view.py --fps` | 纯标准库 |
| 浏览器实时画面 | `python tools/live_view.py --serve` → http://127.0.0.1:8081 | 纯标准库 |
| 换端口 / 允许局域网 | `--serve -p 9000` / `--serve --host 0.0.0.0` | 纯标准库 |
| 裁掉底部 OSD（时间/经纬度/车速） | `--serve --crop 80` | Pillow |
| 命令行拍照（N 秒曝光平均合成） | `--photo --exposure 2` | Pillow+numpy |
| 命令行录视频（N 秒） | `--record -t 30` | 纯标准库 |
| 拍照/录像 + 色彩还原 | 加 `--chroma` | Pillow+numpy |
| 离线单张色彩还原 | `python tools/chroma_restore.py a.jpg` | Pillow+numpy |
| 存 N 帧到 `frames/` | `--save -n 300` | 纯标准库 |
| 只读读 USB 描述符（排障） | `python tools/usb_desc.py` | 纯标准库 |

### HTTP 端点（`--serve` 起来之后）

| 端点 | 用途 |
|---|---|
| `/` | 带状态栏 + 拍照/录制按钮的观看页 |
| `/stream` | 标准 MJPEG 流（`multipart/x-mixed-replace`），OpenCV/ffmpeg 可直接拉 |
| `/snapshot` | 单张 JPEG 快照（定时截图、告警用） |
| `/stat` | JSON 状态 `{"seq","fps","reads","errors","frame_kb"}` |
| `/record?n=30` | 触发录制，JSON 返回文件路径（加 `&chroma=1` 走色彩还原） |
| `/photo/capture?n=2` | 拍照第一步：采集 N 秒 |
| `/photo/process` | 拍照第二步：平均合成 + 保存（加 `?chroma=1` 走色彩还原） |
| `/media?kind=all\|photo\|video` | 列出已有输出文件 |
| `/media/photo/xxx.jpg` | 取文件 |

## 网页操作

`--serve` 起来后页面底部有两组按钮：

- **● 录制**：输入秒数（默认 30），可选勾「色彩还原」→ 出
  `captured/video/dashcam_时间戳.mp4`
- **📷 拍照**：输入曝光秒数（默认 2），可选勾「色彩还原」→ 出
  `captured/photo/photo_时间戳.jpg`

拍照是两步走：先采集 N 秒全部帧 → 逐像素平均合成（噪声随帧数增加
而降低）→ 保存。拍摄/录制进行时网页画面流自动暂停，结束后自动恢复。

## 性能参考（2026-10 实测）

| 操作 | 耗时 | 说明 |
|---|---|---|
| 2s 拍照（~40 帧，含 numpy 平均 + 色彩还原 CPU） | 处理阶段 ~2.3s | 采集 2s + 处理 2.3s ≈ 墙钟 4.3s |
| 30s 拍照（~600 帧，含分块 uint64 累加平均 + 色彩还原 CPU） | 处理阶段 ~20s | 采集 30s + 处理 ~20s ≈ 墙钟 50s |
| 色彩还原单帧（numpy CPU） | ~1.5s/帧 | 离线用，别套实时 |
| 色彩还原单帧（OpenCL GPU，稳态） | ~15ms/帧 | 需 pyopencl + GPU，见 docs/ |

## 目录结构

```
worldcamera-dashcam/
├── README.md                 本文件
├── LICENSE
├── requirements.txt          pillow + numpy（源码模式用；exe 已内置）
├── setup_env.ps1 / .sh       一键装依赖（源码模式用）
├── bin/
│   └── live_view/           ★ 开箱即用 exe（PyInstaller onedir，整目录=一个程序）
│       ├── live_view.exe     双击运行，内置 Python+numpy+Pillow+pyopencl
│       ├── _internal/        依赖库（别删，exe 靠它运行）
│       └── captured/         运行时自动创建（拍照/录像输出）
├── tools/
│   ├── live_view.py          主工具（探测/直播/拍照/录像/裁剪/色彩还原入口）
│   ├── chroma_restore.py     色彩还原模块（numpy CPU，可 import）
│   ├── chroma_restore_opencl.py  色彩还原模块（OpenCL GPU，可选）
│   ├── boot_ocl.py           OpenCL 启动入口（patch pytools 缓存目录）
│   └── usb_desc.py          只读 USB 描述符（排障）
├── docs/
│   ├── 01-原理与证据.md        技术底账：elen1 槽位结构、证据链、已排除的路
│   ├── 02-部署到另一台电脑.md   部署手册：七个坑、故障排查表、性能数据
│   ├── 03-常用指令集.md         所有命令速查
│   └── chroma_restore_opencl_README.md  GPU 版色彩还原说明
└── packaging/
    └── build_exe.ps1        重打 exe 用（需 PyInstaller）
```

## 已知坑（照抄会省很多时间）

1. **必须绕过文件缓存**：`CreateFileW(..., FILE_FLAG_NO_BUFFERING)`，
   否则连读三次全是同一份缓存，会误判"文件是静态的"。
2. **必须读满整个 307200 字节**：帧 JPEG 最大 ~97KB，读少了被截断找不到 EOI。
3. **每次读必须重新 `CreateFileW`**：设备在"打开"那一刻生成新帧，
   句柄常开 + 回绕读回来的是完全不同的东西。
4. **读块必须是该卷扇区大小的整数倍**：工具自动从 65536 往下探到 512。
5. **每次读要落到缓冲区不同偏移**：全读进同一地址则只有最后一块是真的。
6. **`ctypes.create_string_buffer` 的结果必须被持有**：只留地址不引用，
   函数一返回就被 GC，`ReadFile` 往已释放内存写 → 进程静默死亡（退出码 127）。
7. **同一时刻只能有一个读取者**：一边开 `--serve`、一边又跑 `--probe`，
   两边都拿到非 JPEG 内容，帧率掉到 9~10。测速前先把正在跑的服务停掉。

## 走不通的路（别再花时间）

| 方案 | 为什么不行 |
|---|---|
| 绑 WinUSB（Zadig）+ libusb 读 Bulk 端点 | 绑了 `G:` 会消失，把唯一能走的路堵死 |
| Bulk 端点走厂商私有协议取视频 | 那两个 Bulk 端点是大容量存储（SCSI Bulk-Only），视频走文件 |
| 找 TF 卡里的视频 | 可移动卷只有一个；TF 卡录像（AVI）是另一条通路，与实时预览无关 |
| 改画面分辨率 / 帧率 | 需要厂商私有控制通道 → 要绑 WinUSB → 与 U 盘通路互斥 |

## 许可

MIT（见 [LICENSE](LICENSE)）。设备厂商的固件与协议不在本仓库范围内。
