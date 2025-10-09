#!/usr/bin/env python3
import telnetlib3.telnetlib as telnetlib
import socket
import hashlib
import os
import sys
import time
import base64
import threading
import subprocess

# ================== 配置区 ==================
ROUTER_IP = "192.168.1.1"
USERNAME = "useradmin"
PASSWORD = "h54d*58g"
FTP_USER = "useradmin_ftp"  # 请根据光猫铭牌确认

UBOOT_FILE = "./tcboot.bin"
EXPECTED_SHA512 = "142ad1ebcc825e58223e5c28de0aee853d96a05447bda1af9fc78dd3f5eab0e10aec2626bd4b18ce7d8137b7f6943726734bcb38ce06d75ea86b45ccf6969610"  # ←←← 必须填写正确的 SHA512！

BACKUP_DIR = f"./backup_{time.strftime('%Y%m%d_%H%M%S')}"
LOCAL_NC_PORT = 8081
REMOTE_NC_PORT = 9999
TIMEOUT = 15
BACKUP_FIRMWARE = True
STATIC_LOCAL_IP = "192.168.1.1"
# ===========================================

MTD_PARTITIONS = [
    "bootloader", "romfile", "kernel", "rootfs", "kernel_slave", "rootfs_slave",
    "bosa", "ri", "flag", "flagback", "config", "data", "oopsfs", "log",
    "nsb_master", "nsb_slave", "all_flash"
]

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
    """等待直到出现提示符之一"""
    index, match, text = tn.expect(prompt_list, timeout=timeout)
    return text.decode('utf-8', errors='ignore')

def wait_for(tn, prompt, timeout=TIMEOUT):
    try:
        tn.read_until(prompt.encode(), timeout=timeout)
        return True
    except:
        print(f"[!] 等待 '{prompt}' 超时")
        return False

