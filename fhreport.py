#!/usr/bin/env python3
"""烽火 telnet 密码解不开时，收集诊断材料打包成 zip，交给作者分析。

**为什么需要它**：`fhdecrypt.py` 里的 AES 工作密钥是从固件里逆向出来的，
按容器头（`2506` / `2502` …）索引。如果某个新固件换了密钥表，或者换了加密
算法，本地解密就会失败 —— 而失败信息（"解出来是乱码"）对定位问题几乎没有
帮助。把「设备上真实的密文 + 本地用过的密钥 + 解密结果」原样打包发出来，
作者才能判断是密钥不对、算法变了，还是容器头是新的。

**打包什么**（都是只读的下载，不改设备任何状态）：

  - `device.json`      机型 / SN / MAC / 固件版本 / 内核（跟备份清单同一套字段）
  - `files/`           设备上跟密码相关的文件（`/var/tel_passwd` 等）
  - `ciphertext/`      从上面文件里切出来的密文片段（`2506…` / `2502…`）
  - `attempts.json`    每个路径的下载结果、每个密文的解密结果、试过的密钥
  - `keys.json`        本工具内置的密钥表（k2/iv2 + 对应的容器头）
  - `env.json`         工具版本、Python 版本、平台
  - `README.txt`       这份说明 + 隐私提醒

**隐私**：包里含设备 MAC、PON SN、整机 SN、telnet 密码哈希，可能还有
Wi-Fi 密码等配置。只发给信任的人（作者），不要公开贴到论坛。
"""
import io
import re
import json
import os
import platform
import tarfile
import time
import zipfile
import hashlib

__all__ = ["ReportError", "collect", "TARGET_FILES", "SO_FILES",
           "CIPHER_SCAN_LIMIT"]

# 设备上跟 telnet 密码相关的文件。按「最有用 → 最没用」排序。
#
# 为什么是这几个（真机实测哪些能下、哪些不能）：
#   - /var/tel_passwd、/var/telWan_passwd：telnetd 启动时按配置生成的密码文件，
#     内容是 `用户名:哈希:...`，反映**当前生效**的密码 —— 最直接的证据。
#   - /var/telsu：`su` 提权用的密码文件，同样是 `用户名:哈希:...`。
#   - /fhdata/pre_usrconfig_conf：省份预置包，里面是加密的 `option TelnetPassword`。
#   - /fhconf/usrconfig_conf：当前生效的配置（UCI），加密值可能也在里面。
#   - /fhdata/factory_conf、/fhdata/table：板级数据，含 MAC/SN 等身份字段，
#     用来核对「解密出来的身份」和「设备真实身份」是否一致。
#
# 下不到的（实测 `FhToolUnavailable`，webs 的 DownloadFile 只放行 /fhconf/
# 和 /fhdata/ 两个目录，且要真实存在）：`/fhrom/fhconf/param.pdt.enc`、
# `/fhrom/fhconf/calidata`、`/etc/passwd`、`/etc/shadow`、`/proc/version`。
# 仍然列进来 —— 换个固件可能就放行了，多试一次的成本只有一次 RPC。
TARGET_FILES = (
    "/var/tel_passwd",
    "/var/telWan_passwd",
    "/var/telsu",
    "/fhdata/pre_usrconfig_conf",
    "/fhconf/usrconfig_conf",
    "/fhdata/factory_conf",
    "/fhdata/table",
    "/fhrom/fhconf/param.pdt.enc",
    "/fhconf/param.pdt.enc",
    "/fhrom/fhconf/calidata",
    "/fhconf/calidata",
    "/etc/passwd",
    "/etc/shadow",
)

