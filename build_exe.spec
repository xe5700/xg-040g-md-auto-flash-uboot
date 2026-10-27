# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 GUI 打成单个 Windows exe。

用法（在装了 pyinstaller 的 Windows Python 下）：
    pyinstaller --clean --noconfirm build_exe.spec

产物：dist/xe-pon-flash-toolkit.exe（单文件、无控制台窗口）
      用户把 tcboot.bin 放到 exe 同目录即可刷机。

⚠️ 关键点：flash.py 是**运行时用 importlib 动态加载**的（gui.py 里
spec_from_file_location），PyInstaller 静态分析 gui.py 时看不到它内部的
import。所以 flash.py 里用到的标准库必须在这里手工列进 hiddenimports，
否则打包出来的 exe 一跑就报 ModuleNotFoundError（踩过：http.server，
备份用的临时 HTTP 服务器就依赖它）。
"""
import os
import sys

block_cipher = None

# 版本号从 version.py 读（唯一来源），不要在别处再写一遍。
sys.path.insert(0, SPECPATH)
try:
    from version import VERSION, REPO, version_tuple
except Exception:
    VERSION, REPO = "0.0.0", ""
    def version_tuple():
        return (0, 0, 0)

_VT = version_tuple()

# 生成 Windows 版本资源文件：这样 exe 右键「属性 → 详细信息」里能看到版本号，
# 而不是一片空白。PyInstaller 的 version= 参数只接受文件路径，所以在这里现写。
_VERSION_INFO = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_VT + (0,)},
    prodvers={_VT + (0,)},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'xe5700'),
        StringStruct('FileDescription', '邪恶的光猫刷机工具箱'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', 'xe-pon-flash-toolkit'),
        StringStruct('LegalCopyright', 'AGPL-3.0-only'),
        StringStruct('OriginalFilename', 'xe-pon-flash-toolkit.exe'),
        StringStruct('ProductName', '邪恶的光猫刷机工具箱'),
        StringStruct('ProductVersion', '{VERSION}'),
        StringStruct('Comments', 'https://github.com/{REPO}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
_VERSION_FILE = os.path.join(SPECPATH, "_version_info.txt")
with open(_VERSION_FILE, "w", encoding="utf-8") as _f:
    _f.write(_VERSION_INFO)

# flash.py 作为数据文件带进去（而不是编译进 exe）：这样用户可以直接替换
# exe 同目录的 flash.py 来改配置区，不用重新打包。
# config.py 是 gui.py 的 import 依赖，会被正常编译进 exe，不需要重复打包。
# 这几个都是**运行时才 import** 的本地模块（gui.py 用 importlib 加载
# flash.py，flash.py 再按机型/接入方式延迟 import fhtool / serial_shell），
# PyInstaller 静态分析看不到它们，所以必须显式当数据文件带进去。
# 好处是用户也能直接替换 exe 同目录的那一份。
datas = [
    ("flash.py", "."),
    ("models.py", "."),
    ("fhtool.py", "."),
    ("serial_shell.py", "."),
    # fhfactory.py 是 flash.py 在烽火备份后动态 import 的（生成 1 MiB factory），
    # 静态分析看不到，必须作为数据文件一起打包。
    ("fhfactory.py", "."),
    # fhdecrypt.py 同理：只有 telnet 密码被拒时才动态 import（解 param.pdt.enc）。
    # 它是「密码找回」兜底通道的核心，漏打就等于这条功能在 exe 里不存在。
    ("fhdecrypt.py", "."),
    # fhreport.py 是「导出诊断包」用的：把设备上密码相关的文件和密文打包成 zip
    # 交给作者分析。同样是按钮回调里动态 import，静态分析看不到。
    ("fhreport.py", "."),
    # ⚠️ ttl_rescue.py 是 TTL / XMODEM 救砖（第三个标签页）。gui.py 用
    # spec_from_file_location **从磁盘**加载它，不是 import —— 所以它必须作为
    # 数据文件打进来；只写进 hiddenimports 是没用的（模块会被编译进 exe 的
    # PYZ 归档里，磁盘上没有这个 .py，find_resource 找不到）。
    # 踩过：v2.4.0 的 exe 漏了这一行，用户点救砖报「找不到 ttl_rescue.py」。
    ("ttl_rescue.py", "."),
    # nandcheck.py 是 flash.py 里 `import nandcheck` 判断 preloader 的 ECC 与
    # 设备颗粒是否匹配。漏了它不会崩（flash.py 有 try/except 兜底），但华邦
    # 机型那道闸会**静默失效** —— 这比崩了更糟，所以这里必须带上。
    ("nandcheck.py", "."),
    # device.py 是机型/凭据/端口这些「每台机器不一样的量」的归属地（2026-10 重构：
    # 以前是 flash.py 里一堆模块级 global，GUI 走 run_flow() 时没人灌 → 贝尔机型
    # 拿着空账号去逐个试）。flash.py 和 gui.py 都 import 它，静态分析能看到，
    # 但 flash.py 本身也是 datas 动态加载的，所以这里照样列一条。
    ("device.py", "."),
]
# tcboot.bin 是可选的：存在就一起打包，作为「找不到外部文件」时的兜底
if os.path.isfile("tcboot.bin"):
    datas.append(("tcboot.bin", "."))
# an758x-stock2ubi 同理：存在就打进去，用户也可以直接替换 exe 同目录的那份。
# 它是 aarch64 静态 ELF，不是本机可执行文件，只是要原样传到光猫 /tmp 去跑，
# 所以按 datas 处理（普通数据文件），不需要 binaries。
if os.path.isfile("an758x-stock2ubi"):
    datas.append(("an758x-stock2ubi", "."))
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
    # 烽火机型的后端：flash.py 里按需 import fhtool，fhtool 再 import
    # fh_tool_cli 的库函数。整条链都是延迟导入，静态分析看不到，必须显式声明，
    # 否则选烽火机型时 exe 会报 ModuleNotFoundError。
    "fhtool",
    "fh_tool_cli",
    "fh_tool_cli.backends",
    "fh_tool_cli.backends.fh_tool",
    "fh_tool_cli.backends.telnet",
    "fh_tool_cli.credentials",
    # fh_tool_cli 的传递依赖：实测 import fh_tool_cli.backends.fh_tool
    # 会连带加载这些（crypto 用 cryptography，RPC 用 requests，日志用 rich）
    "cryptography",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.primitives.padding",
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "chardet",
    "attr",
    "rich",
    "rich.console",
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
    # TTL 串口：pyserial 是唯一一个「只在用 TTL 时才需要」的第三方依赖，
    # 同样是延迟 import 的，静态分析收不到。
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    # 延迟 import 的本地模块
    "models",
    "serial_shell",
    "fhtool",
    # TTL / XMODEM 救砖：flash.py --mode ttl 里 import ttl_rescue；
    # nandcheck 是 check_nand_ecc_compat 里 import 的。两个都是延迟导入，
    # 静态分析看不到。（ttl_rescue.py / nandcheck.py 同时也进了上面的 datas：
    # 救砖 tab 是直接从磁盘加载 ttl_rescue.py 的，光靠这里编译进去不够。）
    "ttl_rescue",
    "nandcheck",
    # 机型/凭据状态（flash.py 与 gui.py 都 import 它）
    "device",
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
    name="xe-pon-flash-toolkit",
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
    # Windows 版本资源（右键属性→详细信息），由上面的 _VERSION_FILE 生成
    version=_VERSION_FILE,
)
