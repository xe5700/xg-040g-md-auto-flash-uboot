#!/usr/bin/env python3
"""配置文件读写（INI 格式，仅用 Python 标准库 configparser）。

为什么单独一个模块：GUI 和命令行都从这里取账号密码，避免两处各写一份默认值。
用户可以在 GUI 里改账号密码，改动即时落到 config.ini。
"""
import configparser
import os
import sys

CONFIG_FILE = "config.ini"


def resource_dir():
    """可写的工作目录：exe 所在目录（或源码目录）。

    为什么 config.py 也要有一份：GUI、命令行、打包 exe 三种跑法必须读写
    **同一个** config.ini。程序会在运行时改这个文件（例如把顺延后的 HTTP
    端口写回去），如果有一边用了 CWD 相对路径，就会出现「GUI 里存的端口，
    命令行读不到」这种查不出来的怪事。所以统一走这里，别自己拼路径。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    """config.ini 的完整路径。"""
    return os.path.join(resource_dir(), CONFIG_FILE)

# 默认值（与 flash.py 配置区保持一致，XG-040G-MD 移动版实测值）
DEFAULTS = {
    "device": {
        # 机型 id（见 models.py）：bel = 贝尔移动版，hg5585f = 烽火电信版
        "model": "bel",
        "router_ip": "192.168.1.1",
        "auto_enable_telnet": "true",
        "auto_enable_ftp": "true",
        "skip_backup_if_complete": "true",
        # 光猫 MAC（烽火机型靠它推导 telnet / su 密码）
        "mac": "",
    },
    "telnet": {
        # telnet 登录账号（本机型实测为 user，密码在光猫底部铭牌上）
        "username": "user",
        "password": "j3ba73@8",
        # su 提权账号；user_ftp 只在 FTP 服务启用后才存在
        "su_user": "user_ftp",
        # 留空表示与 telnet 密码相同
        "root_password": "",
    },
    "super": {
        # 网页超级管理员（移动版恢复出厂默认）
        "username": "CMCCAdmin",
        "password": "aDm8H%MdA",
    },
    # 烽火 / AN7581 家族（界面上的第二个 tab）。
    # 为什么单独一段而不是复用 [device]/[telnet]/[super]：两个 tab 的账号密码
    # 完全不同（贝尔 CMCCAdmin/user_ftp，烽火 telecomadmin/root），放在同一组
    # 键里就会互相覆盖 —— 在烽火 tab 保存一次，贝尔 tab 的密码就没了。
    # 贝尔那套保持原键名不动，所以老 config.ini 不用迁移。
    "fh": {
        "router_ip": "192.168.1.1",
        # 光猫 MAC：烽火的 su 密码 = Fh@ + MAC 后 6 位，所以必须要
        "mac": "",
        "super_username": "telecomadmin",
        "super_password": "nE7jA%5m",
        # 手动指定的 telnet / su 密码。留空 = 用程序推导的默认值。
        #
        # 什么时候需要填：本工具内置的 AES 密钥只覆盖已知固件版本。如果某台
        # 光猫换了密钥表，本地解密就会失败 —— 但用户可能用别的工具（论坛脚本、
        # 厂商工具、自己逆向）解出了真实密码，那就填在这里，程序会**优先**用它。
        # 这两项永远不会被机型档案覆盖，填了就是最高优先级。
        "telnet_password": "",
        "su_password": "",
    },
    "serial": {
        # TTL 串口（拆机接线）：走这条路时不需要 Telnet / FTP / 超级密码，
        # 串口本身就是 root shell。端口一律由用户从列表里自己选，不自动探测。
        "enabled": "false",
        "port": "",            # 例如 /dev/ttyUSB0（Linux）或 COM3（Windows）
        "baudrate": "115200",
    },
    # TTL + XMODEM 救砖（第三个标签页）。
    # 为什么单独一段而不是复用 [serial]：这里解决的不是「登进去执行命令」，
    # 而是设备根本起不来、只能守在串口上的场景 —— 刷完 U-Boot 后 Winbond
    # 颗粒的机型 BL2 读不出 fip 卷，会一直循环打印
    #   Press x to load BL31 + U-Boot FIP via XMODEM
    # 这一页就是把 FIP 通过 XMODEM 送进内存，让 U-Boot 先起来去重建 UBI。
    "rescue": {
        # 要送进设备的 FIP（bl31 + u-boot）。留空 = 运行时自动找。
        "fip": "",
        # 等设备打印 "Press x" 的秒数。到点还没等到也照样发 x：设备可能早就
        # 停在提示符上了，只是不再重复打印（BL2 只打一次就等）。
        "wait": "30",
        # 传完后继续监听多少秒，用来判断 U-Boot 是否真的起来了。
        "after": "20",
        # 「只看日志」按钮听多少秒后自动停（GUI 里没法给后台线程发 Ctrl+C）。
        "monitor_seconds": "15",
    },
    "safety": {
        # 刷机开关：默认关闭，必须在 GUI 里显式勾选才能解锁刷机按钮
        "allow_flash": "false",
        # telnet 登录失败后自动重启光猫解除锁定（认证失败有阈值，触发后只能
        # 等或重启；重启 1~3 分钟比干等更可控）
        "auto_reboot_on_login_fail": "true",
        # 整个登录过程最多自动重启几次（防止死循环）
        "auto_reboot_max": "1",
        # 刷写后回读校验：写 mtdblock 后数据要过一会儿才真正进 flash，
        # 所以校验是「轮询等待」而不是读一次。这两个值可按需调大/调小。
        # 实测 512KB 的 U-Boot 几秒到几十秒内生效，120 秒余量充足。
        "flash_verify_timeout": "120",     # 最长轮询秒数
        "flash_verify_interval": "5",      # 每次回读间隔秒数
    },
    # 主机侧的临时服务。这些是「程序在自己机器上开的服务」，不是光猫的设置，
    # 单独一段以免和 [device]（光猫地址）混淆。
    "net": {
        # 临时 HTTP 服务器端口：程序把固件 / stock2ubi 送到光猫、或者接光猫
        # 推回来的分区备份，都靠这个服务器。
        #
        # 这一项会被程序自己改：默认 8080，若被别的软件占用（实测有人的 8080
        # 被中国电信「星辰智能体」常驻），程序会往后顺延到第一个空闲端口
        # （8081、8082…）并把结果写回这里，下次启动直接复用。
        # 所以用户只需要照提示去防火墙放行**一次**，之后一直有效。
        # 手动填一个固定端口也可以，填了就以它为首选。
        "http_port": "8080",
    },
}

_SECTION_COMMENTS = {
    "device": "设备与网络",
    "telnet": "telnet 登录与提权账号",
    "super": "网页超级管理员账号（用于自动开启 Telnet / FTP）",
    "fh": "烽火 / AN7581 家族（第二个标签页）",
    "serial": "TTL 串口（拆机接线）",
    "rescue": "TTL / XMODEM 救砖（第三个标签页）",
    "safety": "安全开关",
    "net": "主机侧临时服务（端口被占用时程序会自动顺延并改写这里）",
}

def _new_config():
    # interpolation=None 是必须的：超级密码 aDm8H%MdA 里的 %M 会被默认的
    # BasicInterpolation 当成占位符，直接抛
    #   ValueError: invalid interpolation syntax in 'aDm8H%MdA'
    # 我们的值全是字面量，不做任何插值。
    cfg = configparser.ConfigParser(interpolation=None)
    for section, values in DEFAULTS.items():
        cfg[section] = dict(values)
    return cfg


def load(path=CONFIG_FILE):
    """读取配置；文件不存在或缺项时用默认值补齐（不落盘，保存时才写）。"""
    cfg = _new_config()
    if os.path.isfile(path):
        try:
            cfg.read(path, encoding="utf-8")
        except Exception:
            # 文件损坏就退回默认值，不让 GUI 起不来
            cfg = _new_config()
    # 补齐缺失的项（老配置文件里没有新键时）
    for section, values in DEFAULTS.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
        for key, val in values.items():
            if not cfg.has_option(section, key):
                cfg.set(section, key, val)
    return cfg


def save(cfg, path=CONFIG_FILE):
    """写回配置文件，带中文注释（configparser 原生不支持注释，这里手工补）。"""
    lines = []
    for section in cfg.sections():
        comment = _SECTION_COMMENTS.get(section)
        if comment:
            lines.append(f"# {comment}")
        lines.append(f"[{section}]")
        for key, val in cfg.items(section):
            lines.append(f"{key} = {val}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def get_bool(cfg, section, key, fallback=False):
    try:
        return cfg.getboolean(section, key, fallback=fallback)
    except Exception:
        return fallback


def apply_to_flash(cfg, flash):
    """把 config.ini 里的 [device] / [telnet] / [super] 写进运行时状态。

    2026-10 重构后分两处写：
      - 光猫地址、telnet / 网页凭据 → `device.py` 的对象（`NET`/`TELNET`/`WEB`）；
      - 自动开关、回读校验、串口、自动重启、临时 HTTP 端口等**程序行为参数**
        → 仍然覆盖 flash 模块的常量（命令行以这些常量为准）。

    flash.py 的配置区是模块级常量，GUI 不复制它的逻辑，只覆盖这些变量，
    这样命令行和 GUI 走的是同一份实现。
    """
    import device as device_mod
    # ---- 程序行为参数（仍是 flash 模块常量）----
    flash.AUTO_ENABLE_TELNET = get_bool(cfg, "device", "auto_enable_telnet", True)
    flash.AUTO_ENABLE_FTP = get_bool(cfg, "device", "auto_enable_ftp", True)
    flash.SKIP_BACKUP_IF_COMPLETE = get_bool(
        cfg, "device", "skip_backup_if_complete", True)

    # ---- 机型 / 凭据（device.py）----
    # 只传非空的：configure_device 里空值一律忽略，落到机型档案的默认值。
    root_pwd = cfg.get("telnet", "root_password").strip()
    device_mod.configure_device(None, overrides={
        "router_ip": cfg.get("device", "router_ip", fallback="").strip(),
        "telnet_username": cfg.get("telnet", "username", fallback="").strip(),
        "telnet_password": cfg.get("telnet", "password", fallback="").strip(),
        "su_user": cfg.get("telnet", "su_user", fallback="").strip(),
        # 留空即沿用 telnet 密码（本机型实测 su user_ftp 的密码就是 telnet 密码）
        "su_password": root_pwd,
        "super_username": cfg.get("super", "username", fallback="").strip(),
        "super_password": cfg.get("super", "password", fallback="").strip(),
        "mac": cfg.get("device", "mac", fallback="").strip(),
    })
    # 贝尔 tab 没有「手动指定密码」框，这里清空，避免烽火 tab 填的值串过来
    # （两个 tab 共用同一份 device 状态，切 tab 后 _load_flash 会重新走这里）
    device_mod.TELNET.manual_password = ""
    device_mod.TELNET.manual_su_password = ""

    # 回读校验的轮询参数（写 mtdblock 后数据生效有延迟，见 flash.verify_mtd_hash）
    try:
        flash.FLASH_VERIFY_TIMEOUT = int(cfg.get("safety", "flash_verify_timeout"))
        flash.FLASH_VERIFY_INTERVAL = int(cfg.get("safety", "flash_verify_interval"))
    except Exception:
        pass  # 配置里写了非数字就沿用 flash.py 的默认值

    # TTL 串口：enabled 只影响 GUI 的默认勾选；命令行以 --serial-port 为准
    flash.SERIAL_PORT = cfg.get("serial", "port").strip()
    try:
        flash.SERIAL_BAUD = int(cfg.get("serial", "baudrate") or 115200)
    except Exception:
        pass  # 非数字就沿用 flash.py 的默认值

    # telnet 登录失败后自动重启（认证失败有阈值，触发后只能等或重启）
    flash.AUTO_REBOOT_ON_LOGIN_FAIL = get_bool(
        cfg, "safety", "auto_reboot_on_login_fail", True)
    try:
        flash.AUTO_REBOOT_MAX = max(0, int(cfg.get("safety", "auto_reboot_max")))
    except Exception:
        pass  # 非数字就沿用 flash.py 的默认值

    # 主机临时 HTTP 服务器端口。程序在端口被占用时会顺延并写回 config.ini，
    # 这里照读即可 —— 顺延过的值下次就是首选值。
    try:
        flash.LOCAL_HTTP_PORT = int(cfg.get("net", "http_port"))
    except Exception:
        pass  # 非数字就沿用 flash.py 的默认值


def apply_model(cfg, flash, model=None):
    """按机型把默认值填进 device 的机型档案（再用 config.ini 里的用户值覆盖）。

    为什么需要这一步：`config.ini` 里存的账号密码是**上次选中的机型**的。
    切换机型时如果直接沿用，就会拿贝尔的 `user`/`j3ba73@8` 去登烽火，
    白耗认证机会甚至触发锁定。所以切换机型时应该先用机型默认值打底。

    2026-10 重构：机型档案的归属地是 `device.py`（`configure_device()`），
    这里**不再往 flash 模块挂属性** —— 那些 `flash.MODEL_*` 赋值重构后是死代码
    （写上去也没人读，只会让人以为它还在生效）。`flash` 参数保留是为了不改调用方。
    model 为 None 时读 cfg 里的 [device] model。
    """
    import device as device_mod
    import models as models_mod
    m = model or models_mod.get(cfg.get("device", "model", fallback=""))
    # 只传非空的：configure_device 里空值一律忽略，所以 [device] 留空时
    # 会自然落到机型档案的默认值（地址/端口/账号）。
    device_mod.configure_device(m.key, overrides={
        "router_ip": cfg.get("device", "router_ip", fallback="").strip(),
        "mac": cfg.get("device", "mac", fallback="").strip(),
    })
    return m


def apply_fh_to_flash(cfg, flash):
    """把烽火 tab 的配置写进运行时状态（与贝尔那套完全独立）。

    与 apply_model/apply_to_flash 的区别：那两套按「机型档案」推默认值，
    适合贝尔那种「一个机型一套固定账号」；烽火这边用户可能同时装了多个
    AN7581 机型（HG5585F、HG5382A……），账号密码是用户填的，所以直接
    以 config.ini 的 [fh] 段为准，只在留空时才用机型档案兜底。

    2026-10 重构：机型/凭据都进 `device.py`，这里不再往 flash 模块挂属性。
    """
    import device as device_mod
    import models as models_mod
    m = models_mod.get("hg5585f")
    # 只传非空的：configure_device 里空值一律忽略，于是「选填」留空时
    # 仍然落到机型档案的推导值（FH-nE7jA%5m / Fh@+MAC 后 6 位）。
    device_mod.configure_device("hg5585f", overrides={
        "router_ip": cfg.get("fh", "router_ip", fallback="").strip(),
        "mac": cfg.get("fh", "mac", fallback="").strip(),
        # 用户手填的密码：优先级高于机型档案推导值（见 flash._password_candidates）。
        # 留空就是空串，不参与候选，所以不影响默认行为。
        "manual_password": cfg.get("fh", "telnet_password", fallback="").strip(),
        "manual_su_password": cfg.get("fh", "su_password", fallback="").strip(),
        "super_username": cfg.get("fh", "super_username", fallback="").strip(),
        "super_password": cfg.get("fh", "super_password", fallback="").strip(),
    })
    # telnet 密码是固定值 FH-nE7jA%5m（固件里解出来的），[fh] 里刻意**没有**
    # 「telnet 密码」这个键 —— 避免用户填错反而登录不上；要手填就走「选填」那条。
    # 烽火不刷 tcboot，备份/刷写合并成 stock2ubi 一步，这两个开关无所谓，
    # 但保持与贝尔一致，避免 run_flow 里读到旧值。
    flash.AUTO_ENABLE_TELNET = True
    flash.AUTO_ENABLE_FTP = False
    return m


def fh_defaults(model=None):
    """烽火 tab 的默认值（新建配置或点「恢复默认」时用）。"""
    import models as models_mod
    m = model or models_mod.get("hg5585f")
    return {
        "router_ip": m.router_ip,
        "mac": "",
        "super_username": m.super_user,
        "super_password": m.super_password,
        "telnet_password": "",
        "su_password": "",
    }


def model_defaults(model):
    """某机型的默认账号密码，供 GUI「切换机型」时填充表单。"""
    return {
        "device": {"router_ip": model.router_ip},
        "telnet": {
            "username": model.telnet_user,
            "password": model.telnet_password,
            "su_user": model.su_user,
            "root_password": model.su_password,
        },
        "super": {
            "username": model.super_user,
            "password": model.super_password,
        },
    }
