#!/usr/bin/env python3
"""NAND 闪存兼容性检查 —— 专治「同机型换个闪存颗粒就刷砖」。

## 这个文件解决什么问题

HG5585F / HG5382A 这类 AN7581 光猫用的是**并行 NAND**（TSOP48），
同型号不同批次会焊不同厂家的颗粒。实测两种最常见：

    Toshiba TC58NVG1S3HTAI0   2 Gbit = 256 MiB, 2048+128
    Winbond W29N02KVSIAF      2 Gbit = 256 MiB, 2048+128

两者的几何参数**完全一样**，所以在 /proc/mtd、/sys/class/mtd 上看不出
区别 —— 但 ECC 要求不同，这正是变砖的根源。

## 根因（实测，二进制级证据）

BL2（preloader）内部烧着一张**硬编码的闪存芯片表**，每条带两个 ECC 字段：

    min_ecc_req      —— 这颗芯片至少需要几 bit ECC
    soc_ecc_ability  —— 控制器读写这颗芯片时**实际使用**的 ECC 位数

`soc_ecc_ability` 决定了 NAND 页里 ECC 校验字节的**排布方式**。原厂固件
（BL2 + 内核）整片闪存按 8 bit ECC 读写，所以 stock2ubi（跑在原厂内核上）
刷进去的 preloader / FIP，页数据和 OOB 都是 8 bit 排布。

uboot-an758x 从 **v2026.07-3** 起，把 W29N02KVSIAF 的 soc_ecc 从 8 改成 4
（依据是 Winbond AN0000025 里 ONFI 标称的 4bit/544B）：

    v2026.07-1  W29N02KVSIAF min_ecc=4  soc_ecc=8   ← 与原厂一致，能读
    v2026.07-2  W29N02KVSIAF min_ecc=4  soc_ecc=8   ← 与原厂一致，能读
    v2026.07-3  W29N02KVSIAF min_ecc=4  soc_ecc=4   ← 与原厂不一致，读不出
    v2026.07-4  W29N02KVSIAF min_ecc=4  soc_ecc=4   ← 与原厂不一致，读不出

    v2026.07-1/2 表里 9 条，没有 W29N02KVSIAE
    v2026.07-3/4 表里 15 条，新增 W29N02KVSIAE（soc_ecc=8）

（以上是直接解 preloader.bin 的 LZMA 流读出来的，不是看 commit 猜的。）

于是 BL2 用 4 bit 的方式去解 8 bit 写进去的数据 → 大量不可纠正错误 →
扫不到 UBI 里的 fip 卷 →

    ERROR: UBI error: No volume named fip could be found
    ERROR: BL2: Failed to load image id 3 (-2)
    ERROR: Stored BL31 + U-Boot FIP failed (-2) Press x to load ... via XMODEM

厂商那边是**两侧都按 8 bit**：

* 厂商**内核**自己的并行 NAND 表（在 `vendor_kernel.bin` 偏移 0x9cf494，
  表项步长 0x68）：W29N02KVSIAF → min_ecc=4, soc_ecc=**8**
* 厂商 **BL2** 的表：W29N02KVSIAF → min_ecc=4, soc_ecc=**8**

stock2ubi 路径是「厂商内核**写**（8 bit）→ 新 BL2 **读**（4 bit）」，
两边错位就必砖。

**这也解释了为什么 XMODEM 能救**：那条路径里**写和读都是 BL2 自己**，
用几 bit 都自洽（BL2 把 FIP 收在内存里，绕开了厂商内核写的数据）。
所以 XMODEM 不是「运气好」，而是唯一没有跨阵营的路径。

Toshiba 那颗在四个版本里 soc_ecc 一直是 8，所以它没事 —— 这就是
「别的品牌闪存同机型正常」的原因。

## 怎么办

⚠️ **只把 BL2 改成 8 bit 是不够的。** ECC 强度必须在「所有碰这片闪存的
软件」之间一致：BL2 改 8 之后，U-Boot（仍是 4 bit）读同一片 UBI 会挂；
就算它不读、直接重刷，也是按 4 bit 写 —— 下一次重启时 8 bit 的 BL2
又读不了，等于把砖挪到下一次重启。

目前的结论：

* **可靠做法**：Winbond 机型走 XMODEM 路径（有实机成功记录，上游 issue #22），
  不要用 stock2ubi 一键刷。`stock2ubi` 流程里会自动拦并打印步骤。
* **在做的事**：把整条链（BL2 + U-Boot + OpenWrt 内核）统一到 8 bit，
  去配合厂商内核写入的那片闪存。还缺 `bl31-u-boot.fip`。
* 刷之前用 `report_preloader()` 查手上的 preloader.bin 属于哪一版；
  `patch_preloader_ecc()` 只完成其中 BL2 一层。
"""