# 「含解密密钥的 so」—— 这是用户点名要进包的东西。
#
# ## 为什么要它们
# `fhdecrypt.py` 的 AES 工作密钥是运行时由 `libfhcrypto_new.so` 的
# `ks_wk_pdt_get_by_id` 从静态 blob 解出来的，**没法纯静态算**，只能硬编码
# 到工具里（见 HANDOFF 4.3）。哪天固件换了密钥，本地解密就失败，而
# 「解出来是乱码」这种报错对定位毫无帮助 —— 拿到设备上这几个 .so，
# 作者就能在本地用 qemu 复跑一遍 `dec`，直接抓出新的 k2/iv2。
#
# ## 为什么必须绕一圈（重要，别改成直接下载）
# `DownloadFile` 有**硬编码前缀白名单**，只放行 `/fhconf/`、`/fhdata/`、
# `/var/` 三个目录（`webs` 里就是两条字面量 `"/fhconf/"`、`"/fhdata/"` 加
# 一条 `No download permission!` 判定）。实测：
#   /fhrom/lib/libfhcrypto_new.so   → {'result': -1}（拒）
#   /var/tel_passwd                 → 给下载 URL（放行）
# 路径穿越（`/var/../fhrom/...`、`%2e%2e`）、命令注入（`;`、反引号）、
# 下载 URL 穿越（`/fh_tool/download/../../`）**全部被拒**，别浪费时间再试。
# 顺带否掉一条死路：`LogDownload` 对任意路径都回 result:0，但它只会把
# `/var/framework.log` 打包给你，fileName 参数被完全忽略 —— 看着像洞，其实不是。
#
# 所以走「telnet 受限 shell 复制到 /var → 免登录 DownloadFile 下载」：
# telnetadmin 对 `/fhrom/lib/*.so` 可读、对 `/var/` 可写，两边正好接上。
# 代价是必须能登 telnet；登不上就跳过（不阻断整个诊断包）。
SO_FILES = (
    # 解密主库：`fhcfg_dencryp_string_with_mode` / `_bin_with_mode` 都在这里，
    # 工作密钥的派生也在它依赖的 libfhcrypto_new.so 里
    "/fhrom/lib/libfhcfg.so",
    # 密钥就在这个库里（`ks_wk_pdt_get_by_id`）。用户说的「含解密密钥的 so」就是它
    "/fhrom/lib/libfhcrypto_new.so",
    # libfhcrypto_new.so 的 NEEDED 依赖，单独给出来省得作者再找固件
    "/fhrom/lib/libsecurityfunc.so",
)

# so 暂存到设备上的哪个目录。选 /var 的唯一原因：它在 DownloadFile 白名单里，
# 而且 telnetadmin 可写。/tmp 虽然也能写，但 DownloadFile 不放行（实测拒）。
SO_STAGE_DIR = "/var"

# 暂存文件名前缀。带前缀是为了不跟设备自己的文件撞名（/var 下已有
# tel_passwd、framework.log 等），清理时也好认。
SO_STAGE_PREFIX = "fhrep_"

# 扫描密文时最多看每个文件的前多少字节。param.pdt.enc 有 1.7MB，但密文片段
# 只可能是小块的配置值，扫前面一段就够；避免把整个 1.7MB 都塞进 zip。
CIPHER_SCAN_LIMIT = 256 * 1024

# 单个文件超过这个大小就不放进包里（只记元信息）。避免有人把整个 rootfs
# 分区拖进来把 zip 撑到几百 MB。
MAX_FILE_BYTES = 4 * 1024 * 1024

# 每个文件最多保留多少段密文。实测 `pre_usrconfig_conf` 里有 131 段互不相同
# 的密文（SSID、NTP、时区、TR069 账号……），全列进 attempts.json 只会让作者
# 翻不到重点。密码相关的字段由 `_field_priority()` 排到最前，所以取前 N 段
# 就能覆盖到真正要查的那几段。
MAX_CIPHERS_PER_FILE = 24


class ReportError(Exception):
    """收集失败（设备不可达 / 一个文件都没取到）。"""


def _unwrap_tar(raw, name=""):
    """设备导出的文件外面裹了一层 tar.gz，拆开取第一个普通文件。

    `DownloadFile` 的实现是设备上跑 `tar -cvf "%s%s.tar.gz" "%s"`，所以拿到的
    2048 字节里大部分是 tar 头 + 512 字节对齐的填充，真实内容可能只有几十
    字节（`/var/telsu` 就是 81 字节）。不拆包直接看会以为拿到了二进制垃圾。
    """
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw))
    except Exception:  # noqa: BLE001 - 不是 tar 就原样返回
        return raw, name
    for m in tf.getmembers():
        if m.isfile():
            try:
                return tf.extractfile(m).read(), m.name
            except Exception:  # noqa: BLE001
                continue
    return raw, name


# 字段名里带这些词的密文优先保留 —— 上报的目的是查 telnet 密码，131 段
# 「SSID 别名」「NTP 服务器」之类的密文对定位问题毫无帮助，只会淹没重点。
PRIORITY_FIELD_WORDS = (
    "telnet", "password", "passwd", "pwd", "admin", "user", "account",
    "login", "secret", "key", "auth",
)


