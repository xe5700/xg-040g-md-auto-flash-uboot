#!/usr/bin/env python3
"""XG-040G-MD 光猫免拆机维护工具（开启 Telnet / 开启 FTP / 备份全部分区 / 刷写 U-Boot）

用法:
    python3 flash.py                      # 使用配置区 MODE 的默认模式
    python3 flash.py --mode telnet        # 仅自动开启 Telnet，最安全（不碰 FTP）
    python3 flash.py --mode ftp           # 仅自动开启 FTP（su 提权用，不备份）
    python3 flash.py --mode backup        # 开启 Telnet+FTP + 备份 mtd0-mtd16（不写入）
    python3 flash.py --mode flash         # 开启 Telnet+FTP + 备份 + 刷写 U-Boot（危险）
    python3 flash.py --mode flash -y      # 跳过刷写前的 YES 二次确认
    python3 flash.py --no-enable-telnet   # 假定 Telnet 已手动开启，不走网页步骤
    python3 flash.py --no-enable-ftp      # 假定 FTP 已手动开启，不走网页步骤
    python3 flash.py --force-backup       # 即使已有同一台设备的完整备份也重新备份
    python3 flash.py --reboot             # 只重启光猫（清掉网页会话 / telnet 锁定）

模式在配置区 MODE 中设置，默认 "backup"（只读，不刷机）。

telnet 与 ftp 是两个独立开关，各自单独检测：
  - telnet 已开、FTP 未开（用户自己开过 telnet）→ --mode ftp 单独开 FTP 即可；
  - 两者都关 → --mode backup 会按需依次开启。

关于自动开启 FTP：提权账号 user_ftp 只在 FTP 服务启用后存在，没开 FTP 时
`su user_ftp` 会报 unknown user，拿不到 root，也就无法读 /dev/mtd*。所以
backup/flash 模式会先自动开启 FTP（用完不主动关闭，建议刷完机后手动关掉）。
"""
import telnetlib3.telnetlib as telnetlib
import socket
import hashlib
import json
import os
import sys
import time
import base64
import threading
import subprocess
import re
import http.cookiejar
import urllib.error
from urllib import request as urllib_request, parse as urllib_parse

# ================== 配置区 ==================
ROUTER_IP = "192.168.1.1"
# 不同型号/固件的 telnet 账号不一样（有的 useradmin，有的 user）。
# 留空 AUTO_DETECT_USERNAME=True 时，工具会从登录页 JS 里读出该机型实际的
# telnet 账号（页面会 $("input[name='name']").val("xxx") 预填），
# 读不到再按 USERNAME_CANDIDATES 逐个尝试。
AUTO_DETECT_USERNAME = True
USERNAME = "user"                # 手动指定时用这个（XG-040G-MD 移动版实测为 user）
USERNAME_CANDIDATES = ["user", "useradmin"]  # 自动探测失败时的候选顺序（不要加 admin：错的候选会白耗一次认证机会）
PASSWORD = "j3ba73@8"            # telnet 登录密码（光猫底部铭牌上的那一个）
# su 提权用的账号。原脚本写的是 useradmin_ftp，但 XG-040G-MD 当前固件的
# /etc/passwd 里根本没有这个账号（只有 root / user-common / osgi_admin / user-telnet），
# 提权路径：telnet 用 `user`/`j3ba73@8` 登录后，`su user_ftp`（密码同样是
# telnet 密码）即可直接拿到 root —— 提示符从 `$` 变成 `#`，`whoami` 返回 root。
# 注意 `user_ftp` 账号在 /etc/passwd 里并不存在（那里只有 root / user-common /
# osgi_admin / user-telnet），它是**启用 FTP 服务后由 `IGD FTP Server` 动态注入**的。
# 所以：没开 FTP 时 su 一定失败（su: unknown user）；开了 FTP 才能用这条路径。
# 另一个副作用是启用 FTP 后要记得用网页把它关掉，避免长期暴露 21 端口。
SU_USER = "user_ftp"
ROOT_PASSWORD = PASSWORD         # su user_ftp 的密码就是 telnet 登录密码
# 注意：非 root 身份下 busybox 会直接拒绝执行 dd
#   （dd: you have no permission to run this applet!），
# 而 /dev/mtd* 是 crw-rw---- root root，所以备份和刷写都必须先拿到 root。
# 排除过的 su 目标：user-telnet（接受密码但只是回到自身）/ osgi_admin /
# user-common（拒绝所有已知密码）/ root（拒绝所有已知密码）；
# 另有 /proc/cmdline 泄露的 telecomadmin/nE7jA%5m，作 su 与网页口令均无效。

# ---------- 运行模式（安全优先：默认不刷机）----------
# "telnet"  仅自动开启 Telnet，最安全，什么都不写
# "backup"  开启 Telnet + 备份全部分区（mtd0-mtd16），不写入
# "flash"   开启 Telnet + 备份 + 刷写 U-Boot 到 mtd0（危险）
MODE = "backup"
# 命令行可覆盖：python3 flash.py --mode telnet|backup|flash
# -------------------------------------------------

# --- 自动开启 Telnet（免拆机）配置 ---
AUTO_ENABLE_TELNET = True        # 刷机前自动通过网页开启 Telnet；False 则跳过（假定已手动开启）
SUPER_USER = "CMCCAdmin"         # 超级账号（移动版恢复出厂默认）
SUPER_PASSWORD = "aDm8H%MdA"     # 超级密码（移动版恢复出厂默认）
TELNET_PORT = 23                 # Telnet 服务端口
WEB_SESSION_RETRY = 2400         # 超级账号网页会话被占用时最长等待秒数（单会话限制，默认最多等 40 分钟）
WEB_SESSION_POLL = 20            # 等待会话释放时的轮询间隔（秒）

# --- 自动开启 FTP（su 提权的前提）配置 ---
# 为什么要自动开 FTP：提权账号 user_ftp 只在 FTP 服务启用后才存在，
# 没开 FTP 时 `su user_ftp` 会报 su: unknown user user_ftp，拿不到 root。
# 本工具在备份/刷写前会自动开启，用完不会主动关闭（因为 su 已建立），
# 但建议刷完机后用网页把它关掉，避免长期暴露 21 端口。
AUTO_ENABLE_FTP = True           # 自动通过网页开启 FTP；False 则跳过（假定已手动开启）
FTP_PORT = 21                    # FTP 服务端口

# --- 备份跳过策略 ---
# 备份目录里会写入 device.json（机型/序列号 + 每个分区的大小与 SHA256）。
# 下次运行时若检测到**相同序列号**且该备份**完整、校验全部通过**，就直接跳过备份。
SKIP_BACKUP_IF_COMPLETE = True   # False 则每次强制重新备份
DEVICE_MANIFEST = "device.json"  # 备份清单文件名

UBOOT_FILE = "tcboot.bin"
EXPECTED_SHA512 = "142ad1ebcc825e58223e5c28de0aee853d96a05447bda1af9fc78dd3f5eab0e10aec2626bd4b18ce7d8137b7f6943726734bcb38ce06d75ea86b45ccf6969610"  # ←←← 必须填写正确的 SHA512！

# --- 刷写后的回读校验 ---
# 实测：dd 写入 /dev/mtdblock0 后立刻 `dd if=/dev/mtd0` 读回来的常常还是旧数据，
# 过一段时间才变成新数据。原因见 flash_uboot() 里的注释（mtdblock 写缓存 +
# 后台擦写线程）。所以校验必须**轮询等待**，不能读一次就判失败。
# 512KB 的 U-Boot 实测几秒到几十秒内生效，这里给足余量。
FLASH_VERIFY_TIMEOUT = 120       # 回读校验最长轮询秒数
FLASH_VERIFY_INTERVAL = 5        # 每次回读之间的间隔秒数
BACKUP_DIR = f"backup_{time.strftime('%Y%m%d_%H%M%S')}"
TIMEOUT = 15
BACKUP_FIRMWARE = True
STATIC_LOCAL_IP = "192.168.1.1"
# ===========================================

MTD_PARTITIONS = [
    "bootloader", "romfile", "kernel", "rootfs", "kernel_slave", "rootfs_slave",
    "bosa", "ri", "flag", "flagback", "config", "data", "oopsfs", "log",
    "nsb_master", "nsb_slave", "all_flash"
]

