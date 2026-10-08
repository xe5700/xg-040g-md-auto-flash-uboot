#!/usr/bin/env python3
"""配置文件读写（INI 格式，仅用 Python 标准库 configparser）。

为什么单独一个模块：GUI 和命令行都从这里取账号密码，避免两处各写一份默认值。
用户可以在 GUI 里改账号密码，改动即时落到 config.ini。
"""
import configparser
import os

CONFIG_FILE = "config.ini"

# 默认值（与 flash.py 配置区保持一致，XG-040G-MD 移动版实测值）
DEFAULTS = {
    "device": {
        "router_ip": "192.168.1.1",
        "auto_enable_telnet": "true",
        "auto_enable_ftp": "true",
        "skip_backup_if_complete": "true",
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
    "safety": {
        # 刷机开关：默认关闭，必须在 GUI 里显式勾选才能解锁刷机按钮
        "allow_flash": "false",
        # 刷写后回读校验：写 mtdblock 后数据要过一会儿才真正进 flash，
        # 所以校验是「轮询等待」而不是读一次。这两个值可按需调大/调小。
        # 实测 512KB 的 U-Boot 几秒到几十秒内生效，120 秒余量充足。
        "flash_verify_timeout": "120",     # 最长轮询秒数
        "flash_verify_interval": "5",      # 每次回读间隔秒数
    },
}

_SECTION_COMMENTS = {
    "device": "设备与网络",
    "telnet": "telnet 登录与提权账号",
    "super": "网页超级管理员账号（用于自动开启 Telnet / FTP）",
    "safety": "安全开关",
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
    """把配置写进 flash 模块的全局变量。

    flash.py 的配置区是模块级常量，GUI 不复制它的逻辑，只覆盖这些变量，
    这样命令行和 GUI 走的是同一份实现。
    """
    flash.ROUTER_IP = cfg.get("device", "router_ip")
    flash.AUTO_ENABLE_TELNET = get_bool(cfg, "device", "auto_enable_telnet", True)
    flash.AUTO_ENABLE_FTP = get_bool(cfg, "device", "auto_enable_ftp", True)
    flash.SKIP_BACKUP_IF_COMPLETE = get_bool(
        cfg, "device", "skip_backup_if_complete", True)

    flash.USERNAME = cfg.get("telnet", "username")
    flash.PASSWORD = cfg.get("telnet", "password")
    flash.SU_USER = cfg.get("telnet", "su_user")
    root_pwd = cfg.get("telnet", "root_password").strip()
    # 留空即沿用 telnet 密码（本机型实测 su user_ftp 的密码就是 telnet 密码）
    flash.ROOT_PASSWORD = root_pwd if root_pwd else flash.PASSWORD

    flash.SUPER_USER = cfg.get("super", "username")
    flash.SUPER_PASSWORD = cfg.get("super", "password")

    # 回读校验的轮询参数（写 mtdblock 后数据生效有延迟，见 flash.verify_mtd_hash）
    try:
        flash.FLASH_VERIFY_TIMEOUT = int(cfg.get("safety", "flash_verify_timeout"))
        flash.FLASH_VERIFY_INTERVAL = int(cfg.get("safety", "flash_verify_interval"))
    except Exception:
        pass  # 配置里写了非数字就沿用 flash.py 的默认值