import lzma
import os
import re
import struct

# ---------------------------------------------------------------------------
# 已知的并行 NAND 颗粒
# ---------------------------------------------------------------------------

# (mfr, dev, ext) → (厂商, 型号, 容量 MiB)
# ext 是 READID 第 3~5 字节拼出来的大端值，也就是表里的 ext_id 字段。
KNOWN_PARALLEL_NAND = {
    (0x98, 0xDA, 0x761590): ("Toshiba", "TC58NVG1S3HTAI0", 256),
    (0x98, 0xDA, 0x769590): ("Toshiba", "TC58NVG2S3HTAI0", 512),
    (0xEF, 0xDA, 0x069510): ("Winbond", "W29N02KVSIAF", 256),
    (0xEF, 0xDA, 0x079510): ("Winbond", "W29N02KVSIAE", 256),
    (0xEF, 0xDA, 0x049590): ("Winbond", "W29N02GV", 256),
    (0xEF, 0xDA, 0x0B9590): ("Winbond", "W29N04GV", 512),
    (0x2C, 0xDA, 0x069590): ("Micron", "MT29F02G08ABAGA", 256),
    (0xC2, 0xDA, 0x069590): ("Macronix", "MX30LF2G18AC", 256),
    (0x01, 0xDA, 0x0A9590): ("Spansion", "S34ML02G2", 256),
}

# 实测会踩「4/8 bit ECC 不一致」这个坑的颗粒：Winbond W29N02KVSIAF。
WINBOND_ECC_TRAP_IDS = {(0xEF, 0xDA, 0x069510)}

# uboot-an758x 从这一版起把 W29N02KVSIAF 的 soc_ecc 从 8 改成 4。
BAD_ECC_SINCE = "v2026.07-3"

# 已验证 soc_ecc=8（与原厂一致）的版本。
KNOWN_GOOD_ECC_VERSIONS = ("v2026.07-1", "v2026.07-2")

# 闪存表里名字指针的基址（BL2 链接地址）。解析时用它把 u32 指针还原成
# blob 内偏移，比「猜 stride」稳 —— 条目结构大小随编译开关变化，但
# 字段相对条目的偏移不变。
_BL2_NAME_BASE = 0x81000000

# 条目里各字段相对条目起点的偏移（已用 v2026.07-1..4 四个版本交叉验证）
_OFF_PTR = 0x04       # u32 名字指针
_OFF_SIZE = 0x08      # u32 容量（字节）
_OFF_PAGE = 0x0C      # u32 页大小
_OFF_ERASE = 0x10     # u32 块大小
_OFF_OOB = 0x14       # u32 OOB 大小
_OFF_EXT = 0x34       # u32 扩展 ID
_OFF_MINECC = 0x3C    # u8  min_ecc_req
_OFF_SOCECC = 0x40    # u32 高位字节 = soc_ecc_ability

_VALID_SIZE = {0x00800000, 0x01000000, 0x02000000, 0x04000000,
               0x08000000, 0x10000000}
_VALID_PAGE = {512, 2048, 4096, 8192}
_VALID_ERASE = {0x2000, 0x4000, 0x8000, 0x10000, 0x20000, 0x40000, 0x80000}


# ---------------------------------------------------------------------------
# preloader.bin 解析
# ---------------------------------------------------------------------------

def _lzma_streams(data):
    """找出容器里所有的 LZMA-alone 流，返回 [(偏移, 解压后 bytes)]。

    Airoha 的 preloader 是 TRX 风格容器：头部 + 若干 LZMA 流。
    闪存表在其中的一个流里。不依赖固定偏移 —— 换版本也能用。
    """
    out = []
    i = 0
    end = len(data) - 13
    while i < end:
        if data[i] != 0x5D:          # LZMA-alone 的 lc/lp/pb 属性字节
            i += 1
            continue
        try:
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
            blob = dec.decompress(data[i:])
        except Exception:  # noqa: BLE001
            i += 1
            continue
        if len(blob) > 512:
            out.append((i, blob))
        i += 1
    return out