def _field_priority(field):
    """字段名越像「密码相关」，优先级越高（数字越小越优先）。"""
    f = (field or "").lower()
    for i, w in enumerate(PRIORITY_FIELD_WORDS):
        if w in f:
            return i
    return len(PRIORITY_FIELD_WORDS)


def _find_ciphertexts(blob, magics, limit=None):
    """在二进制或文本里找所有以已知容器头开头的密文片段。

    返回 [{"offset", "magic", "hex", "length", "form", "field"}]。

    去重：**按密文内容去重**（同一个值会出现在几十个配置块里）。排序：字段名
    像密码的先排（`TelnetPassword` 在最前），其余保持出现顺序。`limit` 非空
    时只保留前 N 段 —— 一个 `pre_usrconfig_conf` 里实测有 131 段不同的密文，
    全塞进上报包对作者没有帮助，反而找不到重点。

    两种存放形式都要认：
      - **二进制**：`param.pdt.enc` 这类参数总库里是裸字节 `25 06 00 00 …`；
      - **文本**：UCI 配置（`pre_usrconfig_conf`）里是十六进制字符串
        `option TelnetPassword '250200008554F8E4…'`，实测很常见。
    早期版本只找裸字节，于是 UCI 文件里的密文一段都切不出来 —— 而这恰恰是
    用户上报时最该看的东西。

    `field` 是文本形式下能看到的 UCI 键名（`option <name> '<hex>'`），作者一眼
    就能知道这段密文是 `TelnetPassword` 还是 `SSID`。
    """
    out = []
    if not blob:
        return out
    seen = set()

    def _push(item):
        if item["hex"] in seen:
            return
        seen.add(item["hex"])
        out.append(item)

    head = blob[:CIPHER_SCAN_LIMIT]

    # ---- 形式 1：裸字节 ----
    for magic in magics:
        start = 0
        while True:
            i = head.find(magic, start)
            if i < 0:
                break
            start = i + 1
            n = _count_aes_blocks(head, i + 4)
            if n == 0:
                continue
            frag = head[i: i + 4 + n * 16]
            _push({
                "offset": i,
                "magic": magic.hex(),
                "hex": frag.hex(),
                "length": len(frag),
                "form": "binary",
                "field": "",
            })

    # ---- 形式 2：十六进制字符串 ----
    # 用 latin-1 把字节当文本扫，这样偏移量和原文一致，方便作者对照。
    text = head.decode("latin-1")
    for magic in magics:
        token = magic.hex()          # 例如 "25060000"
        start = 0
        while True:
            i = text.find(token, start)
            if i < 0:
                break
            start = i + 1
            # 往后吃十六进制字符，按 32 个字符（16 字节）一段
            j = i + len(token)
            while j < len(text) and text[j] in "0123456789abcdefABCDEF":
                j += 1
            hexstr = text[i:j]
            n = (len(hexstr) - len(token)) // 32
            if n < 1:
                continue
            hexstr = hexstr[: len(token) + n * 32]
            _push({
                "offset": i,
                "magic": magic.hex(),
                "hex": hexstr,
                "length": len(hexstr) // 2,
                "form": "text",
                "field": _uci_field_name(text, i),
            })

    # 密码相关的排前面（稳定排序，同优先级保持出现顺序）
    out.sort(key=lambda it: _field_priority(it.get("field")))
    if limit is not None:
        out = out[:limit]
    return out


def _uci_field_name(text, pos):
    """从密文位置往前找最近的 `option <名字>`，返回那个名字。

    UCI 的写法是 `option TelnetPassword '25020000…'`，所以从密文起点往回
    找 200 个字符内最后一次出现的 `option ` 即可。找不到就返回空串。
    """
    window = text[max(0, pos - 200): pos]
    m = None
    for m2 in re.finditer(r"option\s+([A-Za-z0-9_]+)\s*'?\s*$", window):
        m = m2
    return m.group(1) if m else ""



def _count_aes_blocks(head, body):
    """从 body 起数有多少个「非全 0 / 非全 FF」的 16 字节块（最多 64 个）。"""
    n = 0
    while n < 64 and body + (n + 1) * 16 <= len(head):
        block = head[body + n * 16: body + (n + 1) * 16]
        if block == b"\xff" * 16 or block == b"\x00" * 16:
            break
        n += 1
    return n



