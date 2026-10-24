#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ttl_rescue.py —— AN7581 光猫 TTL + XMODEM 救砖工具

适用机型：烽火 HG5585F CT/CU、HG5382A，及所有 an7581 平台
（Nokia XG-040G-MD/TF、同洲 ZN504XG-D、优迈 UNG00A、Gemtek XG2010G …）


## 什么时候需要它

Winbond 颗粒（W29N02KVSIAF）的机器用 stock2ubi 免拆刷入后，BL2 会打：

    NOTICE:  Parallel NAND: W29N02KVSIAF, ID ef:da:069510, page 2048, erase 131072, ECC4/512
    NOTICE:  FIP source: UBI volume
    NOTICE:  UBI: attached by fastmap 255MB 2047 blocks
    ERROR:   BL2: Failed to load image id 3 (-22)
    ERROR:   Stored BL31 + U-Boot FIP failed (-22)
    Press x to load BL31 + U-Boot FIP via XMODEM

最后那行就是在等你按 x。按下去之后 BL2 会用 XMODEM 把 FIP 直接收进内存，
**完全绕开 NAND 的 ECC 路径**，所以能起来。

根因见 `NAND-ECC-问题报告.md`：厂商固件按 8bit BCH 写整片闪存，而
v2026.07-3 起的 BL2 按 4bit 读 Winbond 这颗 —— 写读错位，不是镜像坏了。


## 用法

    # 1) 无硬件自检（先跑这个，确认工具本身没问题）
    python3 ttl_rescue.py --selftest

    # 2) 看设备日志，不发任何东西
    python3 ttl_rescue.py --monitor

    # 3) 救砖：先运行脚本，再给设备上电
    sudo python3 ttl_rescue.py --port /dev/ttyUSB0 \
         --file an7581-fiberhome-hg5585f-ct-bl31-u-boot.fip

    # 4) 救回来之后（见文末「传完之后」一节）


## 传完之后

FIP 传完、U-Boot 跑起来，这一步就到位了 —— **而且不用重启**：
设备现在已经在 U-Boot 里等着你，直接往下走就行。

  * 就在当前串口按 Ctrl+C 打断，敲 `httpd dhcp` 进 WebUI；
    也可以按住 reset 进恢复模式，两条路等效（上游 pbs05 在 issue #21 里说的）。
  * 浏览器打开 http://192.168.0.1/，按页面流程重建 UBI 并重刷。
    华邦闪存机型：这时把 U-Boot 换成官方的再收尾。