def _scan_entries(blob):
    """在一段解压后的 BL2 数据里扫出所有闪存表条目。

    识别方式：条目的 +4 是个指向本 blob 的指针（0x81xxxxxx），
    且 +8..+0x18 四个 u32 是一组合法的 size/page/erase/oob。
    这比「头部数字段 + 算 stride」稳，因为结构体大小会随编译变化
    （实测同一份源码不同版本 stride 有 41/49/70/76 多种）。
    """
    out = []
    limit = len(blob) - 0x44
    for o in range(0, limit, 4):
        ptr = struct.unpack("<I", blob[o + _OFF_PTR:o + _OFF_PTR + 4])[0]
        if not (_BL2_NAME_BASE <= ptr < _BL2_NAME_BASE + len(blob)):
            continue
        size, page, erase, oob = struct.unpack(
            "<IIII", blob[o + _OFF_SIZE:o + _OFF_SIZE + 16])
        if size not in _VALID_SIZE or page not in _VALID_PAGE:
            continue
        if erase not in _VALID_ERASE or oob not in (16, 64, 128, 224, 256):
            continue
        noff = ptr - _BL2_NAME_BASE
        name = blob[noff:noff + 40].split(b"\x00")[0].decode("latin1", "replace")
        if not name or not name.isprintable() or not name[0].isalnum():
            continue
        out.append({
            "offset": o,
            "mfr": blob[o],
            "dev": blob[o + 1],
            "name": name,
            "size": size,
            "page": page,
            "erase": erase,
            "oob": oob,
            "ext": struct.unpack("<I", blob[o + _OFF_EXT:o + _OFF_EXT + 4])[0],
            "min_ecc": blob[o + _OFF_MINECC],
            "soc_ecc": (struct.unpack(
                "<I", blob[o + _OFF_SOCECC:o + _OFF_SOCECC + 4])[0] >> 16) & 0xFF,
        })
    return out


def parse_preloader(path):
    """解析一个 preloader.bin，返回闪存表信息。

    返回 dict：path / size / version / build / entries / table_off
    entries 是所有扫到的条目（SPI 与并行都在同一张表里）。
    """
    with open(path, "rb") as f:
        data = f.read()

    info = {
        "path": os.path.abspath(path),
        "size": len(data),
        "version": "",
        "build": "",
        "entries": [],
        "table_off": None,
    }

    m = re.search(rb"v20\d\d\.\d\d-\d+", data[:0x20000])
    if m:
        info["version"] = m.group().decode("ascii", "replace")
    m = re.search(rb"Built : ([0-9: ]+, \w+ \d+ \d+)", data[:0x20000])
    if m:
        info["build"] = m.group(1).decode("ascii", "replace")

    for off, blob in _lzma_streams(data):
        entries = _scan_entries(blob)
        if entries:
            info["entries"] = entries
            info["table_off"] = off
            break
    return info


def find_entry(info, mfr, dev, ext=None):
    """在解析结果里找一颗芯片；ext 给了就精确匹配扩展 ID。"""
    for e in info.get("entries") or []:
        if e["mfr"] != mfr or e["dev"] != dev:
            continue
        if ext is not None and e.get("ext") != ext:
            continue
        return e
    return None


