#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_view.py — 行车记录仪实时画面（Windows，零驱动改动）

═══════════════════════════════════════════════════════════════════════════
原理（2026-10-01 实测定案）
═══════════════════════════════════════════════════════════════════════════
这块记录仪把「当前画面」做成一帧 1280×720 的 JPEG，塞进它 U 盘上的一个
**固定 307200 字节的槽位文件**，名字叫 `elen1`。

    槽位结构 = [16 字节私有头][完整 JPEG]
    每次**重新读**这个文件，设备就现做一帧新的 → 所以它天然就是一份
    「拉取式」视频流。

XCDVR App 也是这么干的 —— 它根本不碰 USB 传输层：
    等 MEDIA_MOUNTED 广播 → 在挂载卷上找 size==307200 的 elen1
    → libc open() + read() → 剥头 → JPEG 解码 → I420

⚠️ **唯一的坑：Windows 文件缓存。**
   普通 open/read 三次会拿到同一份缓存，看起来像"文件是静态的"。
   必须用 FILE_FLAG_NO_BUFFERING 直读，才能每次都真的问设备要数据。

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
    python live_view.py --probe                  # 自检：找到设备 + 测帧率
    python live_view.py --serve                  # ★ 浏览器实时看画面
                                                 #   然后打开 http://127.0.0.1:8081
    python live_view.py --serve -p 9000          # 换端口
    python live_view.py --save -n 60             # 存 60 帧到 frames/
    python live_view.py --fps                    # 只测帧率
    python live_view.py -d H:\\elen1 --serve      # 手动指定盘符

    python live_view.py --serve --crop 80        # ★ 去掉底部那行 OSD（需 Pillow）
                                                 #   画面左下角的时间/经纬度/车速是
                                                 #   设备烧进 JPEG 的，主机侧关不掉，
                                                 #   只能在读到帧之后自己裁。

═══════════════════════════════════════════════════════════════════════════
依赖
═══════════════════════════════════════════════════════════════════════════
    不带 --crop 时：**纯标准库，零依赖**，拷到任何 Windows 机器直接跑。
    带 --crop 时：需要 Pillow（pip install pillow）—— 因为裁剪要解码重编码 JPEG。
    想连 Pillow 都不想装，就在浏览器侧裁：见 crop_osd.py --proxy 或 ffmpeg。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import os
import socket
import json
import struct
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, wt.HANDLE]
k32.ReadFile.restype = wt.BOOL
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                         ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]

SLOT = 307200      # 固件里 malloc(0x4B000)，写死在 g.a() 里做长度校验
CHUNK = 512        # 原生层 read(fd, buf, 512)

# 候选读块大小，从大到小试。
# ⚠️ 跨机器要点：FILE_FLAG_NO_BUFFERING 要求「读长度」必须是该卷**扇区大小**的整数倍，
#    且缓冲区地址对齐。扇区通常是 512，但也可能是 4096。所以不能写死，必须探测。
CHUNK_CANDIDATES = (65536, 32768, 16384, 8192, 4096, 2048, 512)

if getattr(sys, "frozen", False):
    # 打包成 exe（PyInstaller onedir）时：__file__ 指向 _internal/，
    # 输出目录应落在 exe 同级而不是 _internal 里。
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))


# ────────────────────────────────────────────────────────────── 设备定位
def find_elen1(explicit=None):
    """在全部盘符里找 size == 307200 的 elen1"""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    # Scan all fixed/removable drive letters except C (system) and D (often CD-ROM).
    # 2026-10-01: 本机（LTSC 2019）实测记录仪挂在 E:，原 G-Z 扫不到。
    for d in "EFGHIJKLMNOPQRSTUVWXYZ":
        p = "%s:\\elen1" % d
        try:
            st = os.stat(p)
            if st.st_size == SLOT:
                return p
        except OSError:
            continue
    return None