# 各分区字节数（取自本机 `cat /proc/mtd` 实测，2026-10-04）
# 用途：给备份超时一个按分区大小的合理上界，并校验备份文件完整性。
MTD_SIZES = {
    0: 0x00080000,    # bootloader      512 KiB
    1: 0x00040000,    # romfile         256 KiB
    2: 0x003af61f,    # kernel          ~3.7 MiB
    3: 0x01cb0000,    # rootfs          28.7 MiB
    4: 0x00480000,    # kernel_slave    4.5 MiB
    5: 0x02400000,    # rootfs_slave    36 MiB
    6: 0x00040000,    # bosa            256 KiB
    7: 0x00040000,    # ri              256 KiB
    8: 0x00040000,    # flag            256 KiB
    9: 0x00040000,    # flagback        256 KiB
    10: 0x00a00000,   # config          10 MiB
    11: 0x080e0000,   # data            128.9 MiB
    12: 0x00400000,   # oopsfs          4 MiB
    13: 0x00a00000,   # log             10 MiB
    14: 0x02880000,   # nsb_master      40.5 MiB
    15: 0x02880000,   # nsb_slave       40.5 MiB
    16: 0x0eba0000,   # all_flash       235.6 MiB
}

def sha512sum(filepath):
    h = hashlib.sha512()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_local_ip(target_ip):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((target_ip, 80))
        return s.getsockname()[0]
def read_until_prompt(tn: telnetlib.Telnet, prompt_list, timeout=10):
    """等待直到出现提示符之一。

    注意：telnetlib3 的 expect() 会把模式 re.compile() 成正则，所以
    '$' 必须转义（它是零宽锚点，不转义会立即匹配空串）。
    """
    try:
        index, match, text = tn.expect([_re_escape(p) for p in prompt_list], timeout=timeout)
    except EOFError:
        return ""
    except Exception:
        return ""
    return text.decode('utf-8', errors='ignore')

def wait_for(tn, prompt, timeout=TIMEOUT):
    """等到提示符出现。调用方应在**发命令之前**调用 drain() 排空残留缓冲，
    否则上一步遗留的提示符会让这里立刻返回、误判命令已完成。"""
    try:
        tn.read_until(prompt.encode(), timeout=timeout)
        return True
    except Exception:
        print(f"[!] 等待 '{prompt}' 超时")
        return False

def drain(tn):
    """排空 cookedq 里的残留输出（上一步的提示符、回显等）。"""
    try:
        tn.read_very_eager()
    except Exception:  # noqa: BLE001
        pass