def winbond_ecc_verdict(info):
    """判断这份 preloader 用在 Winbond W29N02KVSIAF 上会不会砖。

    返回 (safe, message, entry)：
        True  → soc_ecc=8，与原厂一致，可以刷
        False → soc_ecc=4，刷了 BL2 读不出 FIP（必砖）
        None  → 表里没有这颗芯片 / 读不到 ECC 字段，无法判断
    """
    entry = find_entry(info, 0xEF, 0xDA, 0x069510)
    ver = info.get("version") or "（版本号未识别）"
    if entry is None:
        return None, (
            f"{ver}：闪存表里**没有** Winbond W29N02KVSIAF 这一条，"
            "无法判断它在 Winbond 机型上的行为。\n"
            "建议换用官方发布的 preloader.bin。"), None

    soc = entry.get("soc_ecc")
    if soc is None:
        return None, f"{ver}：解析出了 W29N02KVSIAF 条目，但读不到 ECC 字段。", entry

    if soc >= 8:
        return True, (
            f"{ver}：W29N02KVSIAF 的 soc_ecc_ability = {soc} bit，"
            "与原厂固件的 8 bit 一致，Winbond 机型可以刷。"), entry

    return False, (
        f"{ver}：W29N02KVSIAF 的 soc_ecc_ability = {soc} bit，"
        "而原厂固件整片闪存是按 8 bit 写的。\n"
        "BL2 用 4 bit 去解 8 bit 的数据会大量报不可纠正错误，"
        "扫不到 UBI 里的 fip 卷，重启后卡在：\n"
        "    ERROR: Stored BL31 + U-Boot FIP failed (-2) "
        "Press x to load ... via XMODEM\n"
        "也就是说：Winbond 机型用这一份刷了会砖。\n"
        "解法：改用 uboot-bin/修改版/ 里那一套（8bit 读 / 4bit 写）的\n"
        "    preloader / firstblock / bl31-u-boot.fip\n"
        "—— 它们是原厂内核写下的那份数据的正确解码方式。\n"
        "已经刷成砖了：接 TTL，看到 Press x ... via XMODEM 时按 x，"
        "发送「官方版」的 bl31-u-boot.fip 进内存即可进恢复模式。"), entry


def report_preloader(path):
    """打印一份 preloader 的闪存表摘要，返回 (safe, msg, info)。"""
    info = parse_preloader(path)
    print("=" * 70)
    print(f"  preloader 闪存表检查: {os.path.basename(path)}")
    print("=" * 70)
    print(f"  文件大小 : {info['size']} 字节")
    print(f"  版本     : {info['version'] or '（未识别）'}")
    print(f"  构建时间 : {info['build'] or '（未识别）'}")
    if info["table_off"] is not None:
        print(f"  闪存表   : LZMA 流 @ 0x{info['table_off']:x}")
    print(f"  表条目   : {len(info['entries'])} 条")

    interesting = [e for e in info["entries"] if e["page"] == 2048
                   and e["mfr"] in (0xEF, 0x98)]
    if interesting:
        print("\n  2048 页的 Winbond / Toshiba 条目：")
        print(f"    {'芯片':22s} {'oob':>4s} {'min_ecc':>8s} {'soc_ecc':>8s}  说明")
        for e in interesting:
            note = ""
            if (e["mfr"], e["dev"], e["ext"]) in WINBOND_ECC_TRAP_IDS:
                note = "← Winbond 256M，踩坑型号"
            elif e["mfr"] == 0x98 and e["dev"] == 0xDA:
                note = "← Toshiba 256M，四版一致正常"
            print(f"    {e['name']:22s} {e['oob']:4d} "
                  f"{e['min_ecc']:>8d} {e['soc_ecc']:>8d}  {note}")

    safe, msg, _entry = winbond_ecc_verdict(info)
    print()
    tag = {True: "[OK]  ", False: "[危险]", None: "[未知]"}[safe]
    print(f"  {tag} {msg}")
    print()
    return safe, msg, info


# ---------------------------------------------------------------------------
# 设备侧：读当前机器焊的是哪颗颗粒
# ---------------------------------------------------------------------------

_RE_PAR_PROBE = re.compile(
    r"parallel_nand_probe:\s*mfr_id=0x([0-9a-fA-F]+),\s*dev_id=0x([0-9a-fA-F]+),"
    r"\s*ext_id=0x([0-9a-fA-F]+)")
_RE_SPI_PROBE = re.compile(
    r"spi_nand_probe:\s*mfr_id=0x([0-9a-fA-F]+),\s*dev_id=0x([0-9a-fA-F]+)")