def _try_decrypt(fhdecrypt, hex_str):
    """对一段密文试遍所有已知密钥，返回可读结果或失败原因。

    这是给作者看的核心信息：如果是「密钥不对」，这里会显示每个容器头都解出
    乱码；如果是「容器头没见过」，会显示 no known magic；如果解出来是正常
    明文，那说明工具本身的候选密码表该更新了。
    """
    raw = bytes.fromhex(hex_str)
    magic = raw[:4]
    info = {
        "magic": magic.hex(),
        "known_magic": magic in getattr(fhdecrypt, "CIPHER_KEYS", {}),
        "results": [],
    }
    if not info["known_magic"]:
        info["verdict"] = "容器头不在已知密钥表里 —— 可能是新固件换了加密方式"
        return info
    for mode in ("as-is", "no-magic"):
        try:
            if mode == "as-is":
                plain = fhdecrypt.decrypt_value(hex_str)
            else:
                # 有些固件不在密文前加 4 字节容器头
                plain = fhdecrypt.decrypt_value(raw[4:].hex(), has_magic=False)
        except Exception as e:  # noqa: BLE001
            info["results"].append({"mode": mode, "error": f"{type(e).__name__}: {e}"})
            continue
        try:
            text = plain.decode("utf-8")
            printable = text.isprintable()
        except UnicodeDecodeError:
            text, printable = "", False
        info["results"].append({
            "mode": mode,
            "length": len(plain),
            "printable": printable,
            # 可打印就原样给出（作者要看的正是这个）；不可打印只给前 32 字节，
            # 避免把大段二进制垃圾塞进 JSON。
            "plain": text if printable else plain[:32].hex(),
        })
        if printable:
            info["verdict"] = "解密成功"
            return info
    info["verdict"] = "所有已知密钥都解不出可打印明文 —— 密钥可能已更换"
    return info