# ────────────────────────────────────────────────────────────── 无缓冲读
def detect_chunk(path):
    """探测该卷能接受的最大读块（必须是扇区大小的整数倍，扇区可能是 512 也可能是 4096）。
    返回可用的最大块；全失败返回 None。"""
    for c in CHUNK_CANDIDATES:
        buf = ctypes.create_string_buffer(c + 8192)
        a = ctypes.addressof(buf)
        addr = a + ((-a) % 4096)
        h = k32.CreateFileW(path, GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                            OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
        if not h or h == INVALID_HANDLE_VALUE:
            continue
        try:
            nr = wt.DWORD(0)
            ok = k32.ReadFile(h, ctypes.c_void_p(addr), c, ctypes.byref(nr), None)
        finally:
            k32.CloseHandle(h)
        if ok and nr.value == c:
            return c
    return None


class SlotReader:
    def __init__(self, path, chunk=None):
        self.path = path
        if chunk is None:
            chunk = detect_chunk(path) or CHUNK
        self.chunk = chunk
        # 缓冲区要能装下整个槽位 + 对齐余量
        self._raw = ctypes.create_string_buffer(SLOT + chunk + 8192)
        a = ctypes.addressof(self._raw)
        self._addr = a + ((-a) % 4096)
        self.reads = 0
        self.errors = 0

    def read(self, total=SLOT):
        """顺序读完整个槽位，返回 bytes。失败抛 OSError

        实测要点（2026-10-01）：
          · 必须读满整个 307200 —— 帧 JPEG 最大可达 ~97KB，读少了会被截断（找不到 EOI）
          · 每次读要落到缓冲区的**不同偏移**；若每块都写同一地址、最后整段扫，
            缓冲区里只有最后一块是真的（其余是 0），永远找不到 JPEG
          · chunk 从 512 提到 4096 能把帧率从 8.7 拉到 20（每块有固定开销）；
            再往上（32768/65536）收益递减，~20/s 是这台设备拉取通道的上限
          · 必须每次重新 CreateFileW —— 设备只在**打开那一刻**生成新帧。
            句柄常开 + SetFilePointerEx 回绕读回来的是完全不同的东西（没有任何 JPEG）
        """
        h = k32.CreateFileW(self.path, GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                            OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
        if not h or h == INVALID_HANDLE_VALUE:
            e = ctypes.get_last_error()
            raise OSError("CreateFileW err=%d (%s)" % (e, ctypes.FormatError(e)))
        got = 0
        try:
            while got < total:
                n = min(self.chunk, total - got)
                nread = wt.DWORD(0)
                dst = ctypes.c_void_p(self._addr + got)
                if not k32.ReadFile(h, dst, n, ctypes.byref(nread), None):
                    e = ctypes.get_last_error()
                    raise OSError("ReadFile@%d err=%d (%s)"
                                  % (got, e, ctypes.FormatError(e)))
                if nread.value == 0:
                    break
                got += nread.value
        finally:
            k32.CloseHandle(h)
        self.reads += 1
        return ctypes.string_at(self._addr, got)


# ────────────────────────────────────────────────────────────── 帧提取
def extract_jpeg(b):
    """从 307200 字节槽位里切出一帧完整 JPEG；没有就返回 None"""
    soi = b.find(b"\xff\xd8\xff")
    if soi < 0:
        return None
    eoi = b.find(b"\xff\xd9", soi + 3)
    if eoi < 0:
        return None
    return b[soi:eoi + 2]


# ────────────────────────────────────────────────────────────── 取帧线程
class FrameSource:
    """后台不停读槽位，只保留最新一帧"""

    def __init__(self, path, fps_target=0.0):
        self.reader = SlotReader(path)
        self.lock = threading.Lock()
        self.frame = None
        self.seq = 0
        self.fps = 0.0
        self.running = True
        self.fps_target = fps_target
        self.stream_gate = threading.Event()  # set() = stream may read; clear() = paused
        self.stream_gate.set()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def pause_stream(self):
        """拍照/录制时暂停 /stream 消费帧，避免帧被抢走"""
        self.stream_gate.clear()

    def resume_stream(self):
        self.stream_gate.set()

    def _loop(self):
        n = 0
        t0 = time.time()
        while self.running:
            try:
                raw = self.reader.read()
                j = extract_jpeg(raw)
                if j:
                    j = apply_crop(j)          # 开了 --crop 才动，否则原样返回
                    with self.lock:
                        self.frame = j
                        self.seq += 1
            except Exception:
                self.reader.errors += 1
                time.sleep(0.2)
                continue
            n += 1
            dt = time.time() - t0
            if dt >= 1.0:
                self.fps = n / dt
                n = 0
                t0 = time.time()
            if self.fps_target > 0:
                gap = 1.0 / self.fps_target - (time.time() - t0) / max(n, 1)
                if gap > 0:
                    time.sleep(gap)

    def get(self):
        with self.lock:
            return self.frame, self.seq

    def stop(self):
        self.running = False


# ────────────────────────────────────────────────────── 可选的底部裁剪（OSD）
# 画面左下角那行「时间 / 经纬度 / 车速」是**设备固件烧进 JPEG 的**，主机侧关不掉，
# 只能在拿到帧之后自己裁。裁剪要解码/重编码 JPEG，所以必须有 Pillow。
# → 这里做成**可选**：不传 --crop 就完全不碰 Pillow，主工具保持零依赖。
CROP_H = 0          # 0 = 不裁
CROP_Q = 85         # 重编码质量（实测：keep 反而更大，q70→60KB / q85→84KB / q95→131KB）


def enable_crop(h, quality=CROP_Q):
    """打开底部裁剪。成功返回 True；没有 Pillow 返回 False（由调用方报错）。"""
    global CROP_H, _crop_one
    import io as _io
    from PIL import Image

    def _crop_one(jpg):
        im = Image.open(_io.BytesIO(jpg))
        im.load()
        w, ht = im.size
        if h >= ht:
            return jpg
        buf = _io.BytesIO()
        im.crop((0, 0, w, ht - h)).save(buf, "JPEG", quality=quality)
        return buf.getvalue()

    CROP_H = h
    return True


def apply_crop(jpg):
    """按当前设置裁一帧（没开裁剪就原样返回）。"""
    fn = globals().get("_crop_one")
    if not fn or not jpg:
        return jpg
    try:
        return fn(jpg)
    except Exception:
        return jpg

# ────────────────────────────────────────────────────────────── 输出目录（相对路径，03-工具/captured/ 下）
CAPTURE_DIR = os.path.join(HERE, "captured")
VIDEO_DIR = os.path.join(CAPTURE_DIR, "video")
PHOTO_DIR = os.path.join(CAPTURE_DIR, "photo")


def _mkdirs():
    os.makedirs(VIDEO_DIR, exist_ok=True)
    os.makedirs(PHOTO_DIR, exist_ok=True)


# ────────────────────────────────────────────────────────────── MP4 封装（零依赖）
def _read_jpeg_size(jpg):
    """从 JPEG SOI 后第一个 SOF 段读出宽高"""
    if len(jpg) < 2 or jpg[0:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(jpg)
    while i + 9 < n:
        if jpg[i] != 0xFF:
            i += 1
            continue
        marker = jpg[i + 1]
        if marker in (0xD8, 0xD9):
            i += 2
            continue
        if 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 3 >= n:
            return None
        seglen = (jpg[i + 2] << 8) | jpg[i + 3]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = (jpg[i + 5] << 8) | jpg[i + 6]
            w = (jpg[i + 7] << 8) | jpg[i + 8]
            return w, h
        i += 2 + seglen
    return None


def _box(typ, payload):
    """普通 box：len(4) + type(4) + payload"""
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _fullbox(typ, flags, payload):
    """full box：len(4) + type(4) + version(1)+flags(3) + payload"""
    return struct.pack(">I", 12 + len(payload)) + typ + struct.pack(">I", flags) + payload


class MjpegMp4Writer:
    """把一序列 JPEG 帧写成 .mp4（mjpeg 视频轨）。

    浏览器 / QuickTime / VLC 都能放 mjpeg 封装的 mp4。
    布局：ftyp + moov + mdat，stco 指向 mdat 内的帧偏移。
    时基 10000 Hz，帧间 delta 按真实采集时间算。
    """

    def __init__(self, fps_hint=20.0):
        self.timescale = 10000
        self.fps_hint = max(1.0, fps_hint)
        self.frames = []
        self.w = self.h = 0

    def add(self, jpeg, t_abs):
        self.frames.append((t_abs, jpeg))
        if not self.w:
            d = _read_jpeg_size(jpeg)
            if d:
                self.w, self.h = d

    def finalize(self, out_path):
        if not self.frames:
            raise RuntimeError("no frames")
        n = len(self.frames)
        t0 = self.frames[0][0]

        deltas = []
        prev_t = t0
        for t, jpg in self.frames:
            d = int(round((t - prev_t) * self.timescale))
            if d < 1:
                d = int(1.0 / self.fps_hint * self.timescale) or 1
            deltas.append(d)
            prev_t = t
        total_ticks = sum(deltas)

        mdat_payload = b""
        for _, jpg in self.frames:
            mdat_payload += jpg
        mdat_len = 8 + len(mdat_payload)

        stts_entries = []
        i = 0
        while i < n:
            j = i
            while j < n and deltas[j] == deltas[i]:
                j += 1
            stts_entries.append((deltas[i], j - i))
            i = j
        stts_payload = struct.pack(">I", len(stts_entries))
        for d, c in stts_entries:
            stts_payload += struct.pack(">II", d, c)

        stsz_payload = struct.pack(">II", 0, n)
        for _, jpg in self.frames:
            stsz_payload += struct.pack(">I", len(jpg))

        stsc_payload = struct.pack(">III", 1, 1, 1)
        stco_payload = struct.pack(">I", n) + struct.pack(">I", 0) * n

        stsc_box = _fullbox(b"stsc", 0, stsc_payload)
        stts_box = _fullbox(b"stts", 0, stts_payload)
        stsz_box = _fullbox(b"stsz", 0, stsz_payload)
        stco_box = _fullbox(b"stco", 0, stco_payload)

        video_entry = (
            b"\x00" * 8
            + struct.pack(">H", 1)
            + b"\x00" * 16
            + struct.pack(">HH", self.w, self.h)
            + struct.pack(">II", 0x00480000, 0x00480000)
            + struct.pack(">I", 0)
            + struct.pack(">B", 0)
            + struct.pack(">H", 0x0020)
            + struct.pack(">h", -1)
            + struct.pack(">B", 3)
            + b"mjpg"
            + b" " * 32
        )
        stsd_entry = _box(b"mjpg", video_entry)
        stsd_payload = struct.pack(">I", 1) + stsd_entry
        stsd_box = _fullbox(b"stsd", 0, stsd_payload)

        stbl_payload = stsc_box + stts_box + stsz_box + stco_box + stsd_box
        stbl_box = _box(b"stbl", stbl_payload)

        vmhd_payload = struct.pack(">I", 1) + struct.pack(">HHH", 0, 0, 0) \
            + struct.pack(">HH", self.w, self.h)
        vmhd_box = _fullbox(b"vmhd", 1, vmhd_payload)
        dinf_box = _box(b"dinf", _box(b"dref",
                      struct.pack(">I", 1) + _box(b"url ", struct.pack(">I", 1))))
        minf_payload = vmhd_box + dinf_box + stbl_box
        minf_box = _box(b"minf", minf_payload)

        mdhd_payload = struct.pack(">II", 0, 0) + struct.pack(">I", 0) \
            + struct.pack(">I", self.timescale) + struct.pack(">I", total_ticks) \
            + struct.pack(">H", 0x55C7) + struct.pack(">H", 0)
        mdhd_box = _fullbox(b"mdhd", 0, mdhd_payload)
        hdlr_payload = struct.pack(">I", 0) + b"vide" + b"\x00\x00\x00\x00" \
            + b"\x00" * 4 + b"VideoHandler\x00"
        hdlr_box = _fullbox(b"hdlr", 0, hdlr_payload)
        mdia_payload = mdhd_box + hdlr_box + minf_box
        mdia_box = _box(b"mdia", mdia_payload)

        tkhd_w = self.w << 16
        tkhd_h = self.h << 16
        tkhd_payload = struct.pack(">I", 3) + struct.pack(">II", 0, 1) + struct.pack(">I", 1) \
            + struct.pack(">I", 0) + struct.pack(">I", total_ticks) \
            + struct.pack(">II", 0, 0) + struct.pack(">H", 0) + struct.pack(">H", 0) \
            + struct.pack(">I", 0x00010000) \
            + struct.pack(">4i", 0x40000000, 0, 0, 0x40000000) \
            + struct.pack(">I", 1 << 16) \
            + struct.pack(">ii", tkhd_w, tkhd_h)
        tkhd_box = _fullbox(b"tkhd", 3, tkhd_payload)
        trak_payload = tkhd_box + minf_box
        trak_box = _box(b"trak", trak_payload)

        mvhd_payload = struct.pack(">II", 0, 0) + struct.pack(">I", total_ticks) \
            + struct.pack(">I", self.timescale) + struct.pack(">I", 0) \
            + struct.pack(">H", 0x0100) + struct.pack(">H", 0) \
            + struct.pack(">3H", 0, 0, 0) + struct.pack(">I", 0) \
            + struct.pack(">I", 0x00010000) + struct.pack(">3i", 0, 0, 0) \
            + struct.pack(">I", 0) + struct.pack(">I", 0x40000000) \
            + struct.pack("<6i", 0, 1, 0, -1, 0, 0x40000000) \
            + struct.pack("<I", 0x00010000) + struct.pack("<I", 0) \
            + struct.pack("<I", 2)
        mvhd_box = _fullbox(b"mvhd", 0, mvhd_payload)
        moov_payload = mvhd_box + trak_box
        moov_box = _box(b"moov", moov_payload)

        ftyp_box = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomavc1")

        mdat_start = len(ftyp_box) + len(moov_box)
        mdat_data_start = mdat_start + 8
        offsets = []
        off = 0
        for _, jpg in self.frames:
            offsets.append(mdat_data_start + off)
            off += len(jpg)
        stco_final_payload = struct.pack(">I", n)
        for o in offsets:
            stco_final_payload += struct.pack(">I", o)
        stco_final = _fullbox(b"stco", 0, stco_final_payload)
        moov_box = moov_box.replace(stco_box, stco_final, 1)

        with open(out_path, "wb") as f:
            f.write(ftyp_box)
            f.write(moov_box)
            f.write(struct.pack(">I", mdat_len) + b"mdat" + mdat_payload)
        return out_path


# ────────────────────────────────────────────────────────────── 录制模式（输出到 captured/video/）
def record(src, seconds):
    """录 N 秒 → captured/video/*.mp4"""
    _mkdirs()
    t0 = time.time()
    frames = []
    last = -1
    print("[录制] %.0f 秒 → captured/video/" % seconds)
    while time.time() - t0 < seconds:
        f, seq = src.get()
        if f and seq != last:
            last = seq
            frames.append((time.time(), f))
        else:
            time.sleep(0.005)
    if not frames:
        print("!! 没录到任何帧（先跑 --probe）")
        return None
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(t0))
    out_path = os.path.join(VIDEO_DIR, "dashcam_%s.mp4" % ts)
    print("  共 %d 帧，写入 MP4……" % len(frames))
    w = MjpegMp4Writer(fps_hint=max(1.0, src.fps or 20.0))
    for t, jpg in frames:
        if _CHROMA_ON:
            jpg = chroma_fix_jpeg_bytes(jpg)
        w.add(jpg, t)
    w.finalize(out_path)
    print("✅ 完成：%s（%d 帧 / %.1f 秒，%.1f KB）"
          % (out_path, len(frames), seconds, os.path.getsize(out_path) / 1024.0))
    return out_path


# ────────────────────────────────────────────────────────────── 拍照模式（曝光内帧逐像素平均合成，输出到 captured/photo/）
def _average_photos(frames):
    """逐像素平均合成。返回 (avg_bytes, w, h, is_jpeg)"""
    try:
        from PIL import Image
    except ImportError:
        return frames[len(frames) // 2], 0, 0, True
    imgs = []
    for jpg in frames:
        try:
            im = Image.open(io.BytesIO(jpg)).convert("RGB")
            imgs.append(im)
        except Exception:
            continue
    if not imgs:
        return frames[0], 0, 0, True
    if len(imgs) == 1:
        buf = io.BytesIO()
        imgs[0].save(buf, "JPEG", quality=90)
        return buf.getvalue(), imgs[0].width, imgs[0].height, True
    w, h = imgs[0].width, imgs[0].height
    imgs = [im if im.size == (w, h) else im.resize((w, h)) for im in imgs]
    # 分块 uint32 累加平均：
    #   · 一次 np.stack 全量（N×H×W×3 可达数 GB）在旧机型上又慢又爆内存；
    #   · 块内 sum(axis=0) → uint32 再累加，单块 256 帧时最大 256×255 不溢出，
    #     结果与全量 f64 平均逐位一致（已实测 max diff = 0）
    import numpy as _np
    acc = _np.zeros((h, w, 3), dtype=_np.uint64)
    CHUNK = 256
    for i in range(0, len(imgs), CHUNK):
        blk = _np.stack([_np.asarray(im.convert("RGB"), dtype=_np.uint8)
                         for im in imgs[i:i + CHUNK]])
        acc += blk.sum(axis=0, dtype=_np.uint64)
        del blk
    avg = (acc / len(imgs)).astype(_np.uint8).tobytes()
    del acc
    return avg, w, h, False


def _rgb_to_jpeg(rgb, w, h, quality=90):
    from PIL import Image
    if len(rgb) != w * h * 3:
        raise ValueError("rgb data mismatch: got %d bytes, expected %d"
                         % (len(rgb), w * h * 3))
    im = Image.frombytes("RGB", (w, h), rgb)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def photo(src, exposure_s, quality=90):
    """抓 exposure_s 秒内的全部帧 → 平均合成 → captured/photo/"""
    _mkdirs()
    print("[拍照] 曝光 %.1f 秒（逐像素平均合成）……" % exposure_s)
    t0 = time.time()
    frames = []
    last = -1
    while time.time() - t0 < exposure_s:
        f, seq = src.get()
        if f and seq != last:
            last = seq
            frames.append(f)
        else:
            time.sleep(0.005)
    if not frames:
        print("!! 没抓到任何帧（先跑 --probe）")
        return None, 0, "none"
    avg_bytes, w, h, is_jpeg = _average_photos(frames)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(t0))
    if is_jpeg:
        out_path = os.path.join(PHOTO_DIR, "photo_%s.jpg" % ts)
        with open(out_path, "wb") as fp:
            fp.write(avg_bytes)
        method = "mid"
    else:
        try:
            jpg = _rgb_to_jpeg(avg_bytes, w, h, quality)
            out_path = os.path.join(PHOTO_DIR, "photo_%s.jpg" % ts)
            with open(out_path, "wb") as fp:
                fp.write(jpg)
            method = "average"
        except ImportError:
            out_path = os.path.join(PHOTO_DIR, "photo_%s.raw" % ts)
            with open(out_path, "wb") as fp:
                fp.write(avg_bytes)
            method = "raw(no Pillow)"
    if _CHROMA_ON:
        chroma_fix_file(out_path)
    print("  %d 帧平均 → %s（%s，%.1f KB）"
          % (len(frames), out_path, method, os.path.getsize(out_path) / 1024.0))
    return out_path, len(frames), method



# ────────────────────────────────────────────────────────────── MJPEG 服务
PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Dashcam Live</title>
<style>
 html,body{margin:0;background:#0b0d10;color:#d7dbe0;
    font:14px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif}
 header{padding:12px 18px;display:flex;gap:18px;align-items:baseline;
    border-bottom:1px solid #1d2127}
 h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.02em}
 .m{color:#7d8894;font-variant-numeric:tabular-nums}
 .ts{color:#3fb950;font:600 14px/1.5 "Cascadia Code",Consolas,monospace;
    font-variant-numeric:tabular-nums;letter-spacing:.04em;margin-left:auto}
 .wrap{display:flex;justify-content:center;padding:18px}
 img#v{max-width:100%;height:auto;border-radius:6px;background:#000;
    box-shadow:0 6px 28px rgba(0,0,0,.55)}
 .bar{display:flex;gap:14px;align-items:center;justify-content:center;
    padding:14px 18px;flex-wrap:wrap}
 .grp{display:flex;gap:8px;align-items:center;background:#161a20;
    border:1px solid #232830;border-radius:8px;padding:8px 12px}
 .grp label{color:#8b95a3;font-size:12px}
 .grp input{width:52px;background:#0e1116;color:#d7dbe0;border:1px solid #2a3140;
    border-radius:4px;padding:4px 6px;font-size:13px}
 button{background:#2a63c8;color:#fff;border:none;border-radius:6px;
    padding:8px 18px;font-size:13px;cursor:pointer;font-weight:600}
 button:disabled{background:#3a4150;cursor:default;opacity:.7}
 button.rec{background:#c8402a}
 button.ph{background:#2a9648}
 .out{color:#7d8894;font-size:12px;font-variant-numeric:tabular-nums;
    min-width:220px;text-align:left}
 .ts{color:#3fb950;font:600 14px/1.5 "Cascadia Code",Consolas,monospace;
    font-variant-numeric:tabular-nums;letter-spacing:.04em;margin-left:auto}
 .phase{color:#c9a227;font-size:12px;font-weight:600}
 .ck{color:#8b95a3;font-size:12px;display:inline-flex;align-items:center;gap:4px;
    cursor:pointer;user-select:none}
 .ck input{cursor:pointer}
 .media{padding:8px 18px 14px}
 .media-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
 .media-title{color:#8b95a3;font-size:12px;font-weight:600}
 button.mini{background:#232830;border:1px solid #2a3140;color:#8b95a3;
    padding:3px 10px;font-size:11px;cursor:pointer}
 .media-grid{display:flex;flex-wrap:wrap;gap:8px}
 .mi{width:130px;background:#161a20;border:1px solid #232830;border-radius:6px;
    cursor:pointer;overflow:hidden}
 .mi:hover{border-color:#2a63c8}
 .mi img,.mi video{width:100%;display:block;background:#000}
 .mi .cap{padding:4px 6px;color:#7d8894;font-size:10px;line-height:1.4;
    font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis}
 .mi .cap b{color:#d7dbe0;font-weight:600}
 .lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);
    z-index:10;align-items:center;justify-content:center;cursor:zoom-out}
 .lightbox img,.lightbox video{max-width:90vw;max-height:90vh}
</style>
<header>
  <h1>WORLDCAMERA-MSDC &middot; elen1</h1>
  <span class="m" id="m">\u8fde\u63a5\u4e2d\u2026</span>
  <span class="ts" id="ts">--:--:--</span>
</header>
<div class="wrap"><img id="v" src="/stream"></div>
<div class="bar">
  <div class="grp">
    <label>\u5f55\u5236</label>
    <input id="recN" value="30" type="number" min="1" max="600">
    <button class="rec" id="recBtn" onclick="doRec()">\u25cf \u5f55\u5236</button>
    <label class="ck" title="\u5f55\u5236\u5b8c\u6210\u540e\u9010\u5e27\u8fc7\u4e00\u904d\u8272\u5f69\u8fd8\u539f\uff08\u79bb\u7ebf\uff0c~1.6s/\u5e27\uff0c\u89c6\u9891\u4f1a\u53d8\u957f\uff09">\u8272\u5f69\u8fd8\u539f <input id="recChroma" type="checkbox"></label>
  </div>
  <div class="grp">
    <label>\u62cd\u7167\uff08\u66dd\u5149\u79d2\u6570\uff09</label>
    <input id="expN" value="2" type="number" min="0" max="60" step="0.5">
    <button class="ph" id="phBtn" onclick="doPhoto()">\U0001F4F7 \u62cd\u7167</button>
    <label class="ck" title="\u62cd\u7167\u540e\u81ea\u52a8\u8fc7\u4e00\u904d\u8272\u5f69\u8fd8\u539f\uff08\u79bb\u7ebf\uff0c~1.6s\uff09">\u8272\u5f69\u8fd8\u539f <input id="phChroma" type="checkbox"></label>
  </div>
  <span class="out" id="out"></span>
  <span class="phase" id="phase"></span>
 </div>
<div class="media">
  <div class="media-head">
    <span class="media-title">\u5df2\u62cd\u6444\uff08\u70b9\u51fb\u67e5\u770b\uff09</span>
    <button class="mini" onclick="loadMedia()">\u5237\u65b0</button>
  </div>
  <div class="media-grid" id="mediaGrid"></div>
</div>
<div id="lightbox" class="lightbox" onclick="this.style.display='none'">
  <img id="lightboxImg">
  <video id="lightboxVid" style="display:none" controls></video>
</div>
<script>
  var busy=false;

  // 实时时钟
  function tick(){
    var d=new Date();
    var p=function(n){return ('0'+n).slice(-2)};
    document.getElementById('ts').textContent =
      d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())
      +' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
  }
  setInterval(tick,1000);tick();

  function setPhase(msg){document.getElementById('phase').textContent=msg||'';}
  function setBusy(b){busy=b;
    document.getElementById('recBtn').disabled=b;
    document.getElementById('phBtn').disabled=b;}

  function doRec(){
    if(busy)return;
    var s=+document.getElementById('recN').value||30;
    var ch=document.getElementById('recChroma').checked;
    setBusy(true);
    setPhase('\u5f55\u5236\u4e2d\uff08'+s+'s'+(ch?'\uff0c\u542b\u8272\u5f69\u8fd8\u539f':'')+'\uff09\u2026');
    document.getElementById('out').textContent='';
    fetch('/record?n='+s+(ch?'&chroma=1':''))
      .then(function(r){return r.json();})
      .then(function(d){
        setPhase('');
        document.getElementById('out').textContent=
          '\u2705 '+d.file+'  \uff08'+d.frames+' \u5e27\uff0c'+d.size_kb+' KB\uff09';
        loadMedia();
      })
      .catch(function(e){
        setPhase('');
        document.getElementById('out').textContent='\u274c '+e.message;
      })
      .then(function(){setBusy(false);});
  }

  function doPhoto(){
    if(busy)return;
    var s=+document.getElementById('expN').value||2;
    var ch=document.getElementById('phChroma').checked;
    setBusy(true);
    setPhase('\u6b63\u5728\u91c7\u96c6\uff08\u66dd\u5149 '+s+'s\uff09\u2026');
    document.getElementById('out').textContent='';
    fetch('/photo/capture?n='+s)
      .then(function(r){return r.json();})
      .then(function(d){
        if(!d.frames)throw new Error('\u672a\u6355\u83b7\u5230\u5e27');
        setPhase('\u6b63\u5728\u5408\u6210\uff08'+d.frames+' \u5e27\u5e73\u5747\uff09'+(ch?'\uff0b\u8272\u5f69\u8fd8\u539f':'')+'\u2026');
        return fetch('/photo/process'+(ch?'?chroma=1':''));
      })
      .then(function(r){return r.json();})
      .then(function(d){
        setPhase('');
        document.getElementById('out').textContent=
          '\u2705 '+d.file+'  \uff08'+d.frames+' \u5e27\u5e73\u5747\uff0c'+d.size_kb+' KB\uff09';
        loadMedia();
      })
      .catch(function(e){
        setPhase('');
        document.getElementById('out').textContent='\u274c '+e.message;
      })
      .then(function(){setBusy(false);});
  }

  var __media=[];
  function openMedia(i){
    var f=__media[i];
    if(!f)return;
    var lb=document.getElementById('lightbox');
    var img=document.getElementById('lightboxImg');
    var vid=document.getElementById('lightboxVid');
    var url='/media/'+f.kind+'/'+encodeURIComponent(f.name);
    if(f.kind==='photo'){
      img.style.display='';
      vid.style.display='none';
      img.src=url;
      lb.style.display='flex';
    } else {
      img.style.display='none';
      vid.style.display='block';
      vid.src=url;
      vid.controls=true;
      lb.style.display='flex';
      vid.play().catch(function(){});
    }
  }

  function loadMedia(){
    fetch('/media?kind=all')
      .then(function(r){return r.json();})
      .then(function(d){
        var g=document.getElementById('mediaGrid');
        if(!g) return;
        if(!d.files || !d.files.length){
          g.innerHTML='<span class="cap">(\u6682\u65e0\u6587\u4ef6)</span>';
          __media=[];
          return;
        }
        __media=d.files;
        g.innerHTML=d.files.map(function(f,i){
          var cap=f.name.replace(/^(dashcam|photo)_/,'');
          var body=(f.kind==='photo')
            ? '<img src="/media/photo/'+encodeURIComponent(f.name)+'" loading="lazy">'
            : '<video src="/media/video/'+encodeURIComponent(f.name)+'" muted preload="metadata">';
          var ico=(f.kind==='photo')?'\U0001F4F7':'\u25BC';
          return '<div class="mi" onclick="openMedia('+i+')">'
            +body
            +'<div class="cap"><b>'+ico+'</b> '+cap+'<br>'+f.size_kb+' KB</div></div>';
        }).join('');
      })
      .catch(function(e){
        var g=document.getElementById('mediaGrid');
        if(g) g.innerHTML='<span class="cap">\u5217\u8868\u52a0\u8f7d\u5931\u8d25</span>';
      });
  }
  loadMedia();

  setInterval(function(){
    fetch('/stat')
      .then(function(r){return r.json();})
      .then(function(d){
        document.getElementById('m').textContent =
          d.frame_kb+' KB/\u5e27 \u00b7 '+(d.seq?d.fps.toFixed(1):'0')+' fps \u00b7 \u5171 '
          +d.seq+' \u5e27 \u00b7 \u8bfb '+d.reads
          +(d.errors?('  \u9519\u8bef '+d.errors):'')
          +((typeof d.chroma!=='undefined'&&d.chroma)?'  \u00b7 \u8272\u5f69\u8fd8\u539f\u53ef\u7528':'');
      })
      .catch(function(){});
  },1000);
</script>
""".encode("utf-8")



def _quick_record(src, seconds):
    """HTTP 端点用的快速录制（写 captured/video/）"""
    _mkdirs()
    t0 = time.time()
    frames = []
    last = -1
    while time.time() - t0 < seconds:
        f, seq = src.get()
        if f and seq != last:
            last = seq
            frames.append((time.time(), f))
        else:
            time.sleep(0.005)
    if not frames:
        return None, 0, "none"
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(t0))
    out_path = os.path.join(VIDEO_DIR, "dashcam_%s.mp4" % ts)
    w = MjpegMp4Writer(fps_hint=max(1.0, src.fps or 20.0))
    for t, jpg in frames:
        if _CHROMA_ON:
            jpg = chroma_fix_jpeg_bytes(jpg)
        w.add(jpg, t)
    w.finalize(out_path)
    return out_path, len(frames), "mjpeg-mp4"


_PHOTO_PENDING = {"frames": None}
_DIAG_BUF = ''  # 存拍照中间结果

# ── 色彩还原（离线；优先 GPU OpenCL，回退 numpy CPU）──
_CHROMA_ON = False
_CHROMA_ENGINE = None  # "opencl" | "numpy"

def enable_chroma(force_engine=None):
    """--chroma：拍照/录制完成后离线跑色彩还原。"""
    global _CHROMA_ON, _CHROMA_ENGINE
    _CHROMA_ON = True
    eng = force_engine
    if eng is None:
        # 试 OpenCL，不行回退 numpy
        try:
            import pytools.persistent_dict as pd
            import os as _os
            fallback = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".pytools_cache")
            _os.makedirs(fallback, exist_ok=True)
            _orig = pd._PersistentDictBase.__init__
            def _patched(self, *a, **kw):
                kw["container_dir"] = fallback
                _orig(self, *a, **kw)
            pd._PersistentDictBase.__init__ = _patched
            import chroma_restore_opencl as _ocl
            _ocl._init()  # 预热（选 GPU、编译 kernel、上传 LUT）
            eng = "opencl"
        except Exception:
            eng = "numpy"
    _CHROMA_ENGINE = eng
    print("色彩还原 : 已开启（%s 引擎%s）"
          % (eng, "，离线 ~16ms/帧 GPU" if eng == "opencl" else "，离线 ~1.5s/帧 CPU"))

def chroma_fix_jpeg_bytes(jpeg_bytes, force=False):
    if not (force or _CHROMA_ON) or jpeg_bytes is None:
        return jpeg_bytes
    try:
        if _CHROMA_ENGINE == "opencl":
            import chroma_restore_opencl
            out, _ = chroma_restore_opencl.apply(jpeg_bytes, quality=95)
            return out
        import chroma_restore
        return chroma_restore.apply(jpeg_bytes, quality=95)
    except Exception:
        import traceback
        traceback.print_exc()
        with open(os.path.join(HERE, "_err.log"), "a", encoding="utf-8") as _f:
            _f.write("chroma_fix_jpeg_bytes: " + traceback.format_exc() + "\n")
        return jpeg_bytes

def chroma_available():
    try:
        import chroma_restore
        return True
    except Exception:
        try:
            import chroma_restore_opencl
            return True
        except Exception:
            return False

def chroma_fix_file(path, force=False):
    """就地对文件跑色彩还原，留 .pre 备份。"""
    if not (force or _CHROMA_ON) or not path or not os.path.exists(path):
        return path
    with open(path, "rb") as f:
        data = f.read()
    try:
        fixed = chroma_fix_jpeg_bytes(data, force=True)
    except Exception as e:
        print("!! 色彩还原失败：%s" % e)
        return path
    pre = path + ".pre"
    if not os.path.exists(pre):
        with open(pre, "wb") as f:
            f.write(data)
    with open(path, "wb") as f:
        f.write(fixed)
    print("  色彩还原：%s（%.1f KB → %.1f KB，备份 %s）"
          % (os.path.basename(path), len(data)//1024, len(fixed)//1024,
             os.path.basename(pre)))
    return path

def _quick_photo_capture(src, exposure_s):
    """只抓 N 秒的帧，不做平均。返回帧数。"""
    _mkdirs()
    t0 = time.time()
    frames = []
    last = -1
    while time.time() - t0 < exposure_s:
        f, seq = src.get()
        if f and seq != last:
            last = seq
            frames.append(f)
        else:
            time.sleep(0.005)
    if not frames:
        return 0
    _PHOTO_PENDING["frames"] = frames
    return len(frames)


def _quick_photo_process():
    """对上一步抓的帧做逐像素平均合成，保存 JPEG。返回 (file, frames, size_kb)。"""
    frames = _PHOTO_PENDING.get("frames")
    if not frames:
        return None, 0, 0
    avg_bytes, w, h, is_jpeg = _average_photos(frames)
    ts = time.strftime("%Y%m%d-%H%M%S")
    if is_jpeg:
        out_path = os.path.join(PHOTO_DIR, "photo_%s.jpg" % ts)
        with open(out_path, "wb") as fp:
            fp.write(avg_bytes)
    else:
        try:
            jpg = _rgb_to_jpeg(avg_bytes, w, h, 90)
            out_path = os.path.join(PHOTO_DIR, "photo_%s.jpg" % ts)
            with open(out_path, "wb") as fp:
                fp.write(jpg)
        except ImportError:
            out_path = os.path.join(PHOTO_DIR, "photo_%s.raw" % ts)
            with open(out_path, "wb") as fp:
                fp.write(avg_bytes)
    try:
        if _CHROMA_ON:
            chroma_fix_file(out_path)
    except Exception:
        import traceback
        traceback.print_exc()
        with open(os.path.join(HERE, "_err.log"), "a", encoding="utf-8") as _f:
            _f.write("chroma_fix_file: " + traceback.format_exc() + "\n")
    finally:
        _PHOTO_PENDING["frames"] = None
    size_kb = int(os.path.getsize(out_path) / 1024) if out_path else 0
    return out_path, len(frames), size_kb



def serve(src, host, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            global _CHROMA_ON
            global _DIAG_BUF
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(PAGE)
                return

            if p == "/stat":
                f, seq = src.get()
                body = ('{"seq":%d,"fps":%.2f,"reads":%d,"errors":%d,"frame_kb":%d,"crop_h":%d,"chroma":%s}'
                        % (seq, src.fps, src.reader.reads, src.reader.errors,
                           (len(f) // 1024) if f else 0, CROP_H,
                           "true" if chroma_available() else "false")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/record":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
                n = min(int(q.get("n", "30")), 600)
                ch = q.get("chroma") == "1"
                prev = _CHROMA_ON
                _CHROMA_ON = prev or ch
                src.pause_stream()
                err = None
                try:
                    out_path, nframes, m = _quick_record(src, n)
                except Exception:
                    import traceback; traceback.print_exc()
                    err = "record failed: " + traceback.format_exc(limit=2)
                    out_path, nframes, m = None, 0, "error"
                finally:
                    src.resume_stream()
                    _CHROMA_ON = prev
                if err:
                    body = ('{"file":"","frames":0,"method":"error","error":%s}'
                            % json.dumps(err[:200])).encode()
                    self.send_response(500)
                else:
                    size_kb = int(os.path.getsize(out_path) / 1024) if out_path else 0
                    body = ('{"file":"%s","frames":%d,"method":"%s","size_kb":%d}'
                            % (os.path.basename(out_path) if out_path else "",
                               nframes, m, size_kb)).encode()
                    self.send_response(200 if out_path else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/photo/capture":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
                n = min(float(q.get("n", "2")), 60)
                src.pause_stream()
                try:
                    nframes = _quick_photo_capture(src, n)
                finally:
                    src.resume_stream()
                body = ('{"frames":%d}' % nframes).encode()
                self.send_response(200 if nframes else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/photo/process":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
                ch = q.get("chroma") == "1"
                prev = _CHROMA_ON
                _CHROMA_ON = prev or ch
                err = None
                try:
                    out_path, nframes, size_kb = _quick_photo_process()
                except Exception:
                    import traceback
                    err = traceback.format_exc()
                    try:
                        with open(os.path.join(HERE, "_err.log"), "a", encoding="utf-8") as _f:
                            _f.write(err + "\n")
                    except Exception:
                        pass
                    out_path, nframes, size_kb = None, 0, 0
                finally:
                    _CHROMA_ON = prev
                if err:
                    body = ('{"file":"","frames":0,"size_kb":0,"error":%s}'
                            % json.dumps(err[:300])).encode()
                    self.send_response(500)
                else:
                    body = ('{"file":"%s","frames":%d,"size_kb":%d}'
                            % (os.path.basename(out_path) if out_path else "",
                               nframes, size_kb)).encode()
                    self.send_response(200 if out_path else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/diag":
                # 浏览器端 POST 存到 _DIAG；GET 返回
                q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&") if "=" in x)
                if q.get("save") == "1":
                    # 客户端把 JSON 放 query 太长，改用简单方式：从 body？GET 无 body。
                    # 这里直接接受 base64 在 query 里
                    import base64
                    b64 = q.get("d", "")
                    try:
                        payload = base64.b64decode(b64).decode("utf-8")
                    except Exception:
                        payload = ""
                    _DIAG_BUF = payload
                    body = b'{"ok":true}'
                else:
                    body = _DIAG_BUF.encode() if _DIAG_BUF else b'{"note":"no client diag yet - open page and it will auto-submit"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/diagsave":
                import base64
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
                d = q.get("d", "")
                ok = False
                try:
                    payload = base64.b64decode(d).decode("utf-8")
                    _DIAG_BUF = payload
                    ok = True
                except Exception:
                    pass
                body = ('{"ok":%s}' % ("true" if ok else "false")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/media":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
                kind = q.get("kind", "all")
                base = os.path.abspath(CAPTURE_DIR)
                entries = []
                for sub in ("photo", "video"):
                    if kind not in ("all", sub):
                        continue
                    d = os.path.join(base, sub)
                    for fn in sorted((os.listdir(d) if os.path.isdir(d) else []),
                                     reverse=True):
                        full = os.path.join(d, fn)
                        if os.path.isfile(full) and not fn.endswith(".pre"):
                            entries.append({"kind": sub, "name": fn,
                                            "size_kb": int(os.path.getsize(full) // 1024)})
                body = ('{"files":%s}' % json.dumps(entries)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if p.startswith("/media/"):
                rel = p[len("/media/"):]
                base = os.path.abspath(CAPTURE_DIR)
                full = os.path.abspath(os.path.join(base, rel))
                if not full.startswith(base + os.sep) or not os.path.isfile(full):
                    self.send_error(404)
                    return
                ext = os.path.splitext(full)[1].lower()
                ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                         "png": "image/png", "mp4": "video/mp4"}.get(ext,
                                                                      "application/octet-stream")
                with open(full, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return

            if p == "/snapshot":
                f, _ = src.get()
                if not f:
                    self.send_error(503, "no frame yet")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(f)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(f)
                return

            if p == "/stream":
                self.send_response(200)
                self.send_header("Age", "0")
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                last = -1
                try:
                    while src.running:
                        if not src.stream_gate.is_set():
                            time.sleep(0.05)
                            continue
                        f, seq = src.get()
                        if f and seq != last:
                            last = seq
                            self.wfile.write(b"--frame\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(f))
                            self.wfile.write(f)
                            self.wfile.write(b"\r\n")
                        else:
                            time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return

            self.send_error(404)

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv


# ────────────────────────────────────────────────────────────── main
def main():
    ap = argparse.ArgumentParser(
        description="行车记录仪实时画面（读 elen1 槽位，零驱动改动）")
    ap.add_argument("-d", "--device", help=r"手动指定 elen1 路径，如 H:\elen1")
    ap.add_argument("--probe", action="store_true", help="自检：定位设备 + 测帧率")
    ap.add_argument("--fps", action="store_true", help="只测帧率（10 秒）")
    ap.add_argument("--serve", action="store_true", help="★ 起本机 MJPEG 服务")
    ap.add_argument("-p", "--port", type=int, default=8081)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--save", action="store_true", help="存帧到 frames/")
    ap.add_argument("-n", "--count", type=int, default=30)
    ap.add_argument("--interval", type=float, default=0.0,
                    help="存帧间隔秒（0 = 尽可能快）")
    ap.add_argument("--crop", type=int, default=0, metavar="N",
                    help="裁掉画面底部 N 像素（用来去掉设备烧进 JPEG 的那行 OSD；"
                         "需要 Pillow，实测该行在 y=674..719，裁 80 足够）")
    ap.add_argument("--crop-quality", type=int, default=CROP_Q,
                    help="裁剪后重编码质量，默认 %d" % CROP_Q)
    ap.add_argument("--record", action="store_true",
                    help="★ 录制模式：抓 N 秒存成 .mp4（MJPEG 封装，零依赖，输出到 captured/video/）")
    ap.add_argument("-t", "--rec-seconds", type=int, default=30,
                    help="--record 录制秒数，默认 30")
    ap.add_argument("--photo", action="store_true",
                    help="★ 拍照模式：抓 N 秒内全部帧逐像素平均合成，输出到 captured/photo/")
    ap.add_argument("--exposure", type=float, default=1.0,
                    help="--photo 曝光秒数（平均合成时间窗口），默认 1.0")
    ap.add_argument("--chroma", action="store_true",
                    help="拍照/录制完成后离线过一遍色彩还原（方案 A，来自 色彩还原/chroma_restore.py）")
    args = ap.parse_args()

    if args.chroma:
        try:
            enable_chroma()
        except ImportError:
            print("!! --chroma 需要 numpy + Pillow：   pip install numpy pillow")
            return 4

    if args.crop:
        try:
            enable_crop(args.crop, args.crop_quality)
        except ImportError:
            print("!! --crop 需要 Pillow：   pip install pillow")
            print("   （不加 --crop 时本工具不需要任何第三方库。）")
            return 4
        print("底部裁剪 : 已开启，裁掉 %d 像素（OSD 那一行）" % args.crop)
        print()

    path = find_elen1(args.device)
    if not path:
        print("!! 没找到 elen1（需要在某个盘符根目录下，且大小正好 307200 字节）")
        print("   1) 确认记录仪已插好、盘符已出现")
        print("   2) 手动指定： python live_view.py -d H:\\elen1 --probe")
        return 2

    print("设备文件 : %s" % path)
    print("槽位大小 : %d 字节（固件固定值）" % SLOT)
    print()

    if args.probe:
        r = SlotReader(path)
        print("[自检] 连续读 8 次，看是否是不同帧……")
        seen = {}
        t0 = time.time()
        for i in range(8):
            try:
                raw = r.read()
            except OSError as e:
                print("  读失败:", e)
                return 3
            j = extract_jpeg(raw)
            h = hashlib.md5(j).hexdigest()[:12] if j else "(无 JPEG)"
            seen.setdefault(h, 0)
            seen[h] += 1
            print("  #%d  %s  %s" % (i, h, "%d 字节" % len(j) if j else ""))
        dt = time.time() - t0
        print()
        print("  8 次读到 %d 个不同帧，耗时 %.2fs（%.1f 次/秒）"
              % (len(seen), dt, 8 / dt))
        if len(seen) > 1:
            print("  ✅ 帧在变化 —— 通道通了。起服务看画面：")
            print("     python live_view.py --serve")
            print("     然后浏览器打开  http://127.0.0.1:%d" % args.port)
            print()
            print("     不想要画面底部那行 OSD（时间/经纬度/车速）就加 --crop 80：")
            print("     python live_view.py --serve --crop 80      （需要 Pillow）")
        else:
            print("  ⚠️  8 次都一样。可能是画面完全静止，或设备没在出帧。")
            print("     若同时有别的进程在读这个文件（比如另一个 --serve），")
            print("     两个读取者会互相干扰 —— 先停掉那个再重试。")
        return 0

    if args.fps:
        src = FrameSource(path)
        print("[测速] 采样 10 秒……")
        time.sleep(10)
        f, seq = src.get()
        print("  取帧 %.1f fps（%.1f 次读/秒），共 %d 帧，错误 %d"
              % (src.fps, src.reader.reads / 10.0, seq, src.reader.errors))
        src.stop()
        return 0

    if args.save:
        src = FrameSource(path)
        out = os.path.join(HERE, "frames")
        os.makedirs(out, exist_ok=True)
        print("[存帧] %d 张 -> %s" % (args.count, out))
        n = 0
        last = -1
        t0 = time.time()
        while n < args.count:
            f, seq = src.get()
            if f and seq != last:
                last = seq
                fn = os.path.join(out, "live_%04d.jpg" % n)
                with open(fn, "wb") as fh:
                    fh.write(f)
                n += 1
                if n % 10 == 0 or n == args.count:
                    print("  %d/%d   (%.1f fps)" % (n, args.count, src.fps))
            else:
                time.sleep(0.005)
            if args.interval:
                time.sleep(args.interval)
        src.stop()
        print("完成：%d 张，用时 %.1fs" % (n, time.time() - t0))
        return 0

    if args.serve:
        src = FrameSource(path)
        print("[等待首帧]……")
        for _ in range(200):
            if src.get()[0]:
                break
            time.sleep(0.05)
        f, _ = src.get()
        if not f:
            print("!! 20 秒内没有拿到帧。检查设备是否还在出帧（先跑 --probe）")
            return 4
        srv = serve(src, args.host, args.port)
        url = "http://%s:%d" % (args.host, args.port)
        print("✅ 已就绪： %s" % url)
        print("   浏览器打开即可看到实时画面。/snapshot 可单张取快照")
        print("   Ctrl-C 停止")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n停止中……")
        finally:
            src.stop()
            srv.shutdown()
        return 0

    if args.record or args.photo:
        src = FrameSource(path)
        print("[等待首帧]……")
        for _ in range(200):
            if src.get()[0]:
                break
            time.sleep(0.05)
        f, _ = src.get()
        if not f:
            print("!! 20 秒内没有拿到帧。检查设备是否还在出帧（先跑 --probe）")
            return 4
        if args.record:
            p = record(src, args.rec_seconds)
        else:
            p, n, m = photo(src, args.exposure)
            if p:
                print("  保存到：%s" % os.path.normpath(p))
        src.stop()
        return 0 if p else 4

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())