def detect_nand_chip(run_cmd, log=None):
    """在设备上读当前闪存颗粒，返回 dict 或 None。

    依次尝试（都只读，不改设备）：
      1. dmesg 里的 parallel_nand_probe / spi_nand_probe 探测行
      2. /sys/class/mtd/mtd0/ 的几何参数

    为什么优先 dmesg：这是唯一直接给出厂商 ID 的地方。sysfs 只有几何参数，
    而 Winbond 和 Toshiba 那两颗的几何参数**完全一样**，区分不了。

    run_cmd: 形如 flash.run_cmd(tn, cmd) 的调用对象。
    """
    say = log or (lambda *_: None)
    try:
        out = run_cmd(
            "dmesg | grep -i -E 'parallel_nand_probe|spi_nand_probe' | head -20",
            30) or ""
    except Exception:  # noqa: BLE001
        out = ""

    m = _RE_PAR_PROBE.search(out)
    kind = "parallel"
    if not m:
        m = _RE_SPI_PROBE.search(out)
        kind = "spi"
    if m:
        mfr = int(m.group(1), 16)
        dev = int(m.group(2), 16)
        ext = int(m.group(3), 16) if m.lastindex and m.lastindex >= 3 else None
        vendor, model, cap = KNOWN_PARALLEL_NAND.get(
            (mfr, dev, ext), ("未知厂商", "未知型号", 0))
        chip = {"kind": kind, "mfr": mfr, "dev": dev, "ext": ext,
                "vendor": vendor, "model": model,
                "capacity_mib": cap, "source": "dmesg"}
        say(f"[*] 闪存颗粒: {vendor} {model}"
            f"（ID {mfr:02x}:{dev:02x}:{ext or 0:06x}）")
        return chip

    try:
        geo = run_cmd(
            "for f in type writesize oobsize erasesize; do "
            "printf '%s=' $f; cat /sys/class/mtd/mtd0/$f 2>/dev/null; done", 20) or ""
    except Exception:  # noqa: BLE001
        geo = ""
    fields = dict(re.findall(r"(\w+)=(\d+)", geo))
    if fields.get("type") == "nand" and fields.get("writesize") == "2048":
        say(f"[*] 闪存: NAND 2048+{fields.get('oobsize', '?')}"
            f"，块 {fields.get('erasesize', '?')} 字节"
            "（读不到厂商 ID，无法区分 Winbond / Toshiba）")
        return {"kind": "nand", "mfr": None, "dev": None, "ext": None,
                "vendor": "", "model": "", "capacity_mib": 0,
                "oob": int(fields.get("oobsize") or 0), "source": "sysfs"}
    return None


def chip_is_winbond_ecc_trap(chip):
    """这颗芯片是不是「4/8 bit ECC 不一致会砖」的那一档。

    chip 为 None（读不到）时返回 False —— 交给调用方按「未知」处理，
    不要因为读不到就吓人。
    """
    if not chip:
        return False
    if (chip.get("mfr"), chip.get("dev"), chip.get("ext")) in WINBOND_ECC_TRAP_IDS:
        return True
    return chip.get("vendor") == "Winbond" and chip.get("dev") == 0xDA


def preloader_safe_for_chip(preloader_path, chip):
    """结合「设备颗粒」和「preloader 版本」给出最终判定。

    返回 (ok, message)。ok=False 表示应该拦下来。
    """
    if not preloader_path or not os.path.isfile(preloader_path):
        return True, "没有提供 preloader 文件，跳过 ECC 兼容性检查。"

    try:
        info = parse_preloader(preloader_path)
    except Exception as e:  # noqa: BLE001
        return True, f"无法解析 {os.path.basename(preloader_path)}（{e}），跳过检查。"

    safe, msg, _entry = winbond_ecc_verdict(info)

    if chip_is_winbond_ecc_trap(chip):
        if safe is False:
            return False, (
                "本机是 Winbond 闪存，而你选的 preloader 用的是 4 bit ECC，"
                "刷了 BL2 会读不出 FIP（必砖）。\n" + msg)
        if safe is True:
            return True, f"本机是 Winbond 闪存，preloader 的 ECC 设置正确。\n{msg}"
        return True, "本机是 Winbond 闪存，但无法确认这份 preloader 的 ECC 设置。\n" + msg

    if safe is False:
        return True, (
            "提示：这份 preloader 对 Winbond W29N02KV 是 4 bit ECC"
            "（那批机器会砖），本机不是 Winbond，可以继续。\n" + msg)

    return True, msg


# ---------------------------------------------------------------------------
# 修补 preloader：把 W29N02KVSIAF 的 soc_ecc 改回 8
# ---------------------------------------------------------------------------

def _find_entry_offset(blob, name):
    """在解压后的 BL2 里找某个芯片条目的偏移（-1 表示没有）。"""
    for o in range(0, len(blob) - 0x44, 4):
        ptr = struct.unpack("<I", blob[o + _OFF_PTR:o + _OFF_PTR + 4])[0]
        if not (_BL2_NAME_BASE <= ptr < _BL2_NAME_BASE + len(blob)):
            continue
        size, page = struct.unpack("<II", blob[o + _OFF_SIZE:o + _OFF_SIZE + 8])
        if size not in _VALID_SIZE or page not in _VALID_PAGE:
            continue
        noff = ptr - _BL2_NAME_BASE
        if blob[noff:noff + 40].split(b"\x00")[0] == name:
            return o
    return -1


