"""烽火 1 MiB factory 镜像生成（fiberhome-factory 的 Python 移植）。

来源：fiberhome-factory（作者 22p，GPL-2.0-only）src/model.rs + src/import.rs。
https://github.com/22p/fiberhome-factory

本文件按上游的 **GPL-2.0-only** 分发（本仓库其余文件是 AGPL-3.0-only，
两者在同一个发行包里并列）。详见 README.MD「第三方代码」一节。
用于在备份完烽火光猫后，从 mtd13 factory 分区里自动提取
factory_conf / table / wlan EEPROM，拼出 OpenWrt 用的 1 MiB factory 布局。

布局（an7581-fiberhome-ubi-parts.dtsi 的 ABI）：
    0x000000  0x0800  APONCAL PON 校准
    0x000800  0x0800  保留
    0x001000  0x1000  MT7916 EEPROM
    0x002000  0x0006  基础 MAC
    0x002006  0x000a  保留
    0x002010  0x0008  PON 序列号
    0x002018  0x0008  保留
    0x002020  0x0020  整机序列号
    0x002040  0xfdfc0 保留（0xff 填充）
"""

import struct
import zlib

FACTORY_SIZE = 0x100000
PON_OFFSET, PON_SIZE = 0x0000, 0x0800
WIFI_OFFSET, WIFI_SIZE = 0x1000, 0x1000
BASE_MAC_OFFSET, BASE_MAC_SIZE = 0x2000, 0x0006
PON_SN_OFFSET, PON_SN_SIZE = 0x2010, 0x0008
DEVICE_SN_OFFSET, DEVICE_SN_SIZE = 0x2020, 0x0020

BDBOB_MAGIC = b"fiberhomebdbob"
BDBOB_DATA_SIZE_OFFSET = 0x70
BDBOB_DATA_CRC_OFFSET = 0x74
BDBOB_HEADER_SIZE_OFFSET = 0x78
BDBOB_HEADER_CRC_OFFSET = 0x7c
BDBOB_HEADER_SIZE = 0x80
PON_CAL_MAGIC = b"APONCAL\0"
PON_CAL_VERSION = 1
PON_CAL_HEADER_SIZE = 0x80
PON_CAL_FORMAT_NATIVE = 1
PON_CAL_HEADER_CRC_OFFSET = 0x7c
MT7916_DEVICE_ID = 0x7916

CHIP_GN28L95 = 1
CHIP_UX3363 = 2


class FactoryError(Exception):
    pass


def _crc32_register(data, crc):
    """内核 crc32()：返回原始寄存器值（不做最终取反）。"""
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & (0xFFFFFFFF if crc & 1 else 0))
    return crc & 0xFFFFFFFF


def _fiberhome_crc32(data):
    """BDBOB 用：寄存器初值 0，结果取反。"""
    return _crc32_register(data, 0) ^ 0xFFFFFFFF