def is_port_open(host, port, timeout=3):
    """检查 TCP 端口是否可连接"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def _web_get(op, url, timeout=10):
    with op.open(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")

def _web_post(op, url, body=b"data", timeout=10, headers=None):
    """POST 一个 CGI 接口（设备端 cgi 都是这种简单调用）"""
    hdrs = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"}
    if headers:
        hdrs.update(headers)
    req = urllib_request.Request(url, data=body, headers=hdrs)
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="ignore").strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="ignore").strip()

def _web_login():
    """尝试超级账号网页登录，返回 (status, body, opener)；status==299 为成功"""
    base = f"http://{ROUTER_IP}"
    cj = http.cookiejar.CookieJar()
    op = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(cj))
    _web_get(op, base + "/")  # 先 GET 首页拿到初始 cookie
    data = urllib_parse.urlencode(
        {"newMethodLogin": "1", "name": SUPER_USER, "pswd": SUPER_PASSWORD},
        quote_via=urllib_parse.quote).encode()
    req = urllib_request.Request(
        base + "/login.cgi", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest"})
    with op.open(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return resp.status, body, op

def _telnet_state(op):
    """读取网页上的 Telnet 开关状态（页面 JS: telnet_config.TelnetEnable）"""
    _, page = _web_get(op, f"http://{ROUTER_IP}/system.cgi?telnet")
    m = re.search(r"(?<!Factory)TelnetEnable\s*:\s*(\d+)", page)
    return m.group(1) if m else None

def _csrf_token(op, page_url):
    """从页面里抓本次会话的 csrf_token。

    设备端每个 POST 都必须带 csrf_token（页面 ajaxSend 自动追加）。
    实测不带 token 的 POST 会返回 302 且**作废当前会话**，所以抓不到就绝不能硬发。
    """
    try:
        st, page = _web_get(op, page_url)
    except Exception:
        return None
    if st != 200:
        return None
    m = re.search(r"csrf_token=([A-Za-z0-9]+)", page)
    return m.group(1) if m else None


def reboot_router(op=None):
    """通过管理页接口重启光猫。

    ⚠️ 端点必须是 POST /system.cgi?reboot（body 为 data），对应管理页
    「设备重启」按钮：/webs/html_cm/management/reboot.html 里
        url: self.location.pathname + "?reboot", data: 'data'

    曾经误用 /devicemanagement.cgi?reboot —— 那个 cgi 其实是**恢复配置**页，
    只处理 ?restore 与 ?restore_factory（后者=恢复出厂，会清掉超级密码和
    所有配置！）。写错一个参数名就可能变成抹机，所以这里显式注释警示，
    并且只允许调用 ?reboot 这一个 query。

    重启会清掉服务端所有网页会话，也是解开 telnet 认证锁定（300s）的办法。
    重启耗时通常 1~3 分钟，期间设备不可达。
    """
    base = f"http://{ROUTER_IP}"
    own = op is None
    if own:
        try:
            status, body, op = _web_login()
        except Exception as e:
            print(f"[!] 重启前登录失败: {e!r}")
            return False
        if status != 299:
            print("[!] 重启前无法登录网页（会话可能被占用）。")
            print("    可先用 telnet 以 root 执行 `rm -f /tmp/cgi_session` 释放槽位。")
            return False

    token = _csrf_token(op, base + "/system.cgi?reboot")
    if not token:
        print("[!] 拿不到 csrf_token，拒绝发送重启请求（裸 POST 会作废会话）。")
        if own:
            _web_logout(op, quiet=True)
        return False

    try:
        st, res = _web_post(
            op, base + "/system.cgi?reboot",
            body=f"data&csrf_token={token}".encode(),
            headers={"Referer": base + "/system.cgi?reboot"})
        print(f"[*] 已调用重启接口 POST /system.cgi?reboot（HTTP {st}: {res!r}）")
    except Exception as e:
        print(f"[!] 重启接口调用失败: {e!r}")
        return False
    if own:
        try:
            _web_get(op, base + "/login.cgi?out")
        except Exception:
            pass

    print("[*] 等待光猫重启（通常 1~3 分钟）...")
    # 先等端口断开，再等它回来；否则会在设备还没真正重启时就误判「已恢复」
    time.sleep(5)
    gone = False
    for _ in range(30):
        if not is_port_open(ROUTER_IP, 80, timeout=2):
            gone = True
            break
        time.sleep(2)
    if gone:
        print("[*] 设备已断开，等待重新启动 ...")
    else:
        print("[!] 未观察到端口断开，重启可能未真正执行（请核对 uptime）。")

    for i in range(90):
        time.sleep(5)
        if is_port_open(ROUTER_IP, 80, timeout=2):
            time.sleep(8)  # 让 web 服务彻底就绪
            print(f"[✓] 光猫已恢复响应（约 {(i + 1) * 5 + 13} 秒）")
            return True
    print("[!] 等待超时，请确认光猫已上电联网。")
    return False

def _web_logout(op, quiet=False):
    """主动退出网页登录，释放那个唯一的超级账号会话。"""
    try:
        _web_get(op, f"http://{ROUTER_IP}/login.cgi?out")
        if not quiet:
            print("[*] 已退出网页登录（释放会话）")
    except Exception:
        pass

def _web_login_with_retry(retry_seconds=None, purpose="操作"):
    """获取一个可用的超级账号网页会话，返回 (opener, ok)。

    超级账号同一时间只允许一个网页会话：若它已在别的浏览器登录，会返回
    HTTP 200 + "用户xxx已经登录，请稍后重试"。此时先等待会话超时释放
    （retry_seconds，默认 WEB_SESSION_RETRY），仍不通则询问是否重启光猫
    （重启会清掉所有网页会话，是最可靠的解法）。
    """
    if retry_seconds is None:
        retry_seconds = WEB_SESSION_RETRY
    deadline = time.time() + retry_seconds
    while True:
        try:
            status, body, op = _web_login()
        except Exception as e:
            print(f"[!] 网页登录请求失败: {e!r}")
            break

        if status == 299:
            print(f"[✓] 超级账号登录成功（准备{purpose}）")
            return op, True

        # 登录未通过（HTTP 200 + 登录页）
        if "已经登录" in body:
            remain = max(0, int(deadline - time.time()))
            if remain > 0:
                print(f"[!] 超级账号 {SUPER_USER} 当前已在网页在线（单会话限制），"
                      f"等待其超时释放 ...（剩余约 {remain // 60} 分钟；Ctrl+C 可随时中止）")
                time.sleep(min(WEB_SESSION_POLL, remain))
                continue
            # 等了很久仍被占用 → 询问是否重启光猫强行清掉所有会话
            print(f"[!] 等待 {retry_seconds // 60} 分钟后网页会话仍被占用。")
            print("[!] 提示：重启光猫会立刻清掉所有网页会话，也是解开 telnet "
                  "认证锁定的办法（本工具用管理页的 /system.cgi?reboot 接口）。")
            try:
                ans = input("现在重启光猫吗？输入 YES 执行：").strip()
            except EOFError:
                ans = ""
            if ans == "YES":
                if reboot_router():
                    print(f"[*] 重启完成，重新尝试{purpose} ...")
                    deadline = time.time() + retry_seconds
                    continue
                print("[!] 重启未成功或设备未恢复。")
            break
        m = re.search(r"<font[^>]*>(.*?)</font>", body, re.S)
        print(f"[!] 网页登录失败：{m.group(1).strip() if m else '未知错误（HTTP ' + str(status) + '）'}"
              f"，请检查 SUPER_USER / SUPER_PASSWORD 是否与光猫一致。")
        break
    return None, False

def enable_telnet_via_web(retry_seconds=None):
    """通过超级账号网页接口自动开启 Telnet（免拆机）。

    原理（XG-040G-MD 移动版"中国移动 HGW"管理页，已实测）：
      1) login.cgi 接受明文 POST（无需浏览器那套 RSA/AES 加密）：
         newMethodLogin=1&name=超级账号&pswd=超级密码，HTTP 299 表示登录成功；
      2) system.cgi?telnet 只返回配置页，真正开启是页面 JS 发出的
         POST /system.cgi?telnet+on  （注意 + 必须是字面量，不能编码成 %2B；
          设备端不校验页面里的 csrf_token），成功时返回 "Factory Telnet on !"。
      3) 开启后主动 login.cgi?out 退出，释放这个唯一会话。
    """
    if is_port_open(ROUTER_IP, TELNET_PORT):
        print(f"[✓] 检测到 Telnet 已开启（{ROUTER_IP}:{TELNET_PORT}），跳过网页开启步骤。")
        return

    base = f"http://{ROUTER_IP}"
    print(f"[*] 通过超级账号登录网页（{SUPER_USER}）自动开启 Telnet ...")
    op, ok = _web_login_with_retry(retry_seconds, purpose="开启 Telnet")
    if ok:
        try:
            print(f"[*] 当前 Telnet 开关状态: {_telnet_state(op)}")
        except Exception:
            pass
        try:
            st, res = _web_post(op, base + "/system.cgi?telnet+on")
            print(f"[*] POST system.cgi?telnet+on -> HTTP {st} {res!r}")
        except Exception as e:
            print(f"[!] system.cgi?telnet+on 请求失败: {e!r}")
        # 等待 telnetd 起来
        telnet_ok = False
        for _ in range(15):
            time.sleep(1)
            if is_port_open(ROUTER_IP, TELNET_PORT):
                telnet_ok = True
                break
        try:
            print(f"[*] 开启后页面状态字段: {_telnet_state(op)}")
        except Exception:
            pass
        _web_logout(op)
        if not telnet_ok:
            print(f"[!] 操作后 Telnet（{ROUTER_IP}:{TELNET_PORT}）仍未开启，"
                  f"请确认该型号/固件支持本方式，或手动到网页开启。")
            sys.exit(1)
        print("[✓] Telnet 已开启")
        return

    print("[✗] 无法自动开启 Telnet。请手动操作后重试：")
    print("    1) 浏览器登录 http://192.168.1.1（超级账号 " + SUPER_USER + " / 超级密码）")
    print("    2) 管理页 → 设备管理 → 设备重启（清掉占用中的网页会话）")
    print("    3) Telnet 设置页点「开启」按钮，或直接运行本工具（检测到 23 端口已开会自动跳过网页步骤）")
    sys.exit(1)

def enable_ftp_via_web(retry_seconds=None):
    """通过超级账号网页接口自动开启 FTP 服务（su 提权的前提）。

    为什么必须开 FTP：提权账号 user_ftp 只在 FTP 服务启用后由 IGD FTP Server
    动态注入 /etc/passwd；没开 FTP 时 `su user_ftp` 会报 su: unknown user user_ftp，
    拿不到 root，也就无法读 /dev/mtd*（dd 会被 busybox 直接拒绝）。

    原理（/storage.cgi 就是管理页的"FTP 服务器"页面，已实测）：
      1) GET /storage.cgi，从页面里抓 csrf_token（每次会话不同）；
      2) POST /storage.cgi?ftp_config，body 为 ftp_en=true&csrf_token=<token>，
         需带 Referer: http://<ip>/storage.cgi；
      3) 回读页面 FtpEnable 字段确认；端口 21 约 1 秒内变为 OPEN。

    注意：**不带 csrf_token 的 POST 会返回 302 并作废当前会话**，所以必须抓取。
    """
    if is_port_open(ROUTER_IP, FTP_PORT):
        print(f"[✓] 检测到 FTP 已开启（{ROUTER_IP}:{FTP_PORT}），跳过网页开启步骤。")
        return

    base = f"http://{ROUTER_IP}"
    print(f"[*] 通过超级账号登录网页（{SUPER_USER}）自动开启 FTP ...")
    op, ok = _web_login_with_retry(retry_seconds, purpose="开启 FTP")
    if not ok:
        print("[✗] 无法自动开启 FTP。请手动操作后重试：")
        print("    浏览器登录 http://192.168.1.1 → 存储/FTP 页面 → 勾选「FTP 服务器」并保存")
        sys.exit(1)

    try:
        _, page = _web_get(op, base + "/storage.cgi")
        m = re.search(r"csrf_token=([A-Za-z0-9]+)", page)
        if not m:
            print("[!] 没能从 /storage.cgi 页面里抓到 csrf_token，无法安全提交。")
            _web_logout(op)
            sys.exit(1)
        token = m.group(1)
        cur = re.search(r"FtpEnable\s*:\s*(\d+)", page)
        print(f"[*] 当前 FTP 开关状态: {cur.group(1) if cur else '未知'}")

        body = f"ftp_en=true&csrf_token={token}".encode()
        req = urllib_request.Request(
            base + "/storage.cgi?ftp_config", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": base + "/storage.cgi"})
        with op.open(req, timeout=15) as r:
            st = r.status
            r.read()
        print(f"[*] POST storage.cgi?ftp_config (ftp_en=true) -> HTTP {st}")

        ftp_ok = False
        for _ in range(20):
            time.sleep(1)
            if is_port_open(ROUTER_IP, FTP_PORT):
                ftp_ok = True
                break
        try:
            _, page2 = _web_get(op, base + "/storage.cgi")
            m2 = re.search(r"FtpEnable\s*:\s*(\d+)", page2)
            print(f"[*] 开启后页面状态字段: {m2.group(1) if m2 else '未知'}")
        except Exception:
            pass
    finally:
        _web_logout(op)

    if not ftp_ok:
        print(f"[!] 操作后 FTP（{ROUTER_IP}:{FTP_PORT}）仍未开启。")
        print("[!] 没有 FTP 就无法用 su user_ftp 提权，备份/刷写都无法进行。")
        sys.exit(1)
    print("[✓] FTP 已开启（su user_ftp 提权路径已就绪）")

def parse_args(argv):
    """命令行参数：--mode telnet|ftp|backup|flash、--no-enable-telnet、
    --no-enable-ftp、--force-backup、--reboot、--yes"""
    global MODE
    args = {"mode": MODE, "enable_telnet": AUTO_ENABLE_TELNET, "yes": False,
            "reboot": False, "enable_ftp": AUTO_ENABLE_FTP,
            "force_backup": not SKIP_BACKUP_IF_COMPLETE}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mode" and i + 1 < len(argv):
            args["mode"] = argv[i + 1]; i += 2
        elif a.startswith("--mode="):
            args["mode"] = a.split("=", 1)[1]; i += 1
        elif a == "--no-enable-telnet":
            args["enable_telnet"] = False; i += 1
        elif a == "--no-enable-ftp":
            args["enable_ftp"] = False; i += 1
        elif a == "--force-backup":
            args["force_backup"] = True; i += 1
        elif a == "--reboot":
            args["reboot"] = True; i += 1
        elif a in ("-y", "--yes"):
            args["yes"] = True; i += 1
        elif a in ("-h", "--help"):
            print(__doc__ or "")
            print("用法: python3 flash.py [--mode telnet|ftp|backup|flash] "
                  "[--no-enable-telnet] [--no-enable-ftp] [--force-backup] [--reboot] [-y]")
            sys.exit(0)
        else:
            print(f"[!] 未知参数: {a}（用 --help 查看用法）")
            sys.exit(1)
    if args["mode"] not in ("telnet", "ftp", "backup", "flash"):
        print(f"[!] 非法模式: {args['mode']}（可选 telnet / ftp / backup / flash）")
        sys.exit(1)
    return args

def _re_escape(p):
    """把提示符字面量转成安全的正则字节串。

    重要：telnetlib3 的 expect() 会把每个模式 re.compile() 成**正则表达式**。
    提示符 '$' 在正则里是「字符串结尾」零宽锚点，会立刻匹配空串，
    导致 expect 马上返回空文本、永远等不到真正的命令输出
    （实测表现为 Match span=(1,1) match=b''，登录/su 全部误判超时）。
    所以所有字面量模式都必须转义，'$' -> '\\$'。
    """
    import re as _re
    if isinstance(p, bytes):
        return _re.escape(p)
    return _re.escape(p.encode())

def expect_any(tn, patterns, timeout=15):
    """等待多个提示符之一，返回 (命中的提示符, 收到的文本)。

    失败时（出现 Error! / Login: / 锁定提示）能立刻返回，而不是干等超时。
    连接被对端关闭时返回 ("closed", …) 而不是抛 EOFError。
    """
    try:
        index, match, text = tn.expect([_re_escape(p) for p in patterns], timeout=timeout)
    except EOFError:
        return "closed", ""
    except Exception as e:  # telnet 协议层异常
        return None, repr(e)
    return (patterns[index] if index >= 0 else None), text.decode("utf-8", errors="ignore")

def wait_prompt(tn, timeout=15):
    """等待 shell 提示符并返回层级字符：'#' / '$' / None。

    实现说明：这台光猫的提示符形态不稳定（可能是 '#'、'# '、'$'、'$ '），
    而且 telnetlib3 对单字节模式会返回空 span 的 Match（span=(1,1) match=b''），
    直接靠 expect 判定提示符既不可靠也不必要 —— 权限判定交给 confirm_identity()
    用 `id` 实测。这里只负责把设备输出**排空**到提示符出现，让后续命令不被回显干扰。

    判据：读到含 '#' 或 '$' 的文本即认为已到提示符。
    """
    deadline = time.time() + timeout
    acc = b""
    while time.time() < deadline:
        try:
            idx, match, text = tn.expect([_re_escape("#"), _re_escape("$")], timeout=max(1, int(deadline - time.time())))
        except EOFError:
            return None
        except Exception:  # noqa: BLE001
            return None
        if idx < 0:
            return None
        acc += text or b""
        if b"#" in acc or b"$" in acc:
            token = b"#" if b"#" in acc else b"$"
            return "#" if token == b"#" else "$"
    return None

def confirm_identity(tn, timeout=10, tries=3):
    """发 id 并解析 uid，判断当前身份。返回 'root' / 'user' / None。

    为什么需要重试：这台设备的 shell 在登录/su 成功后的**第一条命令会被吞掉**
    （实测字节流：发密码后收到 b'\\r\\n$'，紧接着发的 `id` 回显为空 b''，
    再发 `su` 才有响应）。所以这里最多发 tries 次，只要某一次读到 uid= 就返回。

    为什么不能用 expect 的 text 判断 root：expect 在**匹配到模式时立即返回**，
    text 只到 b"uid=" 为止（形如 b'id\\r\\nuid='），不含后面的 "uid=0(" 这段。
    早期实现据此判断，恒为 "user"，于是 su 明明成功却被报成失败。
    正确做法是等到提示符后，把这段时间的全部输出拼起来再匹配。
    """
    for attempt in range(tries):
        try:
            tn.read_very_eager()  # 排空残留回显
        except Exception:  # noqa: BLE001
            pass
        tn.write(b"id\n")
        acc = b""
        try:
            # 等到提示符（# 或 $）出现，把整段输出收全
            idx, match, text = tn.expect([_re_escape("#"), _re_escape("$"), _re_escape("Login:")], timeout=timeout)
            acc += text or b""
            try:
                acc += tn.read_very_eager()
            except Exception:  # noqa: BLE001
                pass
        except EOFError:
            return None
        except Exception:  # noqa: BLE001
            continue
        s = acc.decode("utf-8", errors="ignore")
        if "uid=0(" in s:
            return "root"
        if "uid=" in s:
            return "user"
        if "Login:" in s:
            return None
        # 这一条被吞了，再试一次
    return None

def telnet_connect(retries=6, backoff=30):
    """连接 telnetd 并等到出现 Login: 提示。

    这台光猫对 telnet 有「静默限流」：短时间内连续连接时，TCP 握手会成功，
    但 telnetd 立刻关闭连接且不发任何 banner（telnetlib 表现为
    EOFError('telnet connection closed')，裸 socket recv 返回空）。
    这不是 300 秒认证锁定（那个会明确提示 forbidden），而是连接频率限制，
    每次失败重试都会重新计时，必须安静等待数分钟才会恢复。

    所以这里每轮重试之间退避 backoff 秒，耐心等 banner 出现。
    """
    last = ""
    for i in range(retries):
        try:
            tn = telnetlib.Telnet(ROUTER_IP, timeout=10)
        except Exception as e:
            last = f"连接失败 {e!r}"
            print(f"[!] {last}，{backoff} 秒后重试 {i + 1}/{retries} ...")
            time.sleep(backoff)
            continue
        hit, text = expect_any(tn, ["Login:"], timeout=10)
        if hit == "Login:":
            return tn
        last = f"未收到 Login: 提示（收到 {text.strip()[:120]!r}）"
        try:
            tn.close()
        except Exception:
            pass
        print(f"[!] {last}，{backoff} 秒后重试 {i + 1}/{retries} ...")
        time.sleep(backoff)
    print(f"[!] 无法与 telnetd 交互：{last}")
    print("[!] 该光猫对 telnet 有静默限流，连续快速重试只会延长封锁时间，"
          "请安静等待几分钟，或用 python3 flash.py --reboot 重启后再试。")
    sys.exit(1)

def detect_telnet_username():
    """从网页登录页读出该机型实际的 telnet 账号。

    登录页 JS 会预填正确的账号：
        $("input[name='name']").val("user");
    不同型号/固件不一样（有的 useradmin，有的 user）。
    """
    try:
        with urllib_request.urlopen(f"http://{ROUTER_IP}/", timeout=8) as r:
            page = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[!] 读取登录页失败，无法自动探测账号: {e!r}")
        return None
    m = re.search(r"""\$\(\s*["']input\[name=['"]name['"]\]\s*["']\s*\)\s*\.val\(\s*["']([^"']+)["']""", page)
    if m:
        return m.group(1)
    m = re.search(r"""val\(\s*["']([A-Za-z0-9_\-]+)["']\s*\)""", page)
    return m.group(1) if m else None

def telnet_login(reenter_only=False):
    """Telnet 登录并 su 到 root，返回 Telnet 连接对象

    注意：
      - TELNET 密码通常在光猫底部铭牌上，和网页超级密码不是一回事；
      - 账号各型号不同，本函数先从网页登录页自动探测，再按候选列表尝试；
      - 连续 3 次认证失败光猫会锁定 telnet 约 300 秒，所以每个账号只试一次，
        失败立即退出，不要盲目重试（重启可立刻解锁：python3 flash.py --reboot）。
      - reenter_only=True 时跳过候选账号探测，直接用当前候选列表第一个登录，
        用于 su 失败导致连接断开后的重新登录。
    """
    # 1) 决定候选账号
    candidates = []
    if AUTO_DETECT_USERNAME and not reenter_only:
        det = detect_telnet_username()
        if det:
            print(f"[*] 从网页登录页自动探测到 telnet 账号: {det}")
            candidates.append(det)
    for u in ([USERNAME] + list(USERNAME_CANDIDATES)):
        if u and u not in candidates:
            candidates.append(u)
    if not candidates:
        print("[!] 没有可用的 telnet 账号候选，请配置 USERNAME。")
        sys.exit(1)
    if reenter_only:
        candidates = candidates[:1]

    # 2) 逐个尝试登录（每个账号最多一次，避免触发锁定）
    tn = None
    who = None
    for idx, user in enumerate(candidates):
        if tn is None:
            print(f"[*] 连接光猫 {ROUTER_IP}...")
            tn = telnet_connect()
        print(f"[*] 尝试 telnet 账号: {user}"
              + ("（最后一个候选，再失败会触发 300 秒锁定）" if idx == len(candidates) - 1 else ""))
        tn.write(user.encode() + b"\n")
        hit, text = expect_any(tn, ["Password:", "Error!", "closed"], timeout=10)
        if not hit or hit in ("Error!", "closed"):
            print(f"[!] 账号 {user} 未被接受（{(text or '').strip()[:100]}）")
            try:
                tn.close()
            except Exception:
                pass
            tn = None
            time.sleep(3)
            continue
        tn.write(PASSWORD.encode() + b"\n")
        # 不用提示符判定登录成功 —— 这台设备的提示符形态不稳定（可能是 '$' 也
        # 可能是 '$ '），telnetlib3 对单字节模式还会返回空 span 的 Match。
        # 直接发 id 看回显里的 uid，是唯一可靠的做法。
        who = confirm_identity(tn, timeout=15)
        if who is None:
            print(f"[!] 账号 {user} 登录失败：等待身份回显超时")
            try:
                tn.close()
            except Exception:
                pass
            tn = None
            time.sleep(3)
            continue
        if "forbidden" in (text or ""):
            print("[!] 已触发连续认证失败锁定，telnet 约 300 秒内会被拒绝，请勿立即重试。")
            print("[!] 重启光猫可立刻解除锁定：python3 flash.py --reboot")
            sys.exit(1)
        print(f"[✓] 登录成功（账号 {user}，身份 {who}）")
        break

    if tn is None:
        print("[!] 所有候选账号都无法登录 telnet。")
        print(f"[!] telnet 密码通常是光猫底部铭牌上的那一个（当前配置 PASSWORD='{PASSWORD}'），"
              f"与网页超级密码不是同一个。")
        print("[!] 如需指定账号，请修改配置区 USERNAME / USERNAME_CANDIDATES。")
        sys.exit(1)

    # 3) su 提权
    if who == "root":
        print("[✓] 登录后已是 root，跳过 su")
        return tn

    # su 目标固定是 user_ftp，密码固定是 telnet 登录密码。
    # 这是设备上的既定事实（user_ftp 只在启用 FTP 后由 IGD FTP Server 注入，
    # 它的密码就是 telnet 密码），所以不做任何候选尝试：错就是错，直接报错。
    # 试错只会白白消耗认证机会（连续 3 次失败锁 300 秒），没有任何收益。
    print(f"[*] 切换到 {SU_USER} ...")
    tn.write(f"su {SU_USER}\n".encode())
    hit, text = expect_any(tn, ["Password:", "Error!", "not found", "unknown user", "closed"], timeout=10)
    if hit != "Password:":
        print(f"[!] 切换 {SU_USER} 异常：{(text or '').strip()[:150]}")
        if "not found" in (text or "") or "unknown" in (text or "").lower():
            print(f"[!] 很可能 su: unknown user {SU_USER} —— 该账号只在**启用 FTP 服务**后存在。")
            print("[!] 请先在光猫网页上启用 FTP，再重试。")
        sys.exit(1)

    # su user_ftp 的密码就是 telnet 登录密码，不做候选尝试。
    # 试错没有任何收益：su 失败会消耗认证机会（连续 3 次 telnet 失败即锁 300 秒），
    # 而这个密码我们已经从设备上确认过 —— 错就是错，直接报错。
    tn.write(ROOT_PASSWORD.encode() + b"\n")
    # 同样不靠提示符判定，直接实测身份
    who = confirm_identity(tn, timeout=15)
    if who == "root":
        print("[✓] 已获取 root 权限")
        return tn
    print(f"[!] su {SU_USER} 失败：用 telnet 密码登录后身份仍是 {who or '未知'}。")
    print(f"[!] {SU_USER} 的密码应与 telnet 密码（当前配置 '{ROOT_PASSWORD}'）相同，"
          f"若设备上改过 telnet 密码，请同步修改配置区 PASSWORD。")
    print(f"[!] 另外 su {SU_USER} 只在**启用 FTP 服务**后存在；"
          f"没开 FTP 会报 su: unknown user {SU_USER}。")
    tn.close()
    sys.exit(1)

# ---------- 临时 HTTP 文件服务器（替代 nc）----------
# 为什么不用 nc：nc 需要主机侧**精确卡时间**先起监听、设备才敢连，任一环节慢
# 一拍就丢数据（实测表现为备份文件 0 字节）；而且每个分区都要重新起一次监听，
# 17 个分区就是 17 次竞态。HTTP 是请求-响应模型，没有竞态，curl 还自带重试与
# 断点续传。设备上实测有 curl 7.77.0（/sbin/curl，支持 http/https/ftp/tftp），
# 所以这里只依赖设备侧 curl，主机侧用标准库起一个临时服务器即可。
import posixpath
import socket as _socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

LOCAL_HTTP_PORT = 8080           # 主机临时 HTTP 服务器端口


def _safe_basename(path: str) -> str:
    """把 URL 路径清洗成安全的单层文件名，阻断 ../ 目录穿越。"""
    name = unquote(urlparse(path).path).replace("\\", "/")
    name = posixpath.basename(name).lstrip(".") or "unnamed"
    keep = "".join(ch for ch in name if ch.isalnum() or ch in "._-+")
    return (keep or "unnamed")[:200]


class _UploadHandler(BaseHTTPRequestHandler):
    """只服务 serve_dir，只接收来自 allow_ip 的上传。"""
    protocol_version = "HTTP/1.1"
    server_version = "XG040G-FileServer/1.0"
    serve_dir = "."
    recv_dir = "."
    allow_ip = None
    quiet = False

    def log_message(self, fmt, *args):
        if not self.quiet:
            print(f"    [http] {self.client_address[0]} " + (fmt % args), flush=True)

    def _deny(self) -> bool:
        if self.allow_ip and self.client_address[0] != self.allow_ip:
            self.send_error(403, "forbidden source")
            return True
        return False

    def _reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_body_to(self, fh) -> int:
        """把请求体流式写入 fh，兼容 chunked 与 Content-Length。"""
        total = 0
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    continue
                if b";" in line:
                    line = line.split(b";", 1)[0]
                try:
                    size = int(line, 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        t = self.rfile.readline(65536)
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(262144, left))
                    if not chunk:
                        return total
                    fh.write(chunk)
                    total += len(chunk)
                    left -= len(chunk)
                self.rfile.read(2)
        else:
            left = int(self.headers.get("Content-Length") or 0)
            while left > 0:
                chunk = self.rfile.read(min(262144, left))
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                left -= len(chunk)
        return total

    def do_GET(self):
        if self._deny():
            return
        path = os.path.join(self.serve_dir, _safe_basename(self.path))
        if not os.path.isfile(path):
            self._reply(404, b"not found")
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _upload(self):
        if self._deny():
            return
        os.makedirs(self.recv_dir, exist_ok=True)
        name = _safe_basename(self.path)
        path = os.path.join(self.recv_dir, name)
        with open(path, "wb") as f:
            total = self._read_body_to(f)
        self._reply(200, f"stored {name} {total}".encode())

    do_PUT = _upload
    do_POST = _upload


class TempFileServer:
    """后台线程运行的临时 HTTP 文件服务器。"""

    def __init__(self, serve_dir, recv_dir, allow_ip=None, port=LOCAL_HTTP_PORT, quiet=True):
        self.serve_dir = os.path.abspath(serve_dir)
        self.recv_dir = os.path.abspath(recv_dir)
        self.port = port
        handler = type("_H", (_UploadHandler,), {
            "serve_dir": self.serve_dir, "recv_dir": self.recv_dir,
            "allow_ip": allow_ip, "quiet": quiet,
        })
        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
        self.httpd.daemon_threads = True
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        name="http-file-server", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

def run_cmd(tn, cmd: str, timeout: int = 30, prompt="#") -> str:
    """执行一条命令并把输出读到提示符为止。

    关键：先排空 cookedq 里的残留提示符。否则上一步（如 su 确认）留下的 '#'
    会让 expect 立刻命中并返回空输出 —— 实测表现为 curl 明明存在却被判成
    "设备上没有 curl"。
    """
    try:
        tn.read_very_eager()
    except Exception:  # noqa: BLE001
        pass
    tn.write(cmd.encode() + b"\n")
    return read_until_prompt(tn, [prompt.encode()], timeout=timeout)

def device_has_curl(tn) -> bool:
    """探测设备侧是否可用 curl（HTTP 方案的前提）。"""
    out = run_cmd(tn, "which curl; echo CURLEXIT=$?", timeout=20)
    return "CURLEXIT=0" in out

def collect_device_info(tn):
    """从设备读取机型 / 序列号 / 软硬件版本等信息（需 root）。

    各字段的可靠来源（均已实测）：
      - 序列号：必须取 `hostname` 的最后一段。形如
        HLSC-XG140GMD-NBELFCFD60A5 → NBELFCFD60A5。
        不能用 cfgcli 的 DeviceInfo.SerialNumber：`cfgcli -g` 会把它脱敏成
        ******，而 `cfgcli -a` 里是 ealgo="ab" 的密文。
      - 机型/厂商/版本：`cfgcli -g InternetGatewayDevice.DeviceInfo.<K>`。
      - MAC：/sys/class/net/br0/address。
    """
    info = {}

    def _clean(out, cmd):
        """去掉回显的命令行、提示符行和空行，只留真正的输出。"""
        lines = []
        for line in out.splitlines():
            s = line.strip().strip("#").strip()
            if not s:
                continue
            if s == cmd.strip():        # shell 会把命令本身回显回来
                continue
            if s in ("#", "$"):
                continue
            lines.append(s)
        return lines

    def _one(cmd, key, timeout=20):
        try:
            out = run_cmd(tn, cmd, timeout=timeout)
        except Exception:
            return
        lines = _clean(out, cmd)
        if not lines:
            return
        # cfgcli -g 输出形如  InternetGatewayDevice.DeviceInfo.ModelName = XG-140G-MD
        for line in lines:
            if "=" in line:
                v = line.split("=", 1)[1].strip().strip('"')
                if v and v not in ("******",):
                    info[key] = v
                    return
        v = lines[0].strip()
        if v and v not in ("******",):
            info[key] = v

    # 序列号：hostname 最后一段（cfgcli 的 SerialNumber 被脱敏）
    try:
        out = run_cmd(tn, "hostname", timeout=15)
        for line in _clean(out, "hostname"):
            if "-" in line:
                info["SerialNumber"] = line.rsplit("-", 1)[1]
                info["Hostname"] = line
                break
            info["Hostname"] = line
            break
    except Exception:
        pass

    di = "InternetGatewayDevice.DeviceInfo."
    _one(f"cfgcli -g {di}ModelName", "ModelName")
    _one(f"cfgcli -g {di}Manufacturer", "Manufacturer")
    _one(f"cfgcli -g {di}HardwareVersion", "HardwareVersion")
    _one(f"cfgcli -g {di}SoftwareVersion", "SoftwareVersion")
    _one(f"cfgcli -g {di}ProductClass", "ProductClass")
    _one("cat /sys/class/net/br0/address", "MacAddress")

    return info

def _manifest_path(backup_dir):
    return os.path.join(backup_dir, DEVICE_MANIFEST)

def load_manifest(backup_dir):
    """读取备份目录里的 device.json；不存在或损坏时返回 None。"""
    path = _manifest_path(backup_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_manifest(backup_dir, info, files):
    """写入 device.json：设备身份 + 每个备份文件的名称/大小/SHA256。"""
    path = _manifest_path(backup_dir)
    data = {"device": info, "files": files}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"[*] 已写入备份清单: {path}")

def _sha256sum(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

def verify_backup_complete(backup_dir, info):
    """判断该目录里的备份是否「完整且校验正确」。

    判定标准（全部满足才算完整）：
      1) device.json 存在，且其中 SerialNumber 与当前设备一致；
      2) 17 个分区文件全部存在，大小与清单及 MTD_SIZES 三方吻合；
      3) 每个文件的 SHA256 与清单记录一致（逐字节校验）。
    """
    manifest = load_manifest(backup_dir)
    if not manifest:
        return False, "缺少或无法解析 " + DEVICE_MANIFEST
    old_sn = (manifest.get("device") or {}).get("SerialNumber")
    new_sn = info.get("SerialNumber")
    if not old_sn or not new_sn:
        return False, f"序列号缺失（清单 {old_sn!r} / 当前 {new_sn!r}），无法确认是同一台设备"
    if old_sn != new_sn:
        return False, f"序列号不同（清单 {old_sn} / 当前 {new_sn}）"

    files = manifest.get("files") or {}
    if len(files) < len(MTD_PARTITIONS):
        return False, f"清单只记录了 {len(files)}/{len(MTD_PARTITIONS)} 个分区"
    for i, name in enumerate(MTD_PARTITIONS):
        fname = f"mtd{i}_{name}.bin"
        rec = files.get(fname)
        if not rec:
            return False, f"清单缺少 {fname}"
        fpath = os.path.join(backup_dir, fname)
        if not os.path.isfile(fpath):
            return False, f"备份文件缺失: {fname}"
        size = os.path.getsize(fpath)
        if size != rec.get("size"):
            return False, f"{fname} 大小不符（{size} != 清单 {rec.get('size')}）"
        expected = MTD_SIZES.get(i)
        if expected and size != expected:
            return False, f"{fname} 大小与 MTD_SIZES 不符（{size} != {expected}）"
        if _sha256sum(fpath) != rec.get("sha256"):
            return False, f"{fname} SHA256 校验失败（文件可能损坏）"
    return True, f"序列号 {new_sn} 的备份完整且校验通过"

def find_verified_backup(current_info, root_dir="."):
    """在 root_dir 下扫描已有 backup_* 目录，找出同一台设备且校验完整的备份。

    返回 (目录路径, 说明)；没找到返回 (None, 原因)。
    """
    try:
        entries = sorted(os.listdir(root_dir), reverse=True)
    except OSError:
        return None, "无法列目录"
    for name in entries:
        if not name.startswith("backup_"):
            continue
        d = os.path.join(root_dir, name)
        if not os.path.isdir(d):
            continue
        ok, why = verify_backup_complete(d, current_info)
        if ok:
            return d, why
    return None, "未找到同一序列号且校验完整的备份"

def backup_all_partitions(tn, local_ip, backup_dir):
    """用 dd + curl(HTTP) 备份所有 MTD 分区到 backup_dir。

    相对旧版 nc 实现的改进：HTTP 是请求-响应模型，不存在"监听起晚了就丢数据"
    的竞态；每个分区独立一次 PUT，失败可单独重试，不会污染其它分区。
    """
    os.makedirs(backup_dir, exist_ok=True)
    print(f"[*] 备份目录: {backup_dir}")

    if not device_has_curl(tn):
        print("[!] 设备上没有 curl，无法使用 HTTP 方式备份。")
        print("[!] 可改用其它方式（如 tftp / ftpput），或确认 busybox 是否带 wget。")
        sys.exit(1)

    with TempFileServer(serve_dir=backup_dir, recv_dir=backup_dir,
                        allow_ip=ROUTER_IP, port=LOCAL_HTTP_PORT) as srv:
        print(f"[*] 主机临时 HTTP 服务器: http://{local_ip}:{LOCAL_HTTP_PORT}")
        print("[*] 开始备份所有 MTD 分区（使用 dd + curl 上传）...")
        for i, name in enumerate(MTD_PARTITIONS):
            fname = f"mtd{i}_{name}.bin"
            filepath = os.path.join(backup_dir, fname)
            print(f"  → 备份 mtd{i} ({name}) ...")
            # 先删掉可能存在的旧文件，避免把上次的残留误判成本次成功
            try:
                os.remove(filepath)
            except OSError:
                pass

            cmd = (f"dd if=/dev/mtd{i} bs=1M 2>/dev/null | "
                   f"curl -s -T - http://{local_ip}:{LOCAL_HTTP_PORT}/{fname}")
            drain(tn)                       # 先排空，避免上一步的 '#' 让它立刻返回
            tn.write(cmd.encode() + b"\n")
            # 大分区（如 all_flash 236MB）需要更长时间，按大小给足超时
            got = wait_for(tn, "#", timeout=max(60, _expected_size(i) // 1024 // 512))
            if not got:
                print(f"[!] 读取 mtd{i} ({name}) 失败（等待提示符超时）")
                sys.exit(1)

            size = os.path.getsize(filepath) if os.path.isfile(filepath) else 0
            if size == 0:
                print(f"[!] mtd{i} ({name}) 备份为空，中止以免留下不完整备份")
                sys.exit(1)
            expected = MTD_SIZES.get(i)
            if expected and size != expected:
                print(f"[!] mtd{i} ({name}) 大小不符：收到 {size}，期望 {expected}")
                sys.exit(1)
            print(f"    → {filepath} ({size} 字节)")

    # === 备份完成后写入清单（机型/序列号 + 每个文件的 SHA256）===
    # 清单是"下次遇到同一台设备可跳过备份"的判定依据：没有它就无法证明
    # 这份备份完整且属于当前设备。
    print("[*] 计算各分区 SHA256 并写入设备清单（用于下次跳过备份）...")
    info = collect_device_info(tn)
    files = {}
    for i, name in enumerate(MTD_PARTITIONS):
        fname = f"mtd{i}_{name}.bin"
        fpath = os.path.join(backup_dir, fname)
        if os.path.isfile(fpath):
            files[fname] = {"size": os.path.getsize(fpath), "sha256": _sha256sum(fpath)}
    save_manifest(backup_dir, info, files)
    sn = info.get("SerialNumber", "未知")
    model = info.get("ModelName", "未知")
    print(f"[✓] 设备信息: {model} / SN {sn}")

def _expected_size(idx: int) -> int:
    """从 MTD_SIZES 取该分区的期望字节数；未知则给一个宽松的默认值。"""
    return MTD_SIZES.get(idx, 16 * 1024 * 1024)

def flash_uboot(tn, local_ip):
    """用 HTTP 上传 U-Boot 并 dd 写入 /dev/mtdblock0，然后回读校验（危险操作）"""
    print("[*] 上传 U-Boot 并使用 dd 写入 /dev/mtd0 ...")

    if not device_has_curl(tn):
        print("[!] 设备上没有 curl，无法使用 HTTP 方式上传 U-Boot。")
        sys.exit(1)

    uboot_size = os.path.getsize(UBOOT_FILE)
    with TempFileServer(serve_dir=".", recv_dir=".", allow_ip=ROUTER_IP,
                        port=LOCAL_HTTP_PORT) as srv:
        print(f"[*] 主机临时 HTTP 服务器: http://{local_ip}:{LOCAL_HTTP_PORT}")
        tn.write(f"curl -s -o /tmp/uboot.bin http://{local_ip}:{LOCAL_HTTP_PORT}/{UBOOT_FILE}\n".encode())
        if not wait_for(tn, "#", timeout=120):
            print("[!] U-Boot 下载超时")
            sys.exit(1)

    print("[*] 校验 U-Boot 文件...")
    tn.write(b"sha512sum /tmp/uboot.bin\n")
    ret = read_until_prompt(tn, ["#".encode()], timeout=30)
    lines = [l for l in ret.split("\n") if l.strip()]
    print("   ", lines[0] if lines else "(无输出)")
    if not lines or not lines[0].startswith(EXPECTED_SHA512):
        print("[!] U-Boot 校验失败")
        sys.exit(1)
    print("[✓] U-Boot 校验通过")

    # === 关键：使用 dd 写入 /dev/mtdblock0 ===
    # 为什么写完不能立刻回读判定（实测踩过）：
    #   /dev/mtdblock0 是**块设备**，dd 写下去的数据先落到 block 层的页缓存，
    #   dd 退出（close）只保证把数据交给 mtdblock，**不保证**已经写进 flash
    #   芯片：mtdblock 由内核的 mtd_blktrans 线程异步完成擦除+写入，dd 返回时
    #   写请求可能还在队列里。而校验用的 /dev/mtd0 是**字符设备**，直接读芯片
    #   （不走块层缓存），所以这时读回来的还是旧数据，要等一会儿才变成新数据。
    #   ⇒ 写完马上读一次就断言失败属于误判，必须轮询等待。
    # 对策：写完先 sync 推一次，然后**轮询回读**直到哈希匹配或超时。
    tn.write(b"dd if=/tmp/uboot.bin of=/dev/mtdblock0; echo DD_RC=$?\n")
    ret = read_until_prompt(tn, ["#".encode()], timeout=120)
    m = re.search(r"DD_RC=(\d+)", ret)
    if m and m.group(1) != "0":
        print(f"[!] dd 写入失败（返回码 {m.group(1)}），未做后续校验。")
        sys.exit(1)

    if not verify_mtd_hash(tn, 0, EXPECTED_SHA512, label="U-Boot"):
        print("[!] 请想办法手动还原回去，以免你的光猫变成砖。")
        sys.exit(1)

    print("[✓] 刷写完成！")

def verify_mtd_hash(tn, index, expected_sha, label=None,
                    timeout=None, interval=None):
    """回读 /dev/mtd{index} 并校验 SHA512，**轮询等待写入生效**。

    写 mtdblock 后数据要过一会儿才真正进 flash（见 flash_uboot 注释），
    所以这里不能读一次就下结论：循环「sync → dd 回读 → sha512sum」直到
    匹配或超时。返回 True/False。

    注意读取用 /dev/mtdN（字符设备，直读芯片）而不是 /dev/mtdblockN
    （块设备，会命中 mtdblock 自己的缓存，可能读到还没写下去的旧数据）。
    """
    if label is None:
        label = f"mtd{index}"
    if timeout is None:
        timeout = FLASH_VERIFY_TIMEOUT
    if interval is None:
        interval = FLASH_VERIFY_INTERVAL

    print(f"[*] 回读 /dev/mtd{index} 校验 {label}（数据生效有延迟，最多轮询 "
          f"{timeout} 秒，每 {interval} 秒一次）...")
    deadline = time.time() + timeout
    attempt = 0
    while True:
        attempt += 1
        # sync 把 block 层脏页推给 mtdblock，并促使它尽快写入芯片
        run_cmd(tn, "sync", timeout=60)

        drain(tn)   # 先排空，否则上一步的 '#' 会让 expect 立刻返回空输出
        tn.write(f"dd if=/dev/mtd{index} of=/tmp/_verify_mtd{index}.bin "
                 f"2>/dev/null; sha512sum /tmp/_verify_mtd{index}.bin\n".encode())
        ret = read_until_prompt(tn, ["#".encode()], timeout=120)
        m = re.search(r"\b([0-9a-f]{128})\b", ret)
        got = m.group(1) if m else None
        print(f"    第 {attempt} 次回读: {got or '(无输出)'}")

        if got == expected_sha:
            print(f"[✓] {label} 校验通过（第 {attempt} 次回读）")
            return True

        if time.time() >= deadline:
            print(f"[!] {label} 校验失败：轮询 {timeout} 秒后回读仍与期望不符"
                  f"（最后一次: {got or '(无输出)'}）。")
            return False
        print(f"[!] 第 {attempt} 次回读仍是旧数据，{interval} 秒后重试 ...")
        time.sleep(interval)


def _unique_backup_dir():
    """生成不冲突的备份目录名（同一秒内多次运行 / GUI 里连续点两次时用得上）。"""
    base = f"backup_{time.strftime('%Y%m%d_%H%M%S')}"
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}_{n}"):
        n += 1
    return f"{base}_{n}"


def run_flow(args, confirm_flash=None):
    """按 args 执行完整流程。

    CLI（main）和 GUI（gui.py）共用这一条代码路径，避免刷机逻辑出现两份实现。
    confirm_flash: 可调用对象，返回 True 表示用户确认刷写。为 None 时走
    命令行 input()；GUI 传入对话框回调。
    """
    mode = args["mode"]
    mode_desc = {
        "telnet": "仅开启 Telnet（不碰 FTP、不备份、不刷机）",
        "ftp":    "仅开启 FTP（su 提权用，不备份、不刷机）",
        "backup": "开启 Telnet + 备份全部分区（不刷机）",
        "flash":  "开启 Telnet + 备份 + 刷写 U-Boot（危险！）",
    }[mode]
    print("=" * 60)
    print(f"  XG-040G-MD 工具  ·  运行模式: {mode} — {mode_desc}")
    print("=" * 60)

    # === 0. --reboot：只重启光猫然后退出 ===
    if args.get("reboot"):
        print("[*] --reboot 模式：仅重启光猫（清掉所有网页会话 / telnet 锁定）")
        reboot_router()
        return

    # === 1. 验证 U-Boot 文件（仅 flash 模式需要） ===
    if mode == "flash":
        if not os.path.isfile(UBOOT_FILE):
            print(f"[!] 找不到 U-Boot 文件: {UBOOT_FILE}")
            sys.exit(1)

        uboot_size = os.path.getsize(UBOOT_FILE)
        if uboot_size > 0x80000:  # mtd0 大小为 0x80000 (512KB)
            print(f"[!] 错误: U-Boot 文件过大 ({uboot_size} 字节)，mtd0 仅 512KB！")
            sys.exit(1)

        if not EXPECTED_SHA512:
            print("[!] 请设置 EXPECTED_SHA512")
            print(f"当前 SHA512: {sha512sum(UBOOT_FILE)}")
            sys.exit(1)

        actual_sha = sha512sum(UBOOT_FILE)
        if actual_sha != EXPECTED_SHA512:
            print(f"[!] SHA512 校验失败！\n期望: {EXPECTED_SHA512}\n实际: {actual_sha}")
            sys.exit(1)
        print("[✓] U-Boot 校验通过，大小合规。")
    else:
        print(f"[*] 当前模式 {mode} 不需要 U-Boot 文件，跳过固件校验。")

    # === 2. 准备环境 ===
    # 只有 backup / flash 真正用到本机 IP（给设备 curl 回传分区用），
    # telnet 和 ftp 模式只是开关服务，不必探测。
    local_ip = None
    if mode in ("backup", "flash"):
        local_ip = get_local_ip(ROUTER_IP)
        # 如果你使用WSL2请使用本地IP地址，WSL内运行可能无法正确获得本地IP。
        # local_ip = STATIC_LOCAL_IP
        print(f"[*] 本机 IP: {local_ip}")

    # === 3. 自动开启 Telnet（免拆机） ===
    # telnet 与 ftp 是两个独立开关：--mode ftp 只碰 21 端口，不动 telnet。
    if mode == "ftp":
        print("[*] 模式 ftp：只开 FTP，跳过 Telnet 步骤。")
    elif args["enable_telnet"]:
        enable_telnet_via_web()
    else:
        print("[*] --no-enable-telnet：跳过自动开启 Telnet（假定已手动开启）。")

    # === 3b. 自动开启 FTP（su 提权的前提）===
    # 顺序很重要：必须先开 FTP，user_ftp 账号才会被注入 /etc/passwd，
    # 否则后面的 su user_ftp 一定报 unknown user，拿不到 root。
    # 注意：telnet 模式只碰 23 端口，不碰 FTP；需要开 FTP 请用 --mode ftp。
    if mode == "telnet":
        print("[*] 模式 telnet：只开 Telnet，跳过 FTP 步骤。")
    elif args["enable_ftp"]:
        enable_ftp_via_web()
    else:
        print("[*] --no-enable-ftp：跳过自动开启 FTP（假定已手动开启）。")

    if mode == "telnet":
        print("\n[✓] Telnet 已开启，模式 telnet 到此结束（未做任何备份或写入）。")
        print("    需要开 FTP（su 提权用）请运行: python3 flash.py --mode ftp")
        print("    需要备份请运行: python3 flash.py --mode backup")
        return

    if mode == "ftp":
        print("\n[✓] FTP 已开启，模式 ftp 到此结束（未做任何备份或写入）。")
        print("    需要备份请运行: python3 flash.py --mode backup")
        return

    # === 4. Telnet 登录 ===
    tn = telnet_login()

    # 显示分区表
    tn.write(b"cat /proc/mtd\n")
    ret = read_until_prompt(tn, ["#".encode()], timeout=30)
    print(ret)

    backup_dir_used = None

    # === 5. 备份所有分区 ===
    if mode in ("backup", "flash"):
        # 先采集设备身份（机型/序列号），用于「同一台设备且备份完整就跳过」判定
        device_info = collect_device_info(tn)
        model = device_info.get("ModelName", "未知")
        sn = device_info.get("SerialNumber", "未知")
        hw = device_info.get("HardwareVersion", "未知")
        sw = device_info.get("SoftwareVersion", "未知")
        print(f"[*] 设备: {model}（{device_info.get('Manufacturer', '?')}）"
              f" SN={sn} 硬件={hw} 软件={sw}")

        skipped_dir = None
        if not args.get("force_backup"):
            found, why = find_verified_backup(device_info, ".")
            if found:
                skipped_dir = found
                print(f"[✓] 发现同一台设备（SN {sn}）的完整备份，跳过备份：{found}")
                print(f"    {why}")
            else:
                print(f"[*] 需要备份（{why}）")

        if skipped_dir:
            backup_dir_used = skipped_dir
        else:
            this_dir = _unique_backup_dir()
            backup_all_partitions(tn, local_ip, this_dir)
            backup_dir_used = this_dir
    else:
        print(f"[*] 模式 {mode}：无需备份。")

    # === 6. 刷写 U-Boot（仅 flash 模式） ===
    if mode == "flash":
        if not args.get("yes"):
            print("\n⚠️  即将把 U-Boot 写入 /dev/mtdblock0，操作不可逆，变砖风险！")
            if confirm_flash is not None:
                if not confirm_flash():
                    print("[*] 已取消，未做任何写入。")
                    tn.close()
                    return
            else:
                answer = input("确认继续请输入 YES（其他任何输入都退出）：").strip()
                if answer != "YES":
                    print("[*] 已取消，未做任何写入。")
                    tn.close()
                    return
        flash_uboot(tn, local_ip)
    else:
        print(f"[*] 模式 {mode}：已完成备份，跳过刷写（不写入任何分区）。")

    print("[*] 断开连接...")
    tn.close()
    print("\n[✓] 操作完成！")
    if mode == "flash":
        print("\n⚠️  重要提示：")
        print("   - 立即手动断电重启光猫！")
        print("   - 若设备无法启动，请使用 SPI 编程器恢复 mtd0！")
    if backup_dir_used:
        print(f"   - 完整备份位于: {backup_dir_used}")


def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(current_dir)
    args = parse_args(sys.argv[1:])
    run_flow(args)


if __name__ == "__main__":
    main()