def main():
    # === 1. 验证 U-Boot 文件 ===
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

    # === 2. 准备环境 ===
    local_ip = get_local_ip(ROUTER_IP)
    # 如果你使用WSL2请使用本地IP地址，WSL内运行可能无法正确获得本地IP。
    # local_ip = STATIC_LOCAL_IP
    print(f"[*] 本机 IP: {local_ip}")

    # === 3. Telnet 登录 ===
    print(f"[*] 连接光猫 {ROUTER_IP}...")
    tn = telnetlib.Telnet(ROUTER_IP, timeout=10)

    # 普通用户登录
    wait_for(tn, "Login:")
    tn.write(USERNAME.encode() + b"\n")
    print("[✓] 帐号已输入")
    wait_for(tn, "Password:")
    tn.write(PASSWORD.encode() + b"\n")
    print("[✓] 密码已输入")
    wait_for(tn, "$")
    print("[✓] 登录成功")
    # 切换 FTP 用户
    tn.write(f"su {FTP_USER}\n".encode())
    wait_for(tn, "Password:")
    tn.write(PASSWORD.encode() + b"\n")
    wait_for(tn, "#")
    print("[✓] 切换 FTP 用户成功")

    # 获取 root
    tn.write(b"su\n")
    wait_for(tn, "#")
    # wait_for(tn, "Password:")
    # tn.write(PASSWORD.encode() + b"\n")
    # if not wait_for(tn, "#"):
    #     print("[!] 获取 root 权限失败")
    #     sys.exit(1)
    print("[✓] 已获取 root 权限")

    # 显示分区表
    tn.write(b"cat /proc/mtd\n")
    ret = read_until_prompt(tn, ["#".encode()],timeout=30)
    print(ret)
    if BACKUP_FIRMWARE:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        print(f"[*] 备份目录: {BACKUP_DIR}")
    # === 4. 备份所有分区 ===
        print("[*] 开始备份所有 MTD 分区（使用 dd + nc）...")
        for i, name in enumerate(MTD_PARTITIONS):
            filepath = os.path.join(BACKUP_DIR, f"mtd{i}_{name}.bin")
            print(f"  → 备份 mtd{i} ({name}) ...")
            # 使用 nc 监听
            ncP = subprocess.Popen(f"nc -l -p {LOCAL_NC_PORT} > {filepath}", shell=True)
            # start_nc_server(filepath, port)
            time.sleep(1)

            # 使用 dd 读取原始数据（更可靠）
            cmd = f"dd if=/dev/mtd{i} bs=1M | nc {local_ip} {LOCAL_NC_PORT}"
            tn.write(cmd.encode() + b"\n")
            time.sleep(1)
            ncP.wait()
            #等待读取完成
            if not wait_for(tn, "#"):
                print("[!] 读取分区失败")
                sys.exit(1)

    # print("[*] 等待备份完成（约60秒）...")
    # time.sleep(60)

    # === 5. 上传 U-Boot 并用 dd 刷写 ===
    print("[*] 上传 U-Boot 并使用 dd 写入 /dev/mtd0 ...")


    # 从nc写入到临时路径
    tn.write(f"nc -l -p {REMOTE_NC_PORT} > /tmp/uboot.bin\n".encode("utf-8"))
    
    # 使用nc上传uboot文件
    ncP = subprocess.Popen(f"nc {ROUTER_IP} {REMOTE_NC_PORT} -q 0 < {UBOOT_FILE}", shell=True)
    ncP.wait()
    print("[✓] U-Boot 文件已上传")
    wait_for(tn, "#")
    # 校验uboot文件
    print("[*] 校验 U-Boot 文件...")
    tn.write(f"sha512sum /tmp/uboot.bin\n".encode("utf-8"))
    # 验证uboot文件
    # ret = read_until_prompt(tn, ["#".encode()],timeout=30)
    ret = read_until_prompt(tn, ["#".encode()],timeout=30)
    lines = ret.split("\n")
    print(lines[1])
    if lines[1].startswith(EXPECTED_SHA512):
        print("[✓] U-Boot 校验通过")
    else:
        print("[!] U-Boot 校验失败")
        sys.exit(1)

    # === 关键：使用 dd 直接写入 /dev/mtd0 ===
    # print("    执行: dd if=/tmp/uboot.bin of=/dev/mtd0")
    tn.write(b"dd if=/tmp/uboot.bin of=/dev/mtdblock0\n")
    wait_for(tn, "#")
    # 等待写入完成（可能较慢）
    # 通过dd读取uboot分区，再次校验。
    print("[*] 读取 /dev/mtd0 并校验...")
    tn.write(f"dd if=/dev/mtd0 of=/tmp/flash_uboot.bin\n".encode("utf-8"))
    wait_for(tn, "#")
    tn.write(f"sha512sum /tmp/flash_uboot.bin \n".encode("utf-8"))
    ret = read_until_prompt(tn, ["#".encode()],timeout=30)
    lines = ret.split("\n")
    print(lines[1])
    if lines[1].startswith(EXPECTED_SHA512):
        print("[✓] U-Boot分区 U-Boot 校验通过")
    else:
        print("[!] U-Boot分区 U-Boot 校验失败! 请想办法手动还原回去，以免你的光猫变成砖。")
        sys.exit(1)
    
    time.sleep(10)
    # wait_for(tn, "#", timeout=30)

    # tn.write(b"echo '✅ dd 刷写完成！请立即断电重启！'\n")
    print("[✓] 刷写完成！")
    # wait_for(tn, "#")

    print("[*] 断开连接...")
    tn.close()
    print("\n[✓] 操作完成！")
    print("\n⚠️  重要提示：")
    print("   - 立即手动断电重启光猫！")
    print("   - 若设备无法启动，请使用 SPI 编程器恢复 mtd0！")
    print(f"   - 完整备份位于: {BACKUP_DIR}")

if __name__ == "__main__":
    main()