def _calibration_crc32(data):
    """APONCAL 用：寄存器初值 0xffffffff，结果再取反（等价标准 crc32）。"""
    return _crc32_register(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def _u16(data, off):
    if off + 2 > len(data):
        raise FactoryError("数据在 0x%x 处被截断" % off)
    return struct.unpack_from("<H", data, off)[0]


def _u32(data, off):
    if off + 4 > len(data):
        raise FactoryError("数据在 0x%x 处被截断" % off)
    return struct.unpack_from("<I", data, off)[0]


# ---------------- PON 校准（table 文件） ----------------

def _encode_pon_calibration(chip, payload):
    if len(payload) > PON_SIZE - PON_CAL_HEADER_SIZE:
        raise FactoryError("PON 校准数据超过 1920 字节")
    out = bytearray(b"\xff" * PON_SIZE)
    out[:PON_CAL_HEADER_SIZE] = b"\x00" * PON_CAL_HEADER_SIZE
    out[:len(PON_CAL_MAGIC)] = PON_CAL_MAGIC
    struct.pack_into("<H", out, 0x08, PON_CAL_VERSION)
    struct.pack_into("<H", out, 0x0a, PON_CAL_HEADER_SIZE)
    struct.pack_into("<I", out, 0x0c, chip)
    struct.pack_into("<I", out, 0x10, PON_CAL_FORMAT_NATIVE)
    struct.pack_into("<I", out, 0x14, len(payload))
    struct.pack_into("<I", out, 0x18, _calibration_crc32(payload))
    out[PON_CAL_HEADER_SIZE:PON_CAL_HEADER_SIZE + len(payload)] = payload
    struct.pack_into("<I", out, PON_CAL_HEADER_CRC_OFFSET,
                     _calibration_crc32(bytes(out[:PON_CAL_HEADER_CRC_OFFSET])))
    return bytes(out)


def _convert_bdbob(data):
    """把 table（fiberhomebdbob 容器）转成 APONCAL 容器。"""
    if len(data) != PON_SIZE:
        raise FactoryError("table 长度 %d，期望 %d" % (len(data), PON_SIZE))
    if not data.startswith(BDBOB_MAGIC):
        raise FactoryError("table 没有 fiberhomebdbob 头")
    data_size = _u32(data, BDBOB_DATA_SIZE_OFFSET)
    data_crc = _u32(data, BDBOB_DATA_CRC_OFFSET)
    header_size = _u32(data, BDBOB_HEADER_SIZE_OFFSET)
    header_crc = _u32(data, BDBOB_HEADER_CRC_OFFSET)
    if header_size != BDBOB_HEADER_SIZE or header_size + data_size != len(data):
        raise FactoryError("table 的 BDBOB 长度字段不对")
    if _fiberhome_crc32(data[:BDBOB_HEADER_CRC_OFFSET]) != header_crc:
        raise FactoryError("table 的 BDBOB 头 CRC 不匹配")
    if _fiberhome_crc32(data[header_size:header_size + data_size]) != data_crc:
        raise FactoryError("table 的 BDBOB 数据 CRC 不匹配")
    chip_byte = data[0x30]
    if chip_byte in (0, 0x28):
        chip = CHIP_GN28L95
    elif chip_byte == 0x2E:
        chip = CHIP_UX3363
    else:
        raise FactoryError("不支持的 PON BDBOB 芯片 0x%02x" % chip_byte)
    return _encode_pon_calibration(chip, data[BDBOB_HEADER_SIZE:])


def _normalize_pon_calibration(data):
    if data.startswith(PON_CAL_MAGIC):
        return data  # 已经是 APONCAL，直接用
    return _convert_bdbob(data)


# ---------------- Wi-Fi EEPROM ----------------

def _extract_wifi_eeprom(container):
    if len(container) < WIFI_SIZE:
        raise FactoryError("Wi-Fi EEPROM 不足 4096 字节")
    eeprom = container[:WIFI_SIZE]
    dev_id = struct.unpack_from("<H", eeprom, 0)[0]
    if dev_id != MT7916_DEVICE_ID:
        raise FactoryError("Wi-Fi EEPROM 设备 ID 不是 MT7916（读到 0x%04x）" % dev_id)
    return eeprom


def _derive_wifi_macs(base):
    """2.4G = base 第二字节 +1；5G = 2.4G 末字节 +8（溢出回绕 255）。"""
    wifi_2g = bytearray(base)
    wifi_2g[1] = (wifi_2g[1] + 1) & 0xFF
    wifi_5g = bytearray(wifi_2g)
    last = wifi_5g[5] + 8
    wifi_5g[5] = (last - 255) & 0xFF if last > 0xFF else last
    return bytes(wifi_2g), bytes(wifi_5g)


# ---------------- UBI / UBIFS 解析 ----------------

UBIFS_NODE_MAGIC = 0x06101831
UBI_VTBL_VOL_ID = 0x7FFFEFFF
UBI_LAYOUT_VOL_ID = 0x7FFFEFFF
UBI_VTBL_RECORD_SIZE = 172
UBI_MAX_VOLUMES = 128
UBIFS_INO_NODE = 0
UBIFS_DATA_NODE = 1
UBIFS_DENT_NODE = 2
UBIFS_XENT_NODE = 3
UBIFS_TRUN_NODE = 4
UBIFS_PAD_NODE = 5
UBIFS_SB_NODE = 6
UBIFS_MST_NODE = 7
UBIFS_REF_NODE = 8
UBIFS_IDX_NODE = 9
UBIFS_CS_NODE = 10
UBIFS_ORPH_NODE = 11
UBIFS_MAX_FILE = 16 * 1024 * 1024


def _be16(d, o):
    if o + 2 > len(d):
        raise FactoryError("数据在 0x%x 处被截断" % o)
    return struct.unpack_from(">H", d, o)[0]


def _be32(d, o):
    if o + 4 > len(d):
        raise FactoryError("数据在 0x%x 处被截断" % o)
    return struct.unpack_from(">I", d, o)[0]


def _be64(d, o):
    if o + 8 > len(d):
        raise FactoryError("数据在 0x%x 处被截断" % o)
    return struct.unpack_from(">Q", d, o)[0]


def _le16(d, o):
    return _u16(d, o)


def _le32(d, o):
    return _u32(d, o)


def _le64(d, o):
    if o + 8 > len(d):
        raise FactoryError("数据在 0x%x 处被截断" % o)
    return struct.unpack_from("<Q", d, o)[0]


def _check_crc(data, initial, expected, what):
    """initial：JFFS2 用 0（内核 crc32_le(0,…)），UBI/UBIFS 用 0xFFFFFFFF。"""
    actual = _crc32_register(data, initial)
    if actual != expected:
        raise FactoryError("%s CRC 不匹配：存 0x%08x，算 0x%08x" % (what, expected, actual))


def _ubi_decompress(data, size, kind):
    """kind: 0=raw 1=LZO 2=zlib 4=裸 deflate。"""
    if size > UBIFS_MAX_FILE:
        raise FactoryError("文件块过大")
    if kind == 0:
        out = data
    elif kind == 1:
        out = _lzo1x_decompress(data, size)
    elif kind == 2:
        out = zlib.decompress(data)
    elif kind == 4:
        out = zlib.decompressobj(-15).decompress(data)
    else:
        raise FactoryError("不支持的压缩类型 %d" % kind)
    if len(out) != size:
        raise FactoryError("解压后长度 %d 与节点头里的 %d 不符" % (len(out), size))
    return out


def _lzo1x_decompress(src, expected):
    """纯 Python 版 lzo1x 解压（只实现解压，够用即可）。"""
    out = bytearray()
    ip = 0
    n = len(src)
    if n == 0:
        return b""
    if src[0] > 17:
        t = src[0] - 17
        ip = 1
        if t < 4:
            raise FactoryError("lzo 流开头非法")
        out += src[ip:ip + t]
        ip += t
    while ip < n:
        t = src[ip]
        ip += 1
        if t >= 16:
            if t >= 64:
                m_pos = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3)
                ip += 1
                m_len = (t >> 5) - 1
            elif t >= 32:
                m_len = t & 31
                if m_len == 0:
                    while src[ip] == 0:
                        m_len += 255
                        ip += 1
                    m_len += 31 + src[ip]
                    ip += 1
                t2 = src[ip] | (src[ip + 1] << 8)
                ip += 2
                m_pos = len(out) - 1 - (t2 >> 2)
            else:
                m_pos = len(out) - ((t & 8) << 11)
                m_len = t & 7
                if m_len == 0:
                    while src[ip] == 0:
                        m_len += 255
                        ip += 1
                    m_len += 7 + src[ip]
                    ip += 1
                t2 = src[ip] | (src[ip + 1] << 8)
                ip += 2
                m_pos -= t2 >> 2
                if m_pos == len(out):
                    return bytes(out)  # 流结束标记
                m_pos -= 0x4000
            for _ in range(m_len + 2):
                if m_pos < 0:
                    raise FactoryError("lzo 回溯位置非法")
                out.append(out[m_pos])
                m_pos += 1
            continue
        if t == 0:
            while src[ip] == 0:
                t += 255
                ip += 1
            t += 15 + src[ip]
            ip += 1
        t += 3
        out += src[ip:ip + t]
        ip += t
        if ip >= n:
            break
        t = src[ip] | (src[ip + 1] << 8)
        ip += 2
        m_pos = len(out) - 1 - (t >> 2)
        m_len = t & 3
        if m_len == 0:
            while src[ip] == 0:
                m_len += 255
                ip += 1
            m_len += 3 + src[ip]
            ip += 1
        for _ in range(m_len + 2):
            if m_pos < 0:
                raise FactoryError("lzo 回溯位置非法")
            out.append(out[m_pos])
            m_pos += 1
    return bytes(out)