def _collect_so_files(client, flash_mod, log=print):
    """把设备上 `/fhrom/lib/*.so` 搬进诊断包，返回 (meta_list, {zip相对路径: 内容})。

    ## 为什么要绕一圈（别改成直接 DownloadFile）
    `DownloadFile` 有硬编码前缀白名单，只放行 `/fhconf/`、`/fhdata/`、`/var/`。
    实测（V4.20.M5000）：
        /var/tel_passwd                 → 给下载 URL（放行）
        /fhrom/lib/libfhcrypto_new.so   → {'result': -1}（拒）
    顺手排除的死路，别再试：
      · 路径穿越 `/var/../fhrom/...`、`/var/%2e%2e/...`、`/fh_tool/download/../..`
        —— 全部被拒或 400
      · 命令注入 `;` / 反引号 / `&&` / `|` —— 参数没进 shell，注入不成立
      · `LogDownload` —— 对**任意**路径都回 result:0，但 fileName 参数被完全
        忽略，永远只打包 /var/framework.log。看着像洞，其实不是。
    真正能用的路：telnetadmin 的受限 shell 对 `/fhrom/lib/*.so` 可读、对
    `/var/` 可写，所以「cat 到 /var → 免登录 DownloadFile 下载」两边正好接上。

    ## 为什么要一个会话搬完
    `telnet_login()` 会 su 到 root，每次都是一次完整认证。连三次就是烧三次
    认证机会（这台设备 3 次失败锁 300 秒），而且日志会刷屏。所以：登录一次 →
    三个文件全 cat 到 /var → 全部下载 → 统一 rm。

    ## 只读约束
    唯一的写操作是往 /var 丢 `fhrep_` 前缀的临时副本，函数返回前一定删掉
    （下载完才删 —— 早删会让 DownloadFile 找不到文件，实测踩过）。
    不改任何配置、不碰任何系统文件。

    任何一步失败都不抛给上层：so 只是加分项，取不到就记原因，诊断包照常生成。
    """
    meta, payloads = [], {}
    staged_all = []

    tn = None
    try:
        tn = flash_mod.telnet_login()
    except Exception as e:  # noqa: BLE001
        tn = None
        log(f"  [!] telnet 登录异常，跳过 so：{type(e).__name__}: {e}")
    if not tn:
        for remote in SO_FILES:
            meta.append({"remote": remote, "status": "unavailable",
                         "error": "telnet 登录失败（这条通道必须登录）"})
        log("  [--] so 全部跳过：telnet 登不上")
        return meta, payloads

    try:
        # --- 1) 全部暂存到 /var ---
        for remote in SO_FILES:
            base = os.path.basename(remote)
            staged = f"{SO_STAGE_DIR}/{SO_STAGE_PREFIX}{base}"
            entry = {"remote": remote}
            # 用 cat 而不是 cp：实测受限 shell 里 `cp` 直接 Operation not
            # permitted（CP=255），而 `cat > 文件` 可以。
            out = flash_mod.run_cmd(
                tn, f"cat {remote} > {staged}; echo SOCOPY=$?", timeout=60)
            if "SOCOPY=0" not in out:
                entry["status"] = "unavailable"
                entry["error"] = f"设备上复制失败：{out.strip()[:160]}"
                meta.append(entry)
                continue
            # 记下设备侧 md5，下载后比对，避免把截断的文件当成功
            out = flash_mod.run_cmd(tn, f"md5sum {staged}", timeout=30)
            entry["device_md5"] = ""
            for tok in out.split():
                if len(tok) == 32 and all(c in "0123456789abcdef" for c in tok):
                    entry["device_md5"] = tok
                    break
            entry["_staged"] = staged
            meta.append(entry)
            staged_all.append(staged)

        # --- 2) 趁文件还在，逐个下载（下载完才 rm，顺序不能反）---
        workdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "_report_tmp")
        os.makedirs(workdir, exist_ok=True)
        for entry in meta:
            staged = entry.pop("_staged", None)
            if not staged:
                continue
            base = os.path.basename(entry["remote"])
            local = os.path.join(workdir, base + ".tar")
            try:
                got = client.download_file(staged, local, timeout=120.0)
                raw = _read_file_bytes(local)
            except Exception as e:  # noqa: BLE001
                entry["status"] = "unavailable"
                entry["error"] = f"{type(e).__name__}: {e}"
                log(f"  [--] {entry['remote']}（{type(e).__name__}）")
                continue
            finally:
                try:
                    os.remove(local)
                except OSError:
                    pass

            content, inner = _unwrap_tar(raw, base)
            if not content:
                entry["status"] = "unavailable"
                entry["error"] = f"下载回来是空的（tar {got.get('bytes')} 字节）"
                log(f"  [--] {entry['remote']}（空文件）")
                continue
            real = hashlib.md5(content).hexdigest()
            if entry.get("device_md5") and real != entry["device_md5"]:
                entry["status"] = "unavailable"
                entry["error"] = (f"校验不一致：设备 {entry['device_md5']} / "
                                  f"本地 {real}（可能被截断）")
                log(f"  [--] {entry['remote']}（md5 不一致）")
                continue

            entry.update({
                "status": "ok",
                "real_bytes": len(content),
                "inner_name": inner,
                "md5": real,
                "sha256": hashlib.sha256(content).hexdigest(),
                "elf": content[:4] == b"\x7fELF",
            })
            # ELF 头里能直接读出机器类型，作者据此判断该用哪个 qemu
            if entry["elf"] and len(content) > 20:
                entry["elf_machine"] = int.from_bytes(content[18:20], "little")
            rel = f"so/{base}"
            payloads[rel] = content
            entry["zip_path"] = rel
            log(f"  [ok] {entry['remote']}（{len(content)} 字节，md5 {real}）")
    finally:
        # --- 3) 无论如何都清掉设备上的临时副本 ---
        for staged in staged_all:
            try:
                flash_mod.run_cmd(tn, f"rm -f {staged}", timeout=20)
            except Exception:  # noqa: BLE001
                pass
        try:
            tn.close()
        except Exception:  # noqa: BLE001
            pass

    return meta, payloads


