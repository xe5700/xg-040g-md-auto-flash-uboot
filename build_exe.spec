# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 GUI 打成单个 Windows exe。

用法（在装了 pyinstaller 的 Windows Python 下）：
    pyinstaller --clean --noconfirm build_exe.spec

产物：dist/XG-040G-MD工具.exe（单文件、无控制台窗口）
      用户把 tcboot.bin 放到 exe 同目录即可刷机。

⚠️ 关键点：flash.py 是**运行时用 importlib 动态加载**的（gui.py 里
spec_from_file_location），PyInstaller 静态分析 gui.py 时看不到它内部的
import。所以 flash.py 里用到的标准库必须在这里手工列进 hiddenimports，
否则打包出来的 exe 一跑就报 ModuleNotFoundError（踩过：http.server，
备份用的临时 HTTP 服务器就依赖它）。
"""
import os

block_cipher = None

# flash.py 作为数据文件带进去（而不是编译进 exe）：这样用户可以直接替换
# exe 同目录的 flash.py 来改配置区，不用重新打包。
# config.py 是 gui.py 的 import 依赖，会被正常编译进 exe，不需要重复打包。
datas = [
    ("flash.py", "."),
]
# tcboot.bin 是可选的：存在就一起打包，作为「找不到外部文件」时的兜底
if os.path.isfile("tcboot.bin"):
    datas.append(("tcboot.bin", "."))
# icon.png 是窗口图标（tkinter 用）；exe 文件本身的图标用下面的 icon.ico。
# 两个都带上：ico 管资源管理器/快捷方式/任务栏，png 管运行时的窗口标题栏。
# 注意 ico 也要进 datas —— EXE(icon=...) 只把它写进 exe 的资源段，
# 那是给 Windows 看的，运行时 find_resource("icon.ico") 在解包目录里找不到它。
for _icon in ("icon.png", "icon.ico"):
    if os.path.isfile(_icon):
        datas.append((_icon, "."))

# flash.py 动态加载 → 它内部的 import 必须手工声明。
# 下面这些是 flash.py 实际用到的模块（stdlib 里 PyInstaller 不一定全收）。
FLASH_HIDDEN = [
    "telnetlib3",
    "telnetlib3.telnetlib",
    "http.server",          # 备份用的临时 HTTP 服务器（曾漏掉，exe 报错）
    "http.cookiejar",
    "urllib.request",
    "urllib.parse",
    "urllib.error",
    "posixpath",
    "base64",
    "hashlib",
    "subprocess",
    "threading",
    "socket",
    "json",
    "re",
    "time",
]

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=FLASH_HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 明显用不到的大件，砍掉能显著减小体积
        "numpy", "pandas", "matplotlib", "PIL", "scipy",
        "PyQt5", "PySide2", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="XG-040G-MD工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不要黑框
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # exe 文件自身的图标（资源管理器 / 快捷方式 / 任务栏用这个）
    icon="icon.ico" if os.path.isfile("icon.ico") else None,
)