def _ubifs_node(leb, offset):
    if offset + 24 > len(leb):
        raise FactoryError("UBIFS 节点头被截断")
    if _le32(leb, offset) != UBIFS_NODE_MAGIC:
        raise FactoryError("UBIFS 节点魔数不对")
    total = _le32(leb, offset + 16)
    if offset + total > len(leb) or total < 24:
        raise FactoryError("UBIFS 节点长度非法")
    n = leb[offset:offset + total]
    _check_crc(n[8:], 0xFFFFFFFF, _le32(n, 4), "UBIFS 节点")
    return n


def _ubifs_name(raw):
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FactoryError("目录项名字不是合法 UTF-8")
    if not s or "/" in s or "\0" in s or s in (".", ".."):
        raise FactoryError("目录项名字非法")
    return s


def _ubifs_paths(dents, sizes, read):
    result = {}
    todo = [(1, "")]
    seen = set()
    while todo:
        parent, prefix = todo.pop()
        if parent in seen:
            raise FactoryError("UBIFS 目录树有环")
        seen.add(parent)
        for (p, n), ino in dents.items():
            if p != parent or ino == 0:
                continue
            path = n if not prefix else prefix + "/" + n
            if ino in sizes:
                size = sizes[ino]
                if size > UBIFS_MAX_FILE:
                    raise FactoryError("%s 太大" % path)
                result[path] = read(ino, size)
            else:
                todo.append((ino, path))
    return result