**验收标准**：拔掉 TTL、直接断电重启三次，次次能自起才算真脱砖。做不到
的话，说明 bootstrap 那一层还停在 8bit —— 上游 erroljay-stack 在 issue #21
就是刷完能进系统、重启又挂。原因见报告第六节。
"""

from __future__ import annotations

import os
import sys
import time

# ---------------------------------------------------------------------------
# XMODEM 控制字符（全部来自标准，不要改）
# ---------------------------------------------------------------------------

SOH = 0x01          # 128 字节块头
STX = 0x02          # 1024 字节块头
EOT = 0x04          # 结束传输
ACK = 0x06          # 肯定应答
DLE = 0x10
NAK = 0x15          # 否定应答（要求重传）
CAN = 0x18          # 取消（连发两个才算）
CRC_CHAR = 0x43     # 'C'，接收方用它宣告「我支持 CRC 模式」
SUB = 0x1A          # 填充字节（尾部不足一块时补这个）

# 设备进入 XMODEM 等待时打的提示。BL2 打完这行就在轮询串口找 'x'。
PRESS_X_PAT = b"press x to load"
XMODEM_HINT_PAT = b"via xmodem"

DEFAULT_BAUD = 115200


def crc16_xmodem(data: bytes) -> int:
    """XMODEM 用的 CRC-16/CCITT（多项式 0x1021，初值 0）。"""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def hexdump(data: bytes, limit: int = 256, width: int = 16) -> str:
    """给乱码现场留一份十六进制快照 —— 判断是不是波特率错了全靠它。"""
    out = []
    d = data[:limit]
    for off in range(0, len(d), width):
        chunk = d[off:off + width]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)
        out.append(f"    {off:04x}  {hexs:<{width * 3}}  |{text}|")
    if len(data) > limit:
        out.append(f"    …（共收到 {len(data)} 字节，上面只显示前 {limit} 字节）")
    return "\n".join(out) if out else "    （一个字节都没收到）"


class StreamHealth:
    """
    判断收到的字节流到底是不是一段正常的启动日志。

    AN7581 的 BL2 日志几乎全是可见 ASCII（字母数字、空格、标点）加 \r \n。
    **接错线**（TX/RX 反了、GND 没接）、**波特率不对**、**选错串口** 这三种
    情况收到的都会是大段高位字节和控制字符 —— 就是这里要抓的。
    """

    def __init__(self, window: int = 4096):
        self.window = window
        self.buf = bytearray()
        self.total = 0

    def feed(self, raw: bytes) -> None:
        self.buf += raw
        self.total += len(raw)
        if len(self.buf) > self.window:
            del self.buf[:-self.window]

    @staticmethod
    def _printable(b: int) -> bool:
        return b in (0x09, 0x0A, 0x0D) or 0x20 <= b <= 0x7E

    def ratio(self) -> float:
        b = bytes(self.buf)
        if not b:
            return 1.0
        return sum(1 for x in b if self._printable(x)) / len(b)

    def verdict(self):
        """→ ('unknown'|'ok'|'warn'|'bad', 说明)"""
        if self.total < 64:
            return "unknown", f"只收到 {self.total} 字节，还判断不了"
        r = self.ratio()
        if r >= 0.90:
            return "ok", f"可打印 ASCII 占比 {r:.0%}（正常）"
        if r >= 0.55:
            return "warn", f"可打印 ASCII 占比只有 {r:.0%}，夹杂乱码"
        return "bad", f"可打印 ASCII 占比只有 {r:.0%}，基本全是乱码"


def _port_hint(port: str) -> str:
    """尽量把 USB-TTL 芯片型号也报出来，接错线时这条最有用。"""
    import glob
    for link in glob.glob("/dev/serial/by-id/*"):
        try:
            if os.path.realpath(link) == os.path.realpath(port):
                return f"{os.path.basename(link)}  →  {port}"
        except OSError:
            continue
    return f"{port}（读不到 by-id，可能不是 USB-TTL）"


def report_garbled(echo, port: str, baud: int, health: "StreamHealth",
                   reason: str = "") -> None:
    """
    乱码现场报告：把「当前终端信息」原样摊开，再给排查清单。

    光说一句「有乱码」没用 —— 得让用户看见串口参数、字节统计和原始十六进制，
    才能分清是接错线还是波特率错了。
    """
    import datetime
    r = health.ratio() if health.total else 0.0
    echo("")
    echo("=" * 68)
    echo("  ⚠️  检测到串口乱码")
    echo("=" * 68)
    if reason:
        echo(f"  触发原因：{reason}")
    echo("")
    echo("  【当前终端信息】")
    echo(f"    时间        : {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    echo(f"    串口        : {_port_hint(port)}")
    echo(f"    串口参数    : {baud} 8N1，无流控（XMODEM 是停等协议，不能开流控）")
    echo(f"    接收字节数  : {health.total}")
    echo(f"    可打印占比  : {r:.1%}")
    echo(f"    运行环境    : ttl_rescue.py / {sys.platform} / Python "
         f"{sys.version.split()[0]}")
    echo("")
    echo("  【原始字节快照（十六进制）】")
    echo(hexdump(bytes(health.buf), limit=256))
    echo("")
    echo("  【请按顺序排查】")
    echo("    1) TTL 接线 —— 这是最常见的原因：")
    echo("         板子的 TX  →  USB-TTL 的 RX")
    echo("         板子的 RX  →  USB-TTL 的 TX")
    echo("         GND        →  GND（必须接！只接两根线一样会乱码）")
    echo("       上游 issue #21 里 lyq1996 特别提醒过：板子丝印上的 TX/RX 是")
    echo("       **板子自己**的角度，要交叉接。")
    echo("    2) 波特率是不是 115200（AN7581 固定 115200，不是 57600/1500000）")
    echo("    3) 串口有没有选错（插了多个 CH340 时很容易串）")
    echo("    4) 上面第 1 条三根线都对的话，把 TX/RX 对调再试一次")
    echo("       —— 有些山寨适配器的丝印是反的")
    echo("")
    echo("  接好线后重跑即可，工具会自动重新握手。")
    echo("=" * 68)
    echo("")


class _Tee:
    """同时输出到终端和日志文件，乱码现场不至于只留在屏幕上。"""

    def __init__(self, path: str | None = None):
        self.fh = None
        if path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
                self.fh = open(path, "a", encoding="utf-8", errors="replace")
            except OSError:
                self.fh = None
        self.path = path if self.fh else None

    def __call__(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        if self.fh:
            self.fh.write(text)
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 传输层：优先 pyserial，没有就退回纯 termios（零依赖也能跑）
# ---------------------------------------------------------------------------

class Transport:
    """只要能 read/write 就行，方便自检时换成内存回环。"""

    def read(self, n: int, timeout: float) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = DEFAULT_BAUD):
        import serial  # pyserial

        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0,
            write_timeout=10,
            rtscts=False,      # XMODEM 是停等协议，不能开硬件流控
            dsrdtr=False,
            xonxoff=False,
        )
        self.port = port

    def read(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        out = bytearray()
        while len(out) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self.ser.timeout = left
            chunk = self.ser.read(n - len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class TermiosTransport(Transport):
    """pyserial 装不上时的兜底实现（Linux/WSL 直接可用）。"""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD):
        import termios
        import tty

        self.termios = termios
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        speed = getattr(termios, f"B{baud}")
        attrs[4] = attrs[5] = speed               # ispeed / ospeed
        attrs[2] = attrs[2] | termios.CLOCAL | termios.CREAD
        attrs[0] = attrs[1] = attrs[3] = 0        # 关掉所有输入/输出处理
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.port = port

    def read(self, n: int, timeout: float) -> bytes:
        import select

        deadline = time.monotonic() + timeout
        out = bytearray()
        while len(out) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                break
            try:
                chunk = os.read(self.fd, n - len(out))
            except BlockingIOError:
                continue
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def write(self, data: bytes) -> None:
        while data:
            k = os.write(self.fd, data)
            data = data[k:]

    def close(self) -> None:
        try:
            os.close(self.fd)
        except Exception:
            pass


def open_transport(port: str, baud: int) -> Transport:
    try:
        return SerialTransport(port, baud)
    except ImportError:
        return TermiosTransport(port, baud)


def list_ports() -> list:
    try:
        from serial.tools import list_ports as lp

        return [
            (p.device, p.description or "")
            for p in lp.comports()
        ]
    except Exception:
        pass
    out = []
    for name in sorted(os.listdir("/dev")):
        if name.startswith(("ttyUSB", "ttyACM", "ttyS")) and name[5:].isdigit():
            out.append((f"/dev/{name}", ""))
    return out


def pick_port(explicit: str | None, ask=print) -> str:
    if explicit:
        return explicit
    ports = list_ports()
    real = [p for p in ports if "ttyS" not in p[0]]
    cands = real or ports
    if not cands:
        raise SystemExit(
            "[x] 没找到串口。插好 USB-TTL 后重试，或用 --port 指定，"
            "例如 --port /dev/ttyUSB0"
        )
    if len(cands) == 1:
        ask(f"[*] 自动选中串口: {cands[0][0]}  {cands[0][1]}")
        return cands[0][0]
    ask("[?] 检测到多个串口，请用 --port 指定：")
    for dev, desc in cands:
        ask(f"      {dev}   {desc}")
    raise SystemExit("[x] 请用 --port 指定串口")


# ---------------------------------------------------------------------------
# XMODEM 发送端
# ---------------------------------------------------------------------------

class XmodemSender:
    def __init__(self, tp: Transport, block_size: int = 1024, timeout: float = 15.0,
                 max_retry: int = 10, log=print, quiet: bool = False,
                 echo=None, port: str = "", baud: int = DEFAULT_BAUD,
                 garble_check: bool = True):
        self.tp = tp
        self.block_size = block_size
        self.timeout = timeout
        self.max_retry = max_retry
        self.log = log
        self.quiet = quiet
        self.echo = echo or (lambda t: (sys.stdout.write(t), sys.stdout.flush()))
        self.port = port
        self.baud = baud
        self.garble_check = garble_check
        self.health = StreamHealth()
        self._garble_level = None
        self.device_log = bytearray()   # 设备打出来的普通文本，最后回显给用户
        self._last_pct = -1
        self._t0 = 0.0

    # -- 内部 --------------------------------------------------------------

    def _note(self, msg: str) -> None:
        if not self.quiet:
            self.log(msg)

    def _drain_device_text(self, raw: bytes) -> None:
        """把非控制字节收进 device_log，好让用户看到 BL2 到底说了啥。"""
        keep = bytes(b for b in raw if b not in (ACK, NAK, CAN, CRC_CHAR))
        if keep:
            self.device_log += keep

    def _wait_signal(self, timeout: float) -> str:
        """等一个控制字节。返回 'ack'/'nak'/'can'/'c'/'timeout'。"""
        deadline = time.monotonic() + timeout
        can_count = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return "timeout"
            raw = self.tp.read(1, left)
            if not raw:
                return "timeout"
            b = raw[0]
            if b == ACK:
                return "ack"
            if b == NAK:
                return "nak"
            if b == CAN:
                can_count += 1
                if can_count >= 2:
                    return "can"
                continue
            if b == CRC_CHAR:
                return "c"
            self._drain_device_text(bytes([b]))

    # -- 握手 --------------------------------------------------------------

    def wait_for_prompt(self, timeout: float, force_x: bool = True,
                        probe_only: bool = False) -> None:
        """
        盯设备日志，等 "Press x to load ... via XMODEM"。

        为什么等超时后还要主动发 x：设备可能**早就**停在那行提示上了
        （比如你上次上电后没接串口），那行字不会再打第二遍。所以超时后
        周期性补发 'x' 是必要的 —— 'x' 在正常启动流程里没有副作用。
        """
        self._note(f"[*] 正在监听串口（最多 {timeout:.0f}s）… 请现在给设备上电")
        buf = bytearray()
        shown = 0
        deadline = time.monotonic() + timeout
        found = False
        while time.monotonic() < deadline:
            raw = self.tp.read(256, 1.0)
            if not raw:
                continue
            buf += raw
            # 边收边回显，用户能实时看到 DRAM 初始化那些日志
            self.echo(raw.decode("utf-8", "replace"))
            shown += len(raw)

            # 乱码检测：接错线 / 波特率不对 / 选错串口，在这里就会被抓住
            self.health.feed(raw)
            if self.garble_check:
                lvl, why = self.health.verdict()
                if lvl in ("warn", "bad") and lvl != self._garble_level:
                    self._garble_level = lvl
                    report_garbled(self.echo, self.port, self.baud,
                                   self.health, why)

            low = bytes(buf[-512:]).lower()
            if PRESS_X_PAT in low or XMODEM_HINT_PAT in low:
                found = True
                break
        else:
            if not found:
                self._note("")
                self._note("[!] 这段时间没看到 'Press x ...' 提示。")

        if probe_only:
            return
        if not found and not force_x:
            raise SystemExit(
                "[x] 没等到提示，且已禁用 --force-x。请确认设备停在 Press x 那一步。"
            )
        if not found:
            self._note("[*] 按预设策略，主动补发 'x'（设备可能已停在提示上）。")

        self._note("")
        self._note("[*] 发送 'x'，进入 XMODEM 接收模式 …")
        self.send_x()

    def send_x(self, wait_c: float = 30.0) -> None:
        """发 'x'，然后等接收方的 'C'（CRC 模式宣告）。"""
        deadline = time.monotonic() + wait_c
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            self.tp.write(b"x")
            sig = self._wait_signal(2.0)
            if sig == "c":
                self._note("[✓] 收到 'C' —— 设备已进入 XMODEM-CRC 接收模式")
                return
            if sig == "can":
                raise SystemExit("[x] 设备发送 CAN，取消了传输。")
        raise SystemExit(
            "[x] 发了 'x' 但一直没等到 'C'。\n"
            "    排查：① 确认设备真的停在 'Press x ...' 那行\n"
            "          ② 确认 TX/RX 没接反（板子上的 TX 接 USB-TTL 的 RX）\n"
            "          ③ 确认波特率是 115200"
        )

    # -- 发送 --------------------------------------------------------------

    def _build_block(self, seq: int, payload: bytes) -> bytes:
        hdr = STX if self.block_size == 1024 else SOH
        crc = crc16_xmodem(payload)
        return bytes([hdr, seq & 0xFF, (~seq) & 0xFF]) + payload + bytes(
            [(crc >> 8) & 0xFF, crc & 0xFF]
        )

    def send(self, data: bytes) -> bool:
        total = len(data)
        # 尾部不足一块就补 SUB（0x1A），补齐后接收方丢弃多余字节
        pad = (-total) % self.block_size
        payload_all = data + bytes([SUB]) * pad
        nblocks = len(payload_all) // self.block_size

        self._t0 = time.monotonic()
        self._last_pct = -1
        self._note(
            f"[*] 开始传输：{human(total)} → {nblocks} 块 × {self.block_size}B"
            f"（补 {pad}B）"
        )

        seq = 1
        off = 0
        idx = 0
        consecutive_nak = 0
        while idx < nblocks:
            payload = payload_all[off:off + self.block_size]
            sent = False
            for attempt in range(1, self.max_retry + 1):
                self.tp.write(self._build_block(seq, payload))
                sig = self._wait_signal(self.timeout)
                if sig == "ack":
                    sent = True
                    consecutive_nak = 0
                    break
                if sig == "can":
                    self._note("[x] 设备发送 CAN，传输被取消。")
                    return False
                if sig == "c":
                    # 接收方还在等第一块（或者我们发漏了），直接重发即可
                    self._note(f"[!] 第 {idx + 1} 块收到 'C'，重发")
                    continue
                if sig == "nak":
                    consecutive_nak += 1
                    self._note(
                        f"[!] 第 {idx + 1} 块被 NAK（{attempt}/{self.max_retry}）"
                        f"{'，连续 ' + str(consecutive_nak) + ' 次' if consecutive_nak > 1 else ''}"
                    )
                    if consecutive_nak >= 5 and self.block_size == 1024:
                        return self._fallback_128(payload_all, idx)
                    continue
                self._note(
                    f"[!] 第 {idx + 1} 块等回应超时"
                    f"（{attempt}/{self.max_retry}，{self.timeout:.0f}s）"
                )
            if not sent:
                self._note(f"[x] 第 {idx + 1} 块重试 {self.max_retry} 次仍失败，放弃。")
                return False

            idx += 1
            off += self.block_size
            seq = (seq + 1) & 0xFF
            self._progress(idx, nblocks, off if idx < nblocks else total, total)

        # EOT 收尾
        self._note("")
        self._note("[*] 发送 EOT 结束传输 …")
        for attempt in range(1, self.max_retry + 1):
            self.tp.write(bytes([EOT]))
            sig = self._wait_signal(self.timeout)
            if sig == "ack":
                el = time.monotonic() - self._t0
                self._note(
                    f"[✓] 传输完成：{human(total)}，用时 {el:.1f}s"
                    f"（{human(total / el) if el > 0 else '?'}/s）"
                )
                return True
            if sig == "can":
                self._note("[x] 设备发送 CAN，传输被取消。")
                return False
            self._note(f"[!] EOT 未确认（{attempt}/{self.max_retry}），重发")
        return False

    def _fallback_128(self, payload_all: bytes, idx: int) -> bool:
        """
        1024 字节块连续被 NAK，说明接收方只吃 128 字节块（老 XMODEM 实现）。
        切到 128 后从当前块重来。整块边界会变，所以重新按 128 切一遍。
        """
        self._note("[!] 连续 NAK，切换到 128 字节块（SOH）重试 …")
        self.block_size = 128
        return self.send_retry_from(payload_all, idx, 1024)

    def send_retry_from(self, payload_all: bytes, idx: int, old_block_size: int) -> bool:
        """
        换了块大小之后的续传：从「已成功传完的字节数」对应的**新**块边界继续。

        调用点只有一个：1024 → 128。已传字节数 = idx * old_block_size，
        按新块大小重新取整（旧块边界本来就是新块边界的整数倍，所以直接整除）。
        """
        done_bytes = idx * old_block_size
        nblocks = len(payload_all) // self.block_size
        start = done_bytes // self.block_size
        seq = (start + 1) & 0xFF
        off = start * self.block_size
        for i in range(start, nblocks):
            payload = payload_all[off:off + self.block_size]
            ok = False
            for attempt in range(1, self.max_retry + 1):
                self.tp.write(self._build_block(seq, payload))
                sig = self._wait_signal(self.timeout)
                if sig == "ack":
                    ok = True
                    break
                if sig == "can":
                    return False
                if sig == "timeout":
                    self._note(f"[!] 128B 模式第 {i + 1} 块超时 {attempt}/{self.max_retry}")
            if not ok:
                self._note("[x] 128 字节块模式同样失败，放弃。")
                return False
            off += self.block_size
            seq = (seq + 1) & 0xFF
            self._progress(i + 1, nblocks, min(off, len(payload_all)), len(payload_all))

        self._note("")
        self._note("[*] 发送 EOT 结束传输 …")
        for _ in range(self.max_retry):
            self.tp.write(bytes([EOT]))
            if self._wait_signal(self.timeout) == "ack":
                el = time.monotonic() - self._t0
                self._note(f"[✓] 传输完成（128B 模式），用时 {el:.1f}s")
                return True
        return False

    def _progress(self, i: int, n: int, done: int, total: int) -> None:
        pct = int(done * 100 / max(total, 1))
        if pct == self._last_pct and i != n:
            return
        self._last_pct = pct
        el = max(time.monotonic() - self._t0, 0.001)
        speed = done / el
        bar_n = 30
        filled = int(bar_n * pct / 100)
        bar = "█" * filled + "░" * (bar_n - filled)
        eta = (total - done) / speed if speed > 0 else 0
        sys.stdout.write(
            f"\r    [{bar}] {pct:3d}%  {human(done)}/{human(total)}  "
            f"{human(speed)}/s  ETA {eta:4.0f}s "
        )
        sys.stdout.flush()
        if i == n:
            sys.stdout.write("\n")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# 自检：内存回环，用一个真的 XMODEM-CRC 接收方来验收发送端
# ---------------------------------------------------------------------------

class _LoopbackTransport(Transport):
    """
    假装自己是设备：收到 'x' 就回 'C'，然后按 XMODEM-CRC 收块、校验、
    回 ACK/NAK，收到 EOT 回 ACK。可选地故意破坏某一块，验证重传逻辑。
    """

    def __init__(self, corrupt_at: int | None = None):
        self.rx = bytearray()      # 要发给「主机」的字节
        self.recv = bytearray()    # 攒下来的文件内容
        self.buf = bytearray()     # 从主机收到的原始字节
        self.state = "idle"
        self.bs = 0
        self.expected = 1
        self.done = False
        self.corrupt_at = corrupt_at
        self.naks = 0

    def read(self, n: int, timeout: float) -> bytes:
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data: bytes) -> None:
        self.buf += data
        self._pump()

    def _pump(self) -> None:
        while True:
            if self.state == "idle":
                if not self.buf:
                    return
                b = self.buf[0]
                if b == ord("x"):
                    self.buf.pop(0)
                    self.rx += bytes([CRC_CHAR])
                elif b == SOH:
                    self.bs, self.state = 128, "hdr"
                    self.buf.pop(0)
                elif b == STX:
                    self.bs, self.state = 1024, "hdr"
                    self.buf.pop(0)
                elif b == EOT:
                    self.buf.pop(0)
                    self.rx += bytes([ACK])
                    self.state, self.done = "done", True
                else:
                    self.buf.pop(0)
            elif self.state == "hdr":
                need = 2 + self.bs + 2
                if len(self.buf) < need:
                    return
                seq = self.buf[0]
                nseq = self.buf[1]
                payload = bytes(self.buf[2:2 + self.bs])
                crc = (self.buf[2 + self.bs] << 8) | self.buf[3 + self.bs]
                del self.buf[:need]
                self.state = "idle"          # 关键：一块收完就回去找下一个块头
                if nseq != (~seq & 0xFF):
                    self.rx += bytes([NAK])
                    self.naks += 1
                    continue
                if seq == (self.expected & 0xFF):
                    if self.corrupt_at == seq:
                        payload = bytes([payload[0] ^ 0xFF]) + payload[1:]
                        self.corrupt_at = None
                    if crc16_xmodem(payload) == crc:
                        self.recv += payload
                        self.expected = (self.expected + 1) & 0xFF
                        self.rx += bytes([ACK])
                    else:
                        self.rx += bytes([NAK])
                        self.naks += 1
                elif seq == ((self.expected - 1) & 0xFF):
                    self.rx += bytes([ACK])       # 重传的旧块，再确认一次
                else:
                    self.rx += bytes([NAK])
                    self.naks += 1
            else:
                return


def selftest(verbose: bool = True) -> int:
    """不需要硬件，验证 XMODEM 发送端的正确性。"""
    import hashlib

    log = print if verbose else (lambda *_a, **_k: None)
    failures = []

    def case(name: str, blob: bytes, block_size: int, corrupt_at=None,
             expect_ok: bool = True):
        tp = _LoopbackTransport(corrupt_at=corrupt_at)
        s = XmodemSender(tp, block_size=block_size, timeout=1.0,
                         max_retry=8, log=log, quiet=not verbose)
        s.tp.write(b"x")                     # 模拟握手
        got_c = s._wait_signal(1.0) == "c"
        ok = s.send(blob)
        exp = len(blob)
        result = bytes(tp.recv[:exp])
        match = result == blob
        good = got_c and ok == expect_ok and match and tp.done and len(tp.recv) >= exp
        status = "PASS" if good else "FAIL"
        if not good:
            failures.append(name)
        if verbose:
            print(
                f"    [{status}] {name}: 握手={'✓' if got_c else '✗'} "
                f"传输={'✓' if ok else '✗'} "
                f"内容={'✓' if match else '✗'} "
                f"EOT={'✓' if tp.done else '✗'} "
                f"重传={tp.naks} sha={hashlib.sha256(result).hexdigest()[:12]}"
            )
        return good

    print("[*] XMODEM 发送端自检（内存回环，无需硬件）")

    # 1) 与真实 FIP 完全等长的数据、1024 字节块
    fip = _find_file(None, quiet=True)
    if fip and os.path.isfile(fip):
        blob = open(fip, "rb").read()
        print(f"[*] 用真实 FIP 做样本: {fip} ({human(len(blob))})")
    else:
        blob = os.urandom(340992)
        print("[*] 没找到本地 FIP，用 340992 字节随机数据代替（与 FIP 等长）")

    case("1024B 块 / 真实长度（333 块整）", blob, 1024)
    case("1024B 块 / 非整块长度（补 SUB）", blob[:-5], 1024)
    case("128B 块 / 非整块长度", blob[:4097], 128)
    case("1024B 块 / 第 5 块被破坏一次 → 必须重传成功", blob, 1024, corrupt_at=5)
    case("跨 255 块序号回绕（seq 255→0）", os.urandom(1024 * 300), 1024)
    case("1 字节文件", b"\x5a", 1024)
    case("整块边界（恰好 2 块）", os.urandom(2048), 1024)

    # 乱码检测本身也要验：正常日志必须判 ok，乱码必须判 warn/bad
    normal = (b"NOTICE:  Parallel NAND: W29N02KVSIAF, ID ef:da:069510, "
              b"page 2048, erase 131072, ECC4/512\r\n"
              b"NOTICE:  FIP source: UBI volume\r\n") * 4
    h = StreamHealth()
    h.feed(normal)
    lvl, why = h.verdict()
    ok1 = lvl == "ok"
    if not ok1:
        failures.append("乱码检测-正常日志")
    if verbose:
        print(f"    [{'PASS' if ok1 else 'FAIL'}] 正常 BL2 日志判为 ok: {lvl} ({why})")

    for name, junk in [
        ("乱码-高位字节", bytes(range(0x80, 0x100)) * 3),
        ("乱码-波特率错", bytes((i * 37 + 0x9C) & 0xFF for i in range(300))),
        ("乱码-全零", bytes(200)),
    ]:
        h = StreamHealth()
        h.feed(junk)
        lvl, why = h.verdict()
        good = lvl in ("warn", "bad")
        if not good:
            failures.append(name)
        if verbose:
            print(f"    [{'PASS' if good else 'FAIL'}] {name} 判为 {lvl}: {why}")

    print()
    if failures:
        print(f"[✗] 自检失败 {len(failures)} 项: {', '.join(failures)}")
        return 1
    print("[✓] 自检全部通过：分块、CRC、ACK/NAK 重传、序号回绕、EOT、乱码检测")
    return 0


def _find_file(explicit: str | None, quiet: bool = False) -> str | None:
    """没给 --file 时，在当前目录 / _src/rel 里找 FIP。"""
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit(f"[x] 文件不存在: {explicit}")
        return explicit
    import glob

    pats = [
        "*.fip", "_src/rel/*.fip", "firmware/*.fip", "downloaded/*.fip",
        "**/*bl31-u-boot.fip",
    ]
    for pat in pats:
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            if not quiet:
                print(f"[*] 自动选中文件: {hits[0]}")
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="ttl_rescue.py",
        description="AN7581 光猫 TTL + XMODEM 救砖工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 用法")[1] if "## 用法" in __doc__ else "",
    )
    p.add_argument("--port", help="串口设备，如 /dev/ttyUSB0（默认自动检测）")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="波特率，默认 115200")
    p.add_argument("--file", help="要发送的 FIP（默认自动找 *bl31-u-boot.fip）")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="等待每块 ACK 的超时秒数，默认 15")
    p.add_argument("--retry", type=int, default=10, help="每块最大重试次数，默认 10")
    p.add_argument("--wait", type=float, default=90.0,
                   help="开机后等待 'Press x' 提示的秒数，默认 90")
    p.add_argument("--force-x", action="store_true", default=True,
                   help="没等到提示也发 'x'（默认开）")
    p.add_argument("--no-force-x", dest="force_x", action="store_false",
                   help="只在看到提示后才发 'x'")
    p.add_argument("--monitor", action="store_true", help="只看日志，什么都不发")
    p.add_argument("--monitor-seconds", type=float, default=0.0,
                   help="配合 --monitor：只听这么多秒就自动退出（0=一直听）")
    p.add_argument("--selftest", action="store_true", help="无硬件自检")
    p.add_argument("--after", type=float, default=15.0,
                   help="传完后继续显示设备输出多少秒，默认 15")
    p.add_argument("--no-garble-check", dest="garble_check", action="store_false",
                   default=True, help="关掉乱码检测（默认开）")
    p.add_argument("--log", nargs="?", const="", default=None,
                   help="把串口输出同时写进日志文件（不给路径则写到 "
                        "_out/ttl_rescue_<时间>.log）")
    p.add_argument("--list", action="store_true", help="列出串口后退出")
    args = p.parse_args(argv)

    if args.list:
        for dev, desc in list_ports():
            print(f"  {dev}   {desc}")
        return 0

    if args.selftest:
        return selftest()

    if args.baud != DEFAULT_BAUD:
        print(f"[!] 波特率是 {args.baud}，AN7581 稳的是 115200；"
              f"换了可能收不到 'C'。")

    port = pick_port(args.port)

    # 日志文件：乱码现场要留证据，默认不写，--log 才写
    logpath = None
    if args.log is not None:
        if args.log:
            logpath = args.log
        else:
            import datetime
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            logpath = os.path.join("_out", f"ttl_rescue_{stamp}.log")
    echo = _Tee(logpath)
    if logpath:
        print(f"[*] 日志 : {logpath}")

    print("=" * 68)
    print("  AN7581 TTL + XMODEM 救砖")
    print("=" * 68)

    if args.monitor:
        if args.monitor_seconds > 0:
            print(f"[*] 只监听模式：{port} @ {args.baud}，"
                  f"听 {args.monitor_seconds:.0f} 秒后自动退出")
        else:
            print(f"[*] 只监听模式：{port} @ {args.baud}，按 Ctrl+C 退出")
        print("[*] 现在给设备上电\n")
        health = StreamHealth()
        warned = None
        # GUI 里没法给后台线程发 Ctrl+C，所以必须有个「听够就自己停」的出口
        deadline = (time.monotonic() + args.monitor_seconds
                    if args.monitor_seconds > 0 else None)
        tp = None
        try:
            tp = open_transport(port, args.baud)
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    print("\n[*] 监听时间到，退出")
                    break
                raw = tp.read(256, 1.0)
                if not raw:
                    continue
                echo(raw.decode("utf-8", "replace"))
                health.feed(raw)
                if args.garble_check:
                    lvl, why = health.verdict()
                    if lvl in ("warn", "bad") and lvl != warned:
                        warned = lvl
                        report_garbled(echo, port, args.baud, health, why)
        except KeyboardInterrupt:
            print("\n[*] 退出")
        finally:
            if tp is not None:
                try:
                    tp.close()
                except Exception:
                    pass
        return 0

    fip = _find_file(args.file)
    if not fip:
        raise SystemExit(
            "[x] 没找到 FIP 文件。请用 --file 指定，例如：\n"
            "    --file an7581-fiberhome-hg5585f-ct-bl31-u-boot.fip"
        )
    data = open(fip, "rb").read()
    if len(data) < 4 or int.from_bytes(data[:4], "little") != 0xAA640001:
        print(f"[!] 警告：{fip} 开头不是 FIP magic (0xAA640001)，"
              f"实际 {data[:4].hex()}。可能不是 FIP 文件。")
    print(f"[*] 串口 : {port} @ {args.baud} 8N1")
    print(f"[*] 文件 : {fip}")
    print(f"[*] 大小 : {human(len(data))} ({len(data)} 字节, "
          f"{'整' if len(data) % 1024 == 0 else '不整'} 1024 块)")

    tp = open_transport(port, args.baud)
    sender = XmodemSender(tp, block_size=1024, timeout=args.timeout,
                          max_retry=args.retry, echo=echo, port=port,
                          baud=args.baud, garble_check=args.garble_check)

    ok = False
    try:
        sender.wait_for_prompt(args.wait, force_x=args.force_x)
        ok = sender.send(data)
    except KeyboardInterrupt:
        print("\n[x] 用户中断")
        tp.close()
        return 130
    except SystemExit as e:
        print(e)
        tp.close()
        return 1

    # 传完后继续看设备输出，好判断 U-Boot 起没起来
    if ok and args.after > 0:
        print(f"\n[*] 继续监听 {args.after:.0f}s，观察 U-Boot 是否起来 …")
        deadline = time.monotonic() + args.after
        buf = bytearray()
        while time.monotonic() < deadline:
            raw = tp.read(256, 1.0)
            if raw:
                buf += raw
                sys.stdout.write(raw.decode("utf-8", "replace"))
                sys.stdout.flush()
        low = bytes(buf).lower()
        if b"u-boot" in low or b"=>" in low or b"hit any key" in low:
            print("\n[✓] 看到 U-Boot 的输出 —— 这一步成功了")
        else:
            print("\n[!] 没明显看到 U-Boot 提示符，请人工看一眼上面的输出")

    tp.close()

    print()
    print("=" * 68)
    if ok:
        print("  下一步：设备已经在 U-Boot 里了，不用重启")
        print("=" * 68)
        print("  * 就在这个串口按 Ctrl+C 打断，敲 `httpd dhcp` 进 WebUI；")
        print("    （或者按住 reset 进恢复模式，等效）")
        print("  * 浏览器打开 http://192.168.0.1/，按页面流程重建 UBI 并重刷；")
        print("    华邦闪存机型：这时把 U-Boot 换成官方的再收尾。")
        print()
        print("  ⚠️ 验收标准：拔掉 TTL，直接断电重启三次，次次能自起才算真脱砖。")
        print("     做不到说明 bootstrap 那一层还是 8bit（见")
        print("     NAND-ECC-问题报告.md 第六节）。")
        return 0
    print("  [✗] 传输失败")
    print("=" * 68)
    if sender.device_log:
        print("[*] 设备最后说的话：")
        print(bytes(sender.device_log).decode("utf-8", "replace")[-2000:])
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))