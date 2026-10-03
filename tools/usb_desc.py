# -*- coding: utf-8 -*-
"""
只读读取指定 USB 设备的【完整配置描述符】。
目的：确认 1B3F:8301 到底
  - bDeviceClass 是什么（0x08 = 大容量存储，单功能）
  - bNumConfigurations 有几个（>1 说明存在"另一种配置"，可能是摄像头模式）
  - 配置里有多少接口、各接口的 Class / 端点类型
方法：枚举 USB Hub 设备接口 -> CreateFile -> IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION
本脚本只做查询，不发送任何修改设备状态的请求。
输出：usb_desc.txt
"""
import ctypes, os, struct, sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "usb_desc.txt")

GUID_HUB = "{f18a0e88-c30c-11d0-8815-00a0c906bed8}"
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = 0x220410
USB_DESCRIPTOR_TYPE_DEVICE = 0x01
USB_DESCRIPTOR_TYPE_CONFIGURATION = 0x02

TARGET_VID, TARGET_PID = 0x1B3F, 0x8301

cfgmgr32 = ctypes.WinDLL("cfgmgr32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def guid(s):
    g = GUID()
    s = s.strip("{}")
    p = s.split("-")
    g.Data1 = int(p[0], 16); g.Data2 = int(p[1], 16); g.Data3 = int(p[2], 16)
    rest = p[3] + p[4]
    for i in range(8):
        g.Data4[i] = int(rest[i * 2:i * 2 + 2], 16)
    return g


_fn = cfgmgr32.CM_Get_Device_Interface_List_SizeW
_fn.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(GUID),
                ctypes.c_wchar_p, ctypes.c_uint32]
_fn.restype = ctypes.c_uint32
_fn2 = cfgmgr32.CM_Get_Device_Interface_ListW
_fn2.argtypes = [ctypes.POINTER(GUID), ctypes.c_wchar_p, ctypes.c_wchar_p,
                 ctypes.c_uint32, ctypes.c_uint32]
_fn2.restype = ctypes.c_uint32

PRESENT, ALL_DEV = 0x0, 0x1


def iface_list(guidstr, flags=PRESENT):
    g = guid(guidstr)
    sz = ctypes.c_ulong(0)
    rc = _fn(ctypes.byref(sz), ctypes.byref(g), None, flags)
    if rc != 0:
        return rc, []
    buf = ctypes.create_unicode_buffer(sz.value + 2)
    rc = _fn2(ctypes.byref(g), None, buf, sz.value + 2, flags)
    return rc, [x for x in buf[:].split("\x00") if x]


def open_dev(path):
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    OPEN_EXISTING = 3
    h = kernel32.CreateFileW(ctypes.c_wchar_p(path), GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                             OPEN_EXISTING, 0, None)
    if h == -1 or h is None:
        err = ctypes.get_last_error()
        return None, err
    return h, 0


def ioctl(h, code, buf, out_len):
    INVALID = 0xFFFFFFFF
    returned = wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(wintypes.HANDLE(h), wintypes.DWORD(code),
                                  ctypes.byref(buf), wintypes.DWORD(len(buf)),
                                  ctypes.byref(buf), wintypes.DWORD(out_len),
                                  ctypes.byref(returned), None)
    return ok, returned.value


def get_descriptor(h, port, desc_type, index, length):
    header = struct.pack("<IBBHHH", port, 0x80, 0x06,
                         (desc_type << 8) | index, 0, length)
    buf = ctypes.create_string_buffer(header + b"\x00" * length, len(header) + length)
    ok, n = ioctl(h, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, buf,
                  len(header) + length)
    if not ok or n <= len(header):
        return None, ctypes.get_last_error()
    return buf.raw[len(header):n], 0


lines = []
def W(s=""):
    lines.append(str(s))

W("=" * 78)
W("USB 描述符只读探测")
W("=" * 78)
W()

hubs = []
for flags, label in ((PRESENT, "PRESENT"), (ALL_DEV, "ALL")):
    rc, lst = iface_list(GUID_HUB, flags)
    W("CM_Get_Device_Interface_ListW(HUB, %s) rc=%d -> %d 个 hub" % (label, rc, len(lst)))
    if lst:
        hubs = lst
        break
W()
for h in hubs:
    W("  hub: %s" % h)
W()