def _ubifs(lebs):
    if 0 not in lebs:
        raise FactoryError("UBIFS 超级块缺失")
    sb = _ubifs_node(lebs[0], 0)
    if sb[20] != UBIFS_SB_NODE or sb[27] != 0:
        raise FactoryError("不支持的 UBIFS key 格式")

    master = None
    master_seq = -1
    for l in (1, 2):
        leb = lebs.get(l)
        if leb is None:
            continue
        for off in range(0, max(0, len(leb) - 24), 8):
            if leb[off:off + 4] != b"\x31\x18\x10\x06":
                continue
            try:
                n = _ubifs_node(leb, off)
            except FactoryError:
                continue
            if n[20] != UBIFS_MST_NODE:
                continue
            seq = _le64(n, 8)
            if seq > master_seq:
                master_seq = seq
                master = n
    if master is None:
        raise FactoryError("UBIFS 主节点缺失")
    uncommitted = bool(_le32(master, 40) & 1)

    todo = [(_le32(master, 48), _le32(master, 52), _le32(master, 56))]
    seen = set()
    dents = {}
    sizes = {}
    chunks = {}
    while todo:
        l, o, ln = todo.pop()
        if (l, o) in seen:
            raise FactoryError("UBIFS 索引有重复引用")
        seen.add((l, o))
        if l not in lebs:
            raise FactoryError("UBIFS 索引引用了不存在的 LEB")
        n = _ubifs_node(lebs[l], o)
        if len(n) != ln:
            raise FactoryError("UBIFS 索引节点长度不符")
        t = n[20]
        if t == UBIFS_IDX_NODE:
            count = _le16(n, 24)
            if len(n) != 28 + count * 20:
                raise FactoryError("不支持的 UBIFS 索引分支格式")
            for b in range(28, 28 + count * 20, 20):
                todo.append((_le32(n, b), _le32(n, b + 4), _le32(n, b + 8)))
        elif t == UBIFS_INO_NODE:
            if _le32(n, 104) & 0xF000 == 0x8000:
                sizes[_le32(n, 24)] = _le64(n, 48)
        elif t == UBIFS_DENT_NODE:
            dents[(_le32(n, 24), _ubifs_name(n[56:56 + _le16(n, 50)]))] = _le64(n, 40)
        elif t == UBIFS_DATA_NODE:
            if _le32(n, 40) > 4096:
                raise FactoryError("UBIFS 数据块超过 4096 字节")
            chunks[(_le32(n, 24), _le32(n, 28) & 0x1FFFFFFF)] = n
        elif t == UBIFS_XENT_NODE:
            pass
        else:
            raise FactoryError("UBIFS 索引里有未知节点类型 %d" % t)

    def read(ino, size):
        out = bytearray(size)
        for (i, block), n in chunks.items():
            if i != ino:
                continue
            compression = _le16(n, 44)
            decoded = _ubi_decompress(n[48:], _le32(n, 40),
                                     4 if compression == 2 else compression)
            start = block * 4096
            if start >= size:
                raise FactoryError("UBIFS 数据块超出文件大小")
            count = min(len(decoded), size - start)
            out[start:start + count] = decoded[:count]
        return bytes(out)

    return _ubifs_paths(dents, sizes, read), uncommitted