def patch_preloader_ecc(src_path, dst_path, ecc=8, log=print):
    """把 preloader 里 W29N02KVSIAF 的 soc_ecc_ability 改成指定值。

    用途：v2026.07-3/-4 把 W29N02KVSIAF 的 soc_ecc 改成了 4，与出厂固件
    整片按 8 bit 写入的 OOB 排布不兼容。但 v2026.07-4 同时带了「FIP 在
    UBI 卷里时不再因 boot 分区预读失败进 XMODEM」的修复，而 v2026.07-1/-2
    没有这个修复 —— 所以没有一个现成版本同时满足两点。这个函数就地打
    补丁：取 v4 的其它修复，只把 ECC 字段改回 8。

    为什么可行：闪存表在容器里的一个独立 LZMA 流里，重新压缩后比原来
    更短（实测短 30~35 字节），原地覆盖 + 补零即可，不影响后面的流和末尾
    签名区。容器头 0x10/0x40 那两段在四个版本里字节完全相同，说明它们
    不是对 payload 的完整性校验。

    返回 (ok, message)。
    """
    with open(src_path, "rb") as f:
        data = bytearray(f.read())

    target = None
    for off, blob in _lzma_streams(data):
        if _find_entry_offset(blob, b"W29N02KVSIAF") >= 0:
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
            dec.decompress(bytes(data[off:]))
            clen = len(data) - off - len(dec.unused_data)
            target = (off, bytearray(blob), clen)
            break
    if target is None:
        return False, "这份 preloader 里找不到 W29N02KVSIAF 条目，无法修补。"

    off, blob, clen = target
    ent = _find_entry_offset(blob, b"W29N02KVSIAF")
    old = blob[ent + _OFF_SOCECC + 2]        # 结构体里 soc_ecc 在 +0x42
    if old == ecc:
        return True, f"无需修补：W29N02KVSIAF 的 soc_ecc 已经是 {ecc}。"

    blob[ent + _OFF_SOCECC + 2] = ecc
    new = lzma.compress(bytes(blob), format=lzma.FORMAT_ALONE,
                        filters=[{"id": lzma.FILTER_LZMA1, "preset": 9}])
    if len(new) > clen:
        return False, (
            f"重新压缩后 {len(new)} 字节，超过原来的 {clen} 字节，"
            "无法原地覆盖（换压缩参数或改用完整重打包）。")

    data[off:off + clen] = new + b"\x00" * (clen - len(new))
    with open(dst_path, "wb") as f:
        f.write(data)

    check = parse_preloader(dst_path)
    got = find_entry(check, 0xEF, 0xDA, 0x069510)
    if not got or got["soc_ecc"] != ecc:
        return False, "补丁已写入但回读校验失败，请不要使用这个文件。"

    return True, (
        f"已把 W29N02KVSIAF 的 soc_ecc_ability 从 {old} 改成 {ecc}。\n"
        f"    原文件: {os.path.abspath(src_path)}\n"
        f"    新文件: {os.path.abspath(dst_path)}\n"
        f"    压缩流 0x{off:x}：{len(new)}/{clen} 字节（余 {clen - len(new)}）\n"
        f"    其它流与末尾签名区未改动。")


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="nandcheck.py",
        description="检查 preloader 的闪存表 / ECC 设置，必要时把 Winbond "
                    "W29N02KVSIAF 的 soc_ecc 改回 8。")
    ap.add_argument("files", nargs="+", help="preloader.bin 路径")
    ap.add_argument("--fix", metavar="输出文件",
                    help="把 W29N02KVSIAF 的 soc_ecc 改成 8 并写到这个文件")
    ap.add_argument("--ecc", type=int, default=8,
                    help="配合 --fix 使用，目标 ECC 位数（默认 8）")
    a = ap.parse_args()

    rc = 0
    for p in a.files:
        safe, _msg, _info = report_preloader(p)
        if a.fix:
            ok, msg = patch_preloader_ecc(p, a.fix, ecc=a.ecc)
            print(f"  [{'✓' if ok else '✗'}] 修补: {msg}\n")
            if not ok:
                rc = 1
        elif safe is False:
            rc = 1
    sys.exit(rc)