found = False
for hp in hubs:
    h, err = open_dev(hp)
    if h is None:
        W("!! 无法打开 hub (err=%d): %s" % (err, hp))
        continue
    W("== 打开成功: %s" % hp)
    for port in range(1, 17):
        dd, e = get_descriptor(h, port, USB_DESCRIPTOR_TYPE_DEVICE, 0, 18)
        if dd is None or len(dd) < 18:
            continue
        (blen, btype, bcdUSB, bDevClass, bDevSub, bDevProto, bMaxPkt,
         vid, pid, bcdDev, iMfg, iProd, iSer, nConf) = struct.unpack_from("<BBHBBBBHHHBBBB", dd, 0)
        if vid != TARGET_VID or pid != TARGET_PID:
            continue
        found = True
        W()
        W(" " * 2 + "*" * 70)
        W("  找到目标设备！端口 = %d" % port)
        W("  bLength=%d bDescriptorType=0x%02x bcdUSB=0x%04x" % (blen, btype, bcdUSB))
        W("  bDeviceClass=0x%02x  bDeviceSubClass=0x%02x  bDeviceProtocol=0x%02x" % (bDevClass, bDevSub, bDevProto))
        W("  idVendor=0x%04X idProduct=0x%04X bcdDevice=0x%04x" % (vid, pid, bcdDev))
        W("  iManufacturer=%d iProduct=%d iSerialNumber=%d" % (iMfg, iProd, iSer))
        W("  >>> bNumConfigurations = %d  <<<" % nConf)
        # 字符串描述符
        for idx, nm in ((iMfg, "iManufacturer"), (iProd, "iProduct"), (iSer, "iSerial")):
            if idx == 0:
                W("      %-14s = (未提供)" % nm); continue
            sd, e2 = get_descriptor(h, port, 0x03, idx, 255)
            if sd:
                try:
                    txt = sd[2:2 + sd[0]].decode("utf-16-le", "ignore") if sd[1] == 3 else ""
                except Exception:
                    txt = ""
                W("      %-14s = %r" % (nm, txt))
        # 每个配置
        for ci in range(nConf):
            cd, e3 = get_descriptor(h, port, USB_DESCRIPTOR_TYPE_CONFIGURATION, ci, 4096)
            if not cd:
                W("  配置 %d：读取失败 err=%d" % (ci, e3)); continue
            (wTotal, bNumIfaces, bCfgVal, iCfg, bmAttr, bMaxPower) = struct.unpack_from("<HBBBBB", cd, 2)
            W()
            W("  --- 配置 %d ---" % ci)
            W("    wTotalLength=%d  bNumInterfaces=%d  bConfigurationValue=%d  bMaxPower=%d*2mA" % (
                wTotal, bNumIfaces, bCfgVal, bMaxPower))
            off = cd[0]
            iface = None
            while off < min(wTotal, len(cd)):
                l = cd[off]
                if l == 0:
                    break
                t = cd[off + 1]
                if t == 0x04 and l >= 9:
                    (inum, alt, neps, icls, isub, iproto, iif) = struct.unpack_from("<BBBBBBB", cd, off + 2)
                    iface = inum
                    W("    [接口 %d alt=%d] class=0x%02x sub=0x%02x proto=0x%02x 端点=%d" % (
                        inum, alt, icls, isub, iproto, neps))
                elif t == 0x05 and l >= 7:
                    (eaddr, eattr, wMax, bInterval) = struct.unpack_from("<BBHB", cd, off + 2)
                    kind = {0: "Control", 1: "Iso", 2: "Bulk", 3: "Interrupt"}.get(eattr & 3, "?")
                    W("       端点 0x%02x  %-9s  maxpkt=%d interval=%d" % (eaddr, kind, wMax, bInterval))
                elif t == 0x0b and l >= 8:
                    (bFirst, bCount, bCls, bSub, bProto) = struct.unpack_from("<BBBBB", cd, off + 2)
                    W("       IAD: 首个接口=%d 数量=%d class=0x%02x sub=0x%02x proto=0x%02x" % (
                        bFirst, bCount, bCls, bSub, bProto))
                    if bCls == 0x0E:
                        W("       >>> 这是 UVC 视频接口集合！")
                off += l
        W(" " * 2 + "*" * 70)
    kernel32.CloseHandle(wintypes.HANDLE(h))

if not found:
    W()
    W("!! 未在任何 hub 端口上找到 VID_%04X&PID_%04X（可能 hub 句柄打不开或端口 >16）" % (TARGET_VID, TARGET_PID))

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
sys.stdout.reconfigure(encoding="utf-8")
print("\n".join(lines))