def extract_ubi(data):
    """解析 UBI 镜像，返回 (files, uncommitted)。"""
    peb = None
    for shift in range(16, 21):
        s = 1 << shift
        if len(data) % s == 0 and data[s:s + 4] == b"UBI#":
            peb = s
            break
    if peb is None:
        raise FactoryError("无法判断 UBI 擦除块大小")

    blocks = {}  # (vol_id, lnum) -> (seq, payload)
    for off in range(0, len(data) - peb + 1, peb):
        p = data[off:off + peb]
        if all(b == 0xFF for b in p):
            continue
        if not p.startswith(b"UBI#"):
            raise FactoryError("UBI 擦除计数头非法")
        _check_crc(p[:60], 0xFFFFFFFF, _be32(p, 60), "UBI EC 头")
        vo = _be32(p, 16)
        data_off = _be32(p, 20)
        if vo + 64 > len(p):
            raise FactoryError("UBI VID 偏移越界")
        vid = p[vo:vo + 64]
        if all(b == 0xFF for b in vid):
            continue
        if not vid.startswith(b"UBI!"):
            raise FactoryError("UBI VID 头非法")
        _check_crc(vid[:60], 0xFFFFFFFF, _be32(vid, 60), "UBI VID 头")
        key = (_be32(vid, 8), _be32(vid, 12))
        seq = _be64(vid, 40)
        if data_off > peb:
            raise FactoryError("UBI 数据偏移非法")
        payload = p[data_off:]
        if vid[6] != 0 or vid[5] == 2:
            dlen = _be32(vid, 20)
            try:
                _check_crc(payload[:dlen], 0xFFFFFFFF, _be32(vid, 32), "UBI 数据")
                valid = True
            except FactoryError:
                valid = False
            if not valid:
                if vid[6] != 0:
                    continue
                raise FactoryError("UBI 数据 CRC 不匹配")
        old = blocks.get(key)
        if old is None or seq > old[0]:
            blocks[key] = (seq, payload)

    table = None
    for probe in (0, 1):
        entry = blocks.get((UBI_LAYOUT_VOL_ID, probe))
        if entry:
            table = entry[1]
            break
    if table is None:
        raise FactoryError("UBI 卷表缺失")

    vol_id = None
    for i in range(min(UBI_MAX_VOLUMES, len(table) // UBI_VTBL_RECORD_SIZE)):
        rec = table[i * UBI_VTBL_RECORD_SIZE:(i + 1) * UBI_VTBL_RECORD_SIZE]
        nlen = _be16(rec, 14)
        if nlen > 127:
            raise FactoryError("UBI 卷名长度非法")
        if rec[16:16 + nlen] == b"fhdata":
            _check_crc(rec[:168], 0xFFFFFFFF, _be32(rec, 168), "UBI 卷表记录")
            if rec[13] != 0:
                raise FactoryError("fhdata 卷更新未完成")
            vol_id = i
            break
    if vol_id is None:
        raise FactoryError("UBI 镜像里没有 fhdata 卷")

    lebs = {lnum: payload for (v, lnum), (_, payload) in blocks.items() if v == vol_id}
    return _ubifs(lebs)


# ---------------- JFFS2 解析 ----------------

JFFS2_MAGIC = 0x1985
JFFS2_NODETYPE_DIRENT = 0xE001
JFFS2_NODETYPE_INODE = 0xE002
JFFS2_COMPR_NONE = 0
JFFS2_COMPR_ZERO = 1
JFFS2_COMPR_RTIME = 2
JFFS2_COMPR_ZLIB = 6
JFFS2_COMPR_LZO = 7
JFFS2_MAX_FILE = 16 * 1024 * 1024


def extract_jffs2(data):
    """解析 JFFS2 镜像，返回 (files, damaged)。"""
    dents = {}    # (pino, name) -> (version, ino)
    inodes = {}   # ino -> [node]
    damaged = False
    off = 0
    n = len(data)
    while off + 12 <= n:
        if _le16(data, off) != JFFS2_MAGIC:
            off += 4
            continue
        kind = _le16(data, off + 2)
        totlen = _le32(data, off + 4)
        if totlen < 12 or totlen > (1 << 20) or off + totlen > n:
            off += 4
            continue
        node = data[off:off + totlen]
        off += (totlen + 3) & ~3
        if kind & 0x2000 == 0:
            continue  # 不是兼容位为 1 的节点，跳过
        try:
            _check_crc(node[:8], 0, _le32(node, 8), "JFFS2 头")
        except FactoryError:
            damaged = True
            continue
        if kind == JFFS2_NODETYPE_DIRENT:
            try:
                _check_crc(node[:32], 0, _le32(node, 32), "JFFS2 dirent")
                ns = node[28]
                nm = node[40:40 + ns]
                _check_crc(nm, 0, _le32(node, 36), "JFFS2 dirent 名字")
            except FactoryError:
                damaged = True
                continue
            pino, ver, ino = struct.unpack_from("<III", node, 12)
            key = (pino, bytes(nm))
            if key not in dents or ver > dents[key][0]:
                dents[key] = (ver, ino)
        elif kind == JFFS2_NODETYPE_INODE:
            try:
                _check_crc(node[:60], 0, _le32(node, 64), "JFFS2 inode")
                csize = _le32(node, 48)
                _check_crc(node[68:68 + csize], 0, _le32(node, 60), "JFFS2 inode 数据")
            except FactoryError:
                damaged = True
                continue
            inodes.setdefault(_le32(node, 12), []).append(node)

    by_parent = {}
    for (pino, nm), (ver, ino) in dents.items():
        by_parent.setdefault(pino, []).append((nm, ino))
    dirs = set(by_parent.keys())

    def walk(pino, prefix):
        for nm, ino in by_parent.get(pino, []):
            path = prefix + nm.decode("utf-8", "replace")
            if ino in dirs:
                yield from walk(ino, path + "/")
            elif ino in inodes:
                yield path, ino

    files = {}
    for path, ino in walk(1, ""):
        nodes = sorted(inodes[ino], key=lambda x: _le32(x, 16))
        last = nodes[-1]
        isize = _le32(last, 28)
        if isize > JFFS2_MAX_FILE:
            raise FactoryError("%s 太大" % path)
        out = bytearray(isize)
        for node in nodes:
            o, csize, dsize = struct.unpack_from("<III", node, 44)
            compr = node[56]
            raw = node[68:68 + csize]
            if compr == JFFS2_COMPR_NONE:
                payload = raw
            elif compr == JFFS2_COMPR_ZERO:
                payload = bytes(dsize)
            elif compr == JFFS2_COMPR_ZLIB:
                payload = zlib.decompress(raw)
            elif compr == JFFS2_COMPR_LZO:
                payload = _lzo1x_decompress(raw, dsize)
            elif compr == JFFS2_COMPR_RTIME:
                raise FactoryError("暂不支持 JFFS2 RTIME 压缩")
            else:
                raise FactoryError("不支持的 JFFS2 压缩类型 %d" % compr)
            end = min(len(payload), max(0, isize - o))
            out[o:o + end] = payload[:end]
        files[path] = bytes(out)
    return files, damaged


def extract_files(data):
    """按魔数自动识别 UBI / JFFS2 并解析，返回 (files, warning)。"""
    if data.startswith(b"UBI#"):
        files, uncommitted = extract_ubi(data)
        return files, ("UBIFS 主节点标记未提交，镜像可能不完整" if uncommitted else None)
    if data.startswith(b"\x85\x19"):
        files, damaged = extract_jffs2(data)
        return files, ("JFFS2 有损坏节点，已跳过" if damaged else None)
    raise FactoryError("无法识别的 factory 分区格式（既不是 UBI 也不是 JFFS2）")


# ---------------- factory_conf ----------------

def parse_factory_conf(data):
    """解析 UCI 格式的 factory_conf，返回 {key: value}。"""
    text = data.decode("utf-8", "replace")
    fields = {}
    current = None
    for line in text.splitlines():
        words = line.split()
        if len(words) == 3 and words[0] == "config" and words[1] == "key":
            current = _unquote(words[2])
            continue
        if len(words) >= 3 and words[0] == "option" and words[1] == "value" and current:
            idx = line.find("value")
            if idx >= 0:
                val = _unquote(line[idx + 5:].strip())
                if val is not None:
                    fields[current] = val
    return fields


def _unquote(value):
    if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
        return value[1:-1]
    return None


def _decode_hex(value, expected):
    compact = value.replace(":", "").replace("-", "")
    if len(compact) != expected * 2:
        raise FactoryError("十六进制字段长度不对：%r" % value)
    try:
        return bytes.fromhex(compact)
    except ValueError:
        raise FactoryError("十六进制字段非法：%r" % value)


def _parse_mac(value):
    mac = _decode_hex(value, BASE_MAC_SIZE)
    if all(b == 0 for b in mac) or all(b == 0xFF for b in mac) or mac[0] & 1:
        raise FactoryError("MAC 非法：%r" % value)
    return mac


def _parse_pon_serial(value):
    serial = _decode_hex(value, PON_SN_SIZE)
    if all(b == 0 for b in serial) or all(b == 0xFF for b in serial):
        raise FactoryError("GponSN 非法：%r" % value)
    return serial


def _parse_device_serial(value):
    raw = value.encode("utf-8")
    if not raw or len(raw) > DEVICE_SN_SIZE or not all(0x20 <= b <= 0x7E for b in raw):
        raise FactoryError("SerialNumber 非法：%r" % value)
    fixed = bytearray(DEVICE_SN_SIZE)
    fixed[:len(raw)] = raw
    return bytes(fixed)


# ---------------- 组装 ----------------

def build_factory_image(files):
    """从 {相对路径: bytes} 组装 1 MiB factory 镜像。

    需要的键（缺失时相应区域留 0xff）：
        "factory_conf"                      必需
        "table"                             PON 校准（可选）
        "wlan/AX3000_RT30xxEEPROM_5.bin"    Wi-Fi EEPROM（可选）
    """
    conf = files.get("factory_conf")
    if conf is None:
        raise FactoryError("备份里没有 factory_conf")
    fields = parse_factory_conf(conf)
    if "brmac" not in fields or not fields["brmac"]:
        raise FactoryError("factory_conf 缺少 brmac")
    if "GponSN" not in fields or not fields["GponSN"]:
        raise FactoryError("factory_conf 缺少 GponSN")

    image = bytearray(b"\xff" * FACTORY_SIZE)

    if files.get("table"):
        pon = _normalize_pon_calibration(files["table"])
        image[PON_OFFSET:PON_OFFSET + len(pon)] = pon

    wifi_present = bool(files.get("wlan/AX3000_RT30xxEEPROM_5.bin"))
    if wifi_present:
        eeprom = _extract_wifi_eeprom(files["wlan/AX3000_RT30xxEEPROM_5.bin"])
        image[WIFI_OFFSET:WIFI_OFFSET + WIFI_SIZE] = eeprom

    mac = _parse_mac(fields["brmac"])
    pon_sn = _parse_pon_serial(fields["GponSN"])
    image[BASE_MAC_OFFSET:BASE_MAC_OFFSET + BASE_MAC_SIZE] = mac
    image[PON_SN_OFFSET:PON_SN_OFFSET + PON_SN_SIZE] = pon_sn

    if wifi_present:
        wifi_2g, wifi_5g = _derive_wifi_macs(mac)
        image[WIFI_OFFSET + 4:WIFI_OFFSET + 10] = wifi_2g
        image[WIFI_OFFSET + 10:WIFI_OFFSET + 16] = wifi_5g

    dev_sn = fields.get("SerialNumber")
    if dev_sn:
        image[DEVICE_SN_OFFSET:DEVICE_SN_OFFSET + DEVICE_SN_SIZE] = _parse_device_serial(dev_sn)

    return bytes(image)


def describe(image):
    """把 1 MiB 镜像里的身份字段读回来，供日志展示。"""
    mac = ":".join("%02X" % b for b in image[BASE_MAC_OFFSET:BASE_MAC_OFFSET + BASE_MAC_SIZE])
    sn = image[PON_SN_OFFSET:PON_SN_OFFSET + PON_SN_SIZE]
    if all(0x30 <= b <= 0x7A or 0x41 <= b <= 0x5A for b in sn[:4]):
        pon_sn = sn[:4].decode("ascii", "replace") + sn[4:].hex().upper()
    else:
        pon_sn = sn.hex().upper()
    raw = image[DEVICE_SN_OFFSET:DEVICE_SN_OFFSET + DEVICE_SN_SIZE]
    end = next((i for i, b in enumerate(raw) if b in (0, 0xFF)), len(raw))
    device_sn = raw[:end].decode("utf-8", "replace")
    wifi_present = any(b != 0xFF for b in image[WIFI_OFFSET:WIFI_OFFSET + WIFI_SIZE])
    return {
        "mac": mac,
        "pon_sn": pon_sn,
        "device_sn": device_sn,
        "wifi_present": wifi_present,
        "pon_present": any(b != 0xFF for b in image[PON_OFFSET:PON_OFFSET + PON_SIZE]),
    }


if __name__ == "__main__":
    import sys
    import os

    src = sys.argv[1] if len(sys.argv) > 1 else "backup_20261020_234343/mtd13_factory.bin"
    out = sys.argv[2] if len(sys.argv) > 2 else "factory-1m.bin"
    files, warning = extract_files(open(src, "rb").read())
    print("警告:", warning or "无", "文件数:", len(files))
    img = build_factory_image(files)
    with open(out, "wb") as f:
        f.write(img)
    print("已写出:", out, len(img), "字节")
    for k, v in describe(img).items():
        print("  %-14s %s" % (k, v))