def _read_file_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def collect(flash_mod, out_path=None, log=print):
    """收集诊断材料并写成 zip，返回 zip 路径。

    `flash_mod` 是已经加载好的 flash 模块（要读它的 ROUTER_IP / MODEL_MAC /
    MODEL 等全局）；`out_path` 省略时写到程序目录下的
    `fh_report_<时间戳>.zip`。
    """
    import fhtool
    import fhdecrypt

    if not fhtool.available():
        raise ReportError("没装 fh_tool-cli，无法从设备下载文件（pip install fh-tool-cli）")

    ip = getattr(flash_mod, "ROUTER_IP", "192.168.1.1")
    port = getattr(flash_mod, "WEB_PORT", 8080)
    mac = getattr(flash_mod, "MODEL_MAC", "") or ""
    if not mac:
        # 现场探一次 MAC：fh_tool 的密钥由 MAC 派生，没有它连不上
        mac = fhtool.probe_mac(ip, port=port, timeout=6.0) or ""
        if mac and hasattr(flash_mod, "MODEL_MAC"):
            flash_mod.MODEL_MAC = mac
    if not mac:
        raise ReportError(
            f"读不到光猫 MAC（{ip}:{port}/cgi-bin/ajax?ajaxmethod=web_get_brmad）。"
            "请确认电脑接在光猫 LAN 口、地址填对了。"
        )

    log(f"[*] 从设备 {ip}:{port} 收集诊断材料（MAC {mac}）...")
    client = fhtool.FhClient(ip, mac, port=port)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    if not out_path:
        base = os.path.dirname(os.path.abspath(__file__))
        out_path = os.path.join(base, f"fh_report_{stamp}.zip")

    # 临时目录：放下载回来的原始 tar（不直接进 zip，拆包后再写）
    workdir = os.path.join(os.path.dirname(os.path.abspath(out_path)), "_report_tmp")
    os.makedirs(workdir, exist_ok=True)

    files_meta = []      # 每个路径的下载结果
    file_payloads = {}   # zip 内相对路径 -> 真实内容
    ciphertexts = []     # 切出来的密文片段

    for remote in TARGET_FILES:
        name = remote.strip("/").replace("/", "_")
        local = os.path.join(workdir, name + ".tar")
        entry = {"remote": remote}
        try:
            got = client.download_file(remote, local, timeout=60.0)
        except Exception as e:  # noqa: BLE001
            entry["status"] = "unavailable"
            entry["error"] = f"{type(e).__name__}: {e}"
            files_meta.append(entry)
            log(f"  [--] {remote}（{type(e).__name__}）")
            continue

        size = got.get("bytes") or 0
        try:
            raw = _read_file_bytes(local)
        except OSError as e:
            entry["status"] = "read-failed"
            entry["error"] = str(e)
            files_meta.append(entry)
            continue

        content, inner_name = _unwrap_tar(raw, name)
        entry.update({
            "status": "ok",
            "download_bytes": size,
            "real_bytes": len(content),
            "inner_name": inner_name,
        })

        if len(content) > MAX_FILE_BYTES:
            entry["note"] = f"超过 {MAX_FILE_BYTES} 字节，只记录元信息"
        else:
            rel = f"files/{name}"
            file_payloads[rel] = content
            entry["zip_path"] = rel

        # 从内容里切密文片段（每个文件最多留 MAX_CIPHERS_PER_FILE 段，
        # 密码相关的排前面 —— 实测 pre_usrconfig_conf 有 131 段，全留没意义）
        frags = _find_ciphertexts(content, getattr(fhdecrypt, "ALL_MAGICS", ()),
                                  limit=MAX_CIPHERS_PER_FILE)
        for fr in frags:
            fr["from"] = remote
            fr["decrypt"] = _try_decrypt(fhdecrypt, fr["hex"])
            ciphertexts.append(fr)

        # 密码文件顺手解析一下，作者能直接看到哈希类型
        if remote.startswith("/var/tel"):
            try:
                parsed = fhdecrypt.parse_passwd_file(content)
                entry["passwd_entries"] = {
                    u: {"hash_prefix": h[:3] if h.startswith("$") else "plain",
                        "hash_len": len(h)}
                    for u, h in parsed.items()
                }
            except Exception:  # noqa: BLE001
                pass

        files_meta.append(entry)
        log(f"  [ok] {remote}（{len(content)} 字节，{len(frags)} 段密文）")

    ok_count = sum(1 for e in files_meta if e.get("status") == "ok")
    if not ok_count:
        raise ReportError(
            "一个文件都没取到 —— 设备的 DownloadFile 接口可能被关掉或路径全变了。"
            "请把这份日志连同光猫型号/固件版本发给作者。"
        )

    # ---- 含解密密钥的 so（用户点名要的）----
    #
    # 放在免登录文件之后单独做，因为它需要 telnet：登不上就整段跳过，
    # 只记一条原因，不影响前面已经收到的诊断材料。
    so_meta, so_payloads = _collect_so_files(client, flash_mod, log=log)
    file_payloads.update(so_payloads)

    # ---- 设备信息 ----
    # 不能调 flash.collect_device_info()：那个函数要一个已登录的 telnet 会话
    # （靠 run_cmd 敲 shell 命令）。诊断包恰恰是在「登录不上」时才导出的，
    # 所以只能走免登录的 fh_tool RPC。
    device = {}
    try:
        device = dict(client.dev_info() or {})
    except Exception as e:  # noqa: BLE001
        device = {"_error": f"{type(e).__name__}: {e}"}
    # dev_info 在部分固件上不返回 MAC，用我们探到的补上
    device.setdefault("MacAddress", mac)

    # ---- 环境信息 ----
    model = getattr(flash_mod, "MODEL", None)
    env = {
        "tool_version": getattr(flash_mod, "VERSION", "unknown"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "router_ip": ip,
        "web_port": port,
        "model_key": getattr(model, "key", "") if model else "",
        "model_name": getattr(model, "name", "") if model else "",
        "model_family": getattr(flash_mod, "MODEL_FAMILY", ""),
    }

    # ---- 密钥表（作者核对用；只给前 16 字节，完整值在 fhdecrypt.py 里）----
    keys = {}
    for magic, (k2, iv2, note) in getattr(fhdecrypt, "CIPHER_KEYS", {}).items():
        keys[magic.hex()] = {
            "note": note,
            "k2_prefix": k2[:16],
            "k2_len": len(k2) // 2,
            "iv2_prefix": iv2[:8],
        }

    attempts = {
        "files": files_meta,
        "so_files": so_meta,
        "ciphertexts": ciphertexts,
        "summary": {
            "files_ok": ok_count,
            "files_total": len(TARGET_FILES),
            "so_ok": sum(1 for e in so_meta if e.get("status") == "ok"),
            "so_total": len(SO_FILES),
            "ciphertexts": len(ciphertexts),
            "decrypted": sum(1 for c in ciphertexts
                             if c.get("decrypt", {}).get("verdict") == "解密成功"),
            "unknown_magic": sum(1 for c in ciphertexts
                                 if not c.get("decrypt", {}).get("known_magic", True)),
        },
    }

    # ---- 写 zip ----
    readme = _readme_text(env, attempts, device)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", readme)
        zf.writestr("device.json", json.dumps(device, ensure_ascii=False, indent=2))
        zf.writestr("env.json", json.dumps(env, ensure_ascii=False, indent=2))
        zf.writestr("keys.json", json.dumps(keys, ensure_ascii=False, indent=2))
        zf.writestr("attempts.json", json.dumps(attempts, ensure_ascii=False, indent=2))
        for rel, content in file_payloads.items():
            zf.writestr(rel, content)

    # 清理临时目录（里面是下载回来的 tar，已经拆进 zip 了）
    try:
        for n in os.listdir(workdir):
            os.remove(os.path.join(workdir, n))
        os.rmdir(workdir)
    except OSError:
        pass

    log(f"[✓] 诊断包已生成：{out_path}")
    s = attempts["summary"]
    log(f"    文件 {s['files_ok']}/{s['files_total']} 个，密文 {s['ciphertexts']} 段，"
        f"其中解开 {s['decrypted']} 段，未知容器头 {s['unknown_magic']} 段")
    log(f"    解密 so {s['so_ok']}/{s['so_total']} 个"
        + ("" if s["so_ok"] else "（需 telnet 登录，登不上就没有）"))
    return out_path


def _dv(device, *names):
    """从 device 字典里按多个候选键名取值，大小写不敏感。

    为什么需要：`FhClient.dev_info()` 返回的是**全小写**的键
    （`serialnum` / `softwareversion` / `hardwareversion`），而贝尔那边
    `collect_device_info()` 用的是驼峰（`SerialNumber` / `SoftwareVersion`）。
    两套混着用，直接 `device.get("SerialNumber")` 在烽火上永远是 None，
    README 就会显示成「未知」。
    """
    low = {str(k).lower(): v for k, v in (device or {}).items()}
    for n in names:
        v = low.get(n.lower())
        if v:
            return str(v).strip()
    return ""


def _readme_text(env, attempts, device):
    s = attempts["summary"]
    lines = [
        "邪恶的光猫刷机工具箱 —— 烽火 telnet 密码诊断包",
        "=" * 52,
        "",
        f"生成时间：{env['collected_at']}",
        f"工具版本：{env['tool_version']}（Python {env['python']}，{env['platform']}）",
        f"机型：{env['model_name'] or env['model_key'] or '未知'}",
        f"设备地址：{env['router_ip']}:{env['web_port']}",
        f"设备 MAC：{_dv(device, 'MacAddress', 'mac') or '未知'}",
        f"设备 SN ：{_dv(device, 'SerialNumber', 'serialnum', 'sn') or '未知'}",
        f"固件版本：{_dv(device, 'SoftwareVersion', 'softwareversion') or '未知'}",
        f"硬件版本：{_dv(device, 'HardwareVersion', 'hardwareversion') or '未知'}",
        f"厂商/区域：{_dv(device, 'Manufacturer', 'operator_name') or '未知'}"
        f" / {_dv(device, 'AreaCode', 'area_code') or '未知'}",
        f"编译时间：{_dv(device, 'BuildTime', 'Buildtime') or '未知'}",
        "",
        "收集结果",
        "-" * 52,
        f"  文件        {s['files_ok']}/{s['files_total']} 个取到",
        f"  解密 so     {s['so_ok']}/{s['so_total']} 个取到"
        + ("" if s.get("so_ok") else "（需 telnet，登不上就没有）"),
        f"  密文片段    {s['ciphertexts']} 段",
        f"  解开        {s['decrypted']} 段",
        f"  未知容器头  {s['unknown_magic']} 段",
        "",
        "解出来的密码相关字段",
        "-" * 52,
    ]
    # 把「解开了、而且字段名像密码」的挑出来直接列在 README 顶部 ——
    # 作者打开 zip 第一眼就该看到这个，而不是去 attempts.json 里翻 24 段。
    hits = [
        c for c in attempts["ciphertexts"]
        if c.get("decrypt", {}).get("verdict") == "解密成功"
        and _field_priority(c.get("field")) < len(PRIORITY_FIELD_WORDS)
    ]
    if hits:
        for c in hits:
            plain = c["decrypt"]["results"][0].get("plain", "")
            lines.append(f"  {c.get('field') or '(无字段名)':<24} = {plain}"
                         f"    [{c['magic']} @ {c.get('from', '')}]")
    else:
        lines.append("  （没有解出密码相关字段 —— 见下面的三种结论）")
    lines += [
        "",
        "怎么读这份包",
        "-" * 52,
        "  files/       设备上原样导出的文件（已拆掉外层 tar）",
        "  so/          设备上的解密相关 .so（含工作密钥的那几个）",
        "               —— 作者换固件后用它重新抓 k2/iv2，见下",
        "  ciphertext/  没有单独目录 —— 密文片段都在 attempts.json 的",
        "               ciphertexts 里，带 offset / magic / hex 和解密结果",
        "  attempts.json 每个路径的下载结果 + 每段密文的解密判定 + so 清单",
        "  keys.json    本工具内置的密钥表（k2 只给前 16 字节）",
        "  env.json     工具/系统/机型信息",
        "  device.json  设备身份（跟备份清单同一套字段）",
        "",
        "so/ 是怎么来的（作者看这里）",
        "-" * 52,
        "  DownloadFile 只放行 /fhconf/ /fhdata/ /var/，/fhrom/lib/*.so 直接拒，",
        "  所以工具先用 telnet 受限 shell 把它们 cat 到 /var/fhrep_*，",
        "  再走免登录 DownloadFile 下载，下完立刻 rm 掉设备上的副本。",
        "  因此：telnet 登不上时 so/ 是空的（其余材料照常收集）。",
        "  复跑密钥抓取（在固件根目录下，用包里的 so 替换同名的）：",
        "    LD_PRELOAD=./hook.so qemu-aarch64 -L . ./dec 0 <hex>",
        "",
        "三种可能的结论（看 attempts.json 里的 verdict）",
        "-" * 52,
        '  "解密成功"                    → 密码就在明文里，工具候选表该更新',
        '  "所有已知密钥都解不出可打印明文" → 固件换了密钥，需要新的 k2/iv2',
        '  "容器头不在已知密钥表里"        → 换了加密方式或新的容器格式',
        "",
        "隐私提醒",
        "-" * 52,
        "这份包里有设备 MAC、PON SN、整机 SN、telnet 密码哈希，可能还有",
        "Wi-Fi 名称/密码等配置。只发给工具作者用于定位问题，不要公开贴到",
        "论坛或群里。分析完可以自己删掉。",
        "",
    ]
    return "\n".join(lines)
