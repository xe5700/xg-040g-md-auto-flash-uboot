#!/usr/bin/env python3
"""邪恶的光猫刷机工具箱 · 图形界面

只用 Python 标准库 tkinter，不需要额外安装依赖（Windows 版 exe 也是这套）。

界面分区：
  1. 账号密码设置（存 config.ini，可保存/重载）
  2. 功能按钮：开 Telnet / 开 FTP / 备份 / 重启
  3. 刷机（默认锁定，需先勾选"启用刷机功能"解锁；点击后弹出多重确认）
  4. 运行日志
"""
import os
import queue
import re
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox, scrolledtext, filedialog

import config as cfgmod
import models as models_mod
import serial_shell

try:
    from version import VERSION
except Exception:  # 打包漏了 version.py 时不能让 GUI 起不来
    VERSION = "0.0.0"

APP_TITLE = "邪恶的光猫刷机工具箱"
APP_TITLE_FULL = f"{APP_TITLE} v{VERSION}"

# 项目主页。界面上必须把它摆出来：这工具是免费开源的，咸鱼上那些「倒卖」
# 的二道贩子卖的就是同一个东西，用户点两下就能自己去下。
PROJECT_REPO = "xe5700/xg-040g-md-auto-flash-uboot"
PROJECT_URL = "https://github.com/" + PROJECT_REPO
PROJECT_HOME_TEXT = "本项目完全免费、开源："
PROJECT_STAR_HINT = "（点开链接，顺手进去点个 ⭐ Star 就是对作者最大的支持）"
PROJECT_SCALPER_NOTE = (
    "闲鱼上「倒卖」这个工具的都是二道贩子：工具本身免费、代码全部开源，"
    "在项目主页就能直接下载，不用花冤枉钱。（遇到问题也请以项目主页的说明为准，"
    "二手卖家给的版本没人维护。）")


def _resource_dir():
    """可写的工作目录：exe 所在目录（或源码目录）。

    config.ini、backup_* 都放这里，因为打包后 _MEIPASS 是只读临时目录。

    实现直接复用 config.resource_dir()：flash.py 也会在运行中改写
    config.ini（把顺延后的 HTTP 端口写回去），两边必须是同一个文件，
    各写一份相同的判断迟早会漂移。
    """
    return cfgmod.resource_dir()


def _bundle_dir():
    """PyInstaller 解包出的只读资源目录；非打包时即源码目录。"""
    return getattr(sys, "_MEIPASS", _resource_dir())


def find_resource(name):
    """按「工作目录 → 打包内资源」的顺序找文件。

    先看 exe 同目录，这样用户可以直接替换 flash.py 或换一个 tcboot.bin，
    不用重新打包；找不到再退回 exe 内自带的副本。
    """
    for d in (_resource_dir(), _bundle_dir()):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


# ===== 华邦（Winbond）闪存专项说明 =====
# 这段文案要在机型 tab、救砖 tab 里重复出现，所以只写一份，避免改一处漏两处。
#
# v2.4.0 起结论变了，别再按旧说法走：
#   旧：临时引导只兼容华邦颗粒，非华邦机型刷了会砖 —— 所以写了「非华邦千万别刷」。
#   新：临时引导已适配所有闪存颗粒，照刷不会坏；而它同时还是把华邦机型从
#       「原厂 8bit 写 / 新 BL2 4bit 读」那个死局里拉出来的办法。
#   于是 HG5585F / HG5382A 的建议统一成「先刷修改版临时引导，进系统后立刻
#   换刷官方版」—— 分两步走，能把软砖概率压到最低。
# 临时引导和官方引导都由 uboot_release.py 从上游下好、随 zip 一起发，
# 解压后在 exe 同级的这两个目录里：
UBOOT_MODIFIED_DIRNAME = "修改版"
UBOOT_OFFICIAL_DIRNAME = "官方版"
UBOOT_MODIFIED_DIR = "uboot-bin/" + UBOOT_MODIFIED_DIRNAME
UBOOT_OFFICIAL_DIR = "uboot-bin/" + UBOOT_OFFICIAL_DIRNAME

# 烽火 tab 就是 HG5585F 电信版，所以这两个文件名用 ct 那份；
# cu / HG5382A 只是机型段不同，目录里的文件名一一对应。
WINBOND_PRELOADER_TMP = \
    "an7581-fiberhome-hg5585f-ct-preloader-tmp-8bit-read-4bit-write.bin"
WINBOND_UBOOT_TMP = \
    "an7581-fiberhome-hg5585f-ct-bl31-u-boot-tmp-8bit-read-4bit-write.fip"

# 刷机页（机型 tab）用的完整版
WINBOND_NOTE_FULL = (
    "⚠️ HG5585F / HG5382A：先刷「修改版」临时引导，进系统后换刷「官方版」\n"
    "\n"
    "华邦 W29N02KVSIAF 这颗料，原厂 tcboot/BL2 与原厂内核都按 8 bit/512B 的\n"
    "BCH 强度写 NAND；官方 preloader 从 v2026.07-3 起把它的 soc_ecc_ability\n"
    "改成了 4 bit/512B。BL2 用 4bit 去读原厂按 8bit 写进去的 fip 卷会直接失败，\n"
    "串口上一直循环这两行：\n"
    "    ERROR: Stored BL31 + U-Boot FIP failed (-2)\n"
    "    Press x to load BL31 + U-Boot FIP via XMODEM\n"
    "\n"
    "所以这两款机型请分两步刷，别想一次刷到位：\n"
    "  第 1 步：先刷本项目改过的临时引导（8bit 读 / 4bit 写），\n"
    "          它能把机器从上面那个死局里拉出来：\n"
    "              " + UBOOT_MODIFIED_DIR + "/\n"
    "  第 2 步：进系统之后，立刻进恢复页换刷官方引导长期使用：\n"
    "              " + UBOOT_OFFICIAL_DIR + "/\n"
    "先临时、后官方，能把「刷完进不去系统、只能 TTL 救砖」的概率压到最低。\n"
    "\n"
    "临时引导现在已经适配了所有闪存颗粒（东芝 / 镁光等机型刷它也不会坏），\n"
    "所以不用再纠结自己的机器是不是华邦的料 —— 按机型选对文件就行。\n"
    "但它只是过渡：刷完一定要换上官方引导，别长期留在临时版上。"
)

# 救砖 tab 用的版本。
# 注意：**这里一定不要推荐修改版（临时引导）**。
# 用户砖到这个界面，多半就是给华邦颗粒的机器刷了官方 U-Boot：原厂固件按
# 8bit 写 NAND、官方 BL2 按 4bit 读，读不出原厂写进去的 fip 卷。
# 救砖要做的是把机器拉回标准状态 —— 送官方引导 + 重建 UBI。XMODEM 是把 FIP
# 直接送进内存、不经过 NAND 读取，所以官方版在这一步照样起得来；把修改版塞进
# 救砖流程只会让用户在两种引导之间来回横跳。
WINBOND_NOTE_SHORT = (
    "⚠️ HG5585F / HG5382A：这里送「官方版」的引导，不要用修改版\n"
    "会砖到需要救砖，多半是因为给华邦（Winbond）颗粒的机器刷了官方 U-Boot：\n"
    "原厂固件按 8 bit/512B 写 NAND，官方 BL2 从 v2026.07-3 起按 4 bit/512B 读，\n"
    "读不出原厂写进去的 fip 卷，于是串口一直循环：\n"
    "    ERROR: Stored BL31 + U-Boot FIP failed (-2)\n"
    "    Press x to load BL31 + U-Boot FIP via XMODEM\n"
    "\n"
    "救砖直接用官方引导：\n"
    "    " + UBOOT_OFFICIAL_DIR + "/\n"
    "XMODEM 是把 FIP 直接送进内存、不经过 NAND，所以这一步官方版一定跑得起来。\n"
    "跑起来之后按下面第 ④ 块进恢复页重建 UBI 并重刷，机器就回到标准状态了。\n"
    "（「修改版」的临时引导是刷机流程里用的，见「烽火 HG5585F 电信版」tab。）"
)

# 贝尔 tab 用的版本：贝尔这几款不在华邦死局的确认名单里，直接刷官方引导即可，
# 所以这里不再是警告，只是告诉用户「出问题了去哪找」。
WINBOND_NOTE_BEL = (
    "这两款机型的引导直接刷「官方版」就行：\n"
    "    " + UBOOT_OFFICIAL_DIR + "/\n"
    "贝尔 XG-040G-MD / XG-140G-MD / XG-040G-MF / XG-040G-TF 都不在上面的\n"
    "华邦死局确认名单里（名单是烽火的 HG5585F / HG5382A）。\n"
    "万一串口上出现 Stored BL31 + U-Boot FIP failed (-2) 这个循环，说明这台\n"
    "机器的颗粒也是「原厂 8bit 写、新 BL2 4bit 读」，就照着「烽火 HG5585F\n"
    "电信版」tab 里红框那段走「先临时、后官方」；本工具的「TTL / XMODEM 救砖」\n"
    "tab 也能把临时引导直接送进机器。"
)

# 救砖 tab：解释为什么「刷 preloader / firstblock」那两个按钮平时用不到
NO_SHORT_NOTE = (
    "ℹ️ 这两个按钮现在用不上，也不用为此去短接闪存引脚\n"
    "它们对应「从 TTL 直接写 preloader / firstblock」，需要先短接闪存引脚让 SoC\n"
    "停在 BootROM，本工具还没实现。\n"
    "\n"
    "但绝大多数情况下你并不需要它们：单独刷 preloader 一般不会把机器刷坏，\n"
    "只是紧接着那次 bl31/u-boot 的写入会失败。换句话说机器里通常已经有\n"
    "preloader 了 —— 这种情况下不用短接、也不用进 preloader 模式，\n"
    "直接用上面的「刷入 U-Boot」把 bl31 + u-boot.fip 送进去就行。"
)


def _is_modified_uboot(path):
    """这个 FIP / preloader 是不是本项目改过的「修改版」（8bit 读 / 4bit 写）？

    文件名和目录名双重判断：uboot_release.py 会把它们放进 uboot-bin/修改版/，
    文件名里也带 -tmp-8bit-read-4bit-write。用户手动改名或挪位置时，任意一条
    命中就算修改版 —— 救砖那边宁可保守一点，把话说死比让用户猜强。
    """
    base = os.path.basename(path).lower()
    if "-tmp-8bit-read-4bit-write" in base:
        return True
    parts = [q.lower() for q in os.path.normpath(path).split(os.sep)]
    return UBOOT_MODIFIED_DIRNAME.lower() in parts


# 传完 FIP、U-Boot 跑起来之后给用户的下一步。界面上第④块和成功后的弹窗
# 共用这一份，免得两处说法不一致。
# 关键一点：这时设备**已经在 U-Boot 里**了，不用重启就能直接往下走。
RESCUE_OK_NEXT = (
    "U-Boot 已经跑起来了 —— 设备就在 U-Boot 里等着你，不用重启。\n\n"
    "  1. 直接在当前串口按 Ctrl+C 打断，敲 httpd dhcp 进 WebUI；\n"
    "     （或者按住 reset 进恢复模式，两条路等效）\n"
    "  2. 浏览器打开 http://192.168.0.1/，按页面流程重建 UBI 并重刷；\n"
    "     （HG5585F / HG5382A：重建 UBI 会把引导一并按新方式写回去，\n"
    "       重刷完这台机器就回到标准状态了）\n"
    "  3. 验收标准：拔掉 TTL 线，断电重启三次，次次能自己起来。\n\n"
    "做不到第 3 步，说明 bootstrap 那一层仍是原厂内核按 8bit 写进去的，"
    "4bit 的 BL2 读不出来 —— 见 NAND-ECC-问题报告.md 第六节。")


# ---------- 运行日志的降噪规则 ----------
# 刷错 ubi 分区 / uboot 之后，设备会哗哗地刷同一条报错，只有 PEB 号或地址不同，
# 例如：
#   ubi0 error: ubi_io_read: error -74 (ECC error) while reading 64 bytes from PEB 1690:0, ...
# 这类行折成一条并在行尾标次数，否则 Text 控件会被几百次重绘拖死。
# 把地址和 4 位以上的数字抹掉，「同一个错、不同块号」就算同一行。
_LOG_ADDR = re.compile(r"0x[0-9a-fA-F]+|\b\d{4,}\b")

# (名字, 命中规则, 给用户的一句话)。每种只提示一次，免得跟着刷屏一起重复。
_LOG_HINTS = (
    ("ubi",
     re.compile(r"ubi0? error|ubi_io_read|UBI error|No volume named fip|ubi_attach",
                re.I),
     "[!] 你目前 ubi 分区是错误的，需要重建为正确的 ubi 分区。"),
    ("uboot",
     re.compile(r"Stored BL31 \+ U-Boot FIP failed|Failed to load image id|"
                r"Press x to load BL31|nand_read.*fail|failed with -1", re.I),
     "[!] 你目前刷入了错误的 uboot，需要救砖刷入正确的对应 uboot。"),
)


class LogRedirector:
    """把 print 输出接进 GUI 日志框。

    flash.py 全程用 print 汇报进度，GUI 不去改动它，只在运行时把 stdout
    换成本对象，这样命令行和 GUI 共用同一份实现。
    """

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


def _set_window_icon(root):
    """给窗口设置标题栏/任务栏图标。

    优先用 icon.png（tkinter 的 iconphoto 支持 PNG，跨平台且不需要 Pillow）；
    Windows 下再补一次 iconbitmap(icon.ico)，因为部分环境下任务栏只认 .ico。
    两者都找不到就静默跳过 —— 图标只是外观，不能因此让程序起不来。
    """
    png = find_resource("icon.png")
    if png:
        try:
            # 用 PhotoImage 保持引用：不保存会被 GC 掉，图标随即消失
            root._icon_img = tk.PhotoImage(file=png)
            root.iconphoto(True, root._icon_img)
        except Exception:
            pass
    ico = find_resource("icon.ico")
    if ico:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE_FULL)
        self.root.geometry("1040x900")
        self.root.minsize(960, 640)
        _set_window_icon(self.root)

        self.log_queue = queue.Queue()
        # 日志降噪用的状态：上一次的「特征行」、已重复几次、那条的行号
        self._log_buf = ""          # 没写完的半行，等下一批补齐
        self._log_key_last = None
        self._log_last = ""
        self._log_repeat = 0
        self._log_idx = 0           # 上面那条在 Text 里的行号，重复时原地改它
        self._log_dirty = False
        self._log_hinted = set()    # 已经提示过的报错种类
        self.worker = None
        self.flash = None          # 延迟导入 flash，避免启动时就做网络探测
        self.rescue = None         # 同理，ttl_rescue 也是点按钮时才导入

        self.cfg = cfgmod.load(os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        self.vars = {}
        self.hint_labels = {}
        self.model = models_mod.get(self.cfg.get("device", "model"))
        self._build_ui()
        self._load_into_ui()
        self._pump_log()

    # ---------- 界面 ----------
    def _make_scroll_tab(self, parent):
        """在一个 tab 里做可滚动区域，返回内容 Frame。

        两个 tab 的内容高度不同（贝尔有刷机区、烽火没有），各自滚动比
        整窗一个滚动条更好用：切 tab 后不会因为另一个 tab 更高而多出空白。
        """
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas, padding=10)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas(e):
            canvas.itemconfigure(win, width=e.width)

        inner.bind("<Configure>", _on_inner)
        canvas.bind("<Configure>", _on_canvas)

        def _on_wheel(e):
            # 鼠标停在日志框/下拉框上时交给控件自己处理，不然两个一起动很难用
            if isinstance(e.widget, (tk.Text, tk.Listbox, ttk.Combobox)):
                return
            if getattr(e, "num", None) == 4:
                step = -1
            elif getattr(e, "num", None) == 5:
                step = 1
            else:
                step = -1 if getattr(e, "delta", 0) > 0 else 1
            canvas.yview_scroll(step, "units")

        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.bind_all(_seq, _on_wheel)
        return inner

    def _tip_bar(self, parent, text):
        """顶部常驻黄色提示条（恢复出厂设置的前提说明）。

        这是使用前提，不跟任何按钮绑定（用户明确要求过）。
        """
        tip = tk.Frame(parent, bg="#fff8e1", highlightbackground="#e0c060",
                       highlightthickness=1)
        tip.pack(fill="x", pady=(0, 8))
        lbl = tk.Label(tip, bg="#fff8e1", fg="#8a6d00", justify="left",
                       anchor="w", wraplength=820, text=text)
        lbl.pack(fill="x", padx=8, pady=6)
        return lbl

    # 提示框配色：danger=必砖红线 / warn=要注意 / info=说明 / ok=推荐做法
    _NOTE_STYLES = {
        "danger": ("#fdecea", "#d93025", "#a50e0e"),
        "warn": ("#fff8e1", "#e0c060", "#8a6d00"),
        "info": ("#eef4fb", "#7aa7d9", "#1b4a7a"),
        "ok": ("#e8f5e9", "#7cc47f", "#1e5b23"),
    }

    def _note_box(self, parent, text, kind="warn", bold=True, pady=(6, 0)):
        """带底色的说明块。

        跟 _tip_bar 的区别：_tip_bar 是整页顶部常驻的「使用前提」，一个 tab 只
        该有一条；这个是可叠加的段落级提示，一个 tab 里能放好几块。
        """
        bg, border, fg = self._NOTE_STYLES[kind]
        box = tk.Frame(parent, bg=bg, highlightbackground=border,
                       highlightthickness=1)
        box.pack(fill="x", pady=pady)
        tk.Label(box, bg=bg, fg=fg, justify="left", anchor="w",
                 wraplength=920, font=("", 10, "bold") if bold else ("", 10),
                 text=text).pack(anchor="w", padx=8, pady=6)
        return box

    def _build_ui(self):
        # 串口变量必须**先**建：第三个 tab（TTL 救砖）的控件要拿它们当
        # textvariable，放到方法末尾会 AttributeError。
        self.serial_on = tk.BooleanVar(value=False)
        self.serial_port = tk.StringVar(value="")
        self.serial_baud = tk.StringVar(value="115200")

        # 顶层：上面是 Notebook（两个机型 tab + 一个救砖 tab），下面是共用日志框。
        # 为什么按机型分 tab 而不是一个表单切下拉框：两个厂商的流程差别太大
        # —— 贝尔要开 FTP 才能 su、要刷 tcboot.bin；烽火不用 FTP、刷写走
        # stock2ubi 网页且和备份合并成一步。放在一张表单里会有一半控件是
        # 灰的，用户很难看懂「我现在到底该点哪个」。
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.notebook = notebook

        tab_bel = ttk.Frame(notebook)
        tab_fh = ttk.Frame(notebook)
        tab_ttl = ttk.Frame(notebook)
        notebook.add(tab_bel, text="  贝尔 XG-040G-MD / XG-140G-MD（移动版）  ")
        notebook.add(tab_fh, text="  烽火 HG5585F 电信版  ")
        # 救砖 tab 独立成一页、而不是塞进两个机型 tab 里：
        #   · 它和机型无关 —— 贝尔/烽火都是 AN7581，BL2 停在 "Press x" 的样子
        #     一模一样，救砖流程没有任何区别；
        #   · 正常情况下根本用不到它，放进机型 tab 会让只想刷机的用户看到
        #     一堆「砖了才有用」的按钮。
        notebook.add(tab_ttl, text="  TTL / XMODEM 救砖  ")
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_change)

        self._build_bel_tab(self._make_scroll_tab(tab_bel))
        self._build_fh_tab(self._make_scroll_tab(tab_fh))
        self._build_ttl_tab(self._make_scroll_tab(tab_ttl))

        # ===== 共用底部：配置按钮 + 日志 =====
        # bottom 不设 expand：否则它会和 Notebook 平分高度，把 tab 内容挤到
        # 只剩半屏（两个 tab 的表单都比日志重要）。日志框给固定行数即可。
        bottom = ttk.Frame(self.root, padding=(8, 6))
        bottom.pack(fill="x", expand=False, pady=(0, 8))

        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        ttk.Button(bar, text="保存配置", command=self.on_save).pack(side="left")
        ttk.Button(bar, text="重新载入", command=self.on_reload).pack(
            side="left", padx=6)
        ttk.Button(bar, text="恢复默认", command=self.on_reset).pack(side="left")

        # ===== 共用：项目主页 / Star 提示 =====
        # 放共用区而不是塞进某个 tab 里：三个 tab 都该看到，而且这是「关于」
        # 信息，不该跟着机型的表单一起滚走。
        home = ttk.Frame(bottom)
        home.pack(fill="x", pady=(8, 0))
        row1 = ttk.Frame(home)
        row1.pack(fill="x")
        ttk.Label(row1, text=PROJECT_HOME_TEXT,
                  foreground="#666").pack(side="left")
        self._link_label(row1, PROJECT_REPO, PROJECT_URL).pack(side="left")
        ttk.Label(row1, text=PROJECT_STAR_HINT,
                  foreground="#666").pack(side="left")
        # ⚠️ 单独一个 Label：Tk 会把 emoji 的宽度估得很大，跟正文放在同一个
        # Label 里时它自己占一行、正文从下一行开始，很难看。
        note_row = ttk.Frame(home)
        note_row.pack(fill="x", pady=(2, 0))
        tk.Label(note_row, text="⚠️", fg="#a50e0e",
                 font=("", 9, "bold")).pack(side="left", anchor="n")
        tk.Label(note_row, text=PROJECT_SCALPER_NOTE, fg="#a50e0e",
                 font=("", 9, "bold"), anchor="w", justify="left",
                 wraplength=950).pack(side="left", fill="x")

        logbox = ttk.LabelFrame(bottom, text="运行日志", padding=6)
        logbox.pack(fill="x", pady=(6, 0))
        self.log = scrolledtext.ScrolledText(
            logbox, height=8, wrap="word", state="disabled",
            font=("Consolas", 9))
        # 报错提示要显眼，不然会淹没在被折叠的刷屏里
        self.log.tag_configure("hint", foreground="#c62828",
                               font=("Consolas", 9, "bold"))
        self.log.pack(fill="both", expand=True)

    # ---------- 贝尔 tab ----------
    def _build_bel_tab(self, outer):
        self.tip_label = self._tip_bar(
            outer,
            "提示：请先恢复光猫出厂设置\n"
            "按住光猫 reset 键 20 秒以上，让超级管理员密码回到默认值"
            "（CMCCAdmin / aDm8H%MdA），程序才能自动登录网页开启 Telnet 与 FTP。")

        acct = ttk.LabelFrame(outer, text="账号密码（保存在 config.ini 的 [device] / [telnet] / [super]）",
                              padding=8)
        acct.pack(fill="x")

        rows = [
            ("device", "router_ip", "光猫地址", False,
             "光猫的管理地址，默认 192.168.1.1"),
            ("telnet", "username", "铭牌用户账号", False,
             "光猫底部铭牌上的用户账号（本机型为 user）"),
            ("telnet", "password", "铭牌用户密码", False,
             "光猫底部铭牌上的用户密码；telnet 登录用它"),
            ("telnet", "su_user", "提权账号（su）", False,
             "自动填成「铭牌用户账号_ftp」，即拿到 root 的账号"),
            ("telnet", "root_password", "提权密码（留空=同上）", False,
             "留空表示与「铭牌用户密码」相同"),
            ("super", "username", "网页超级账号", False,
             "登录管理网页用的超级账号"),
            ("super", "password", "网页超级密码", False,
             "恢复出厂后的默认超级密码，用于自动开启 Telnet / FTP"),
        ]
        self._build_account_rows(acct, rows)

        # ===== 功能 =====
        ops = ttk.LabelFrame(outer, text="操作（默认只读，不写入任何分区）", padding=8)
        ops.pack(fill="x", pady=(8, 0))

        row1 = ttk.Frame(ops)
        row1.pack(fill="x")
        self.btn_telnet = ttk.Button(
            row1, text="① 开启 Telnet", width=16,
            command=lambda: self.run_mode("telnet"))
        self.btn_telnet.pack(side="left")
        # telnet 和 FTP 是两个独立开关，所以给两个按钮：
        # 用户自己开过 telnet、没开 FTP 时，只点这个即可。
        self.btn_ftp = ttk.Button(
            row1, text="② 开启 FTP", width=14,
            command=lambda: self.run_mode("ftp"))
        self.btn_ftp.pack(side="left", padx=6)
        self.btn_backup = ttk.Button(
            row1, text="③ 备份全部分区", width=16,
            command=lambda: self.run_mode("backup"))
        self.btn_backup.pack(side="left")
        self.btn_reboot = ttk.Button(
            row1, text="重启光猫", width=12, command=self.on_reboot)
        self.btn_reboot.pack(side="left", padx=6)

        row2 = ttk.Frame(ops)
        row2.pack(fill="x", pady=(8, 0))
        self.btn_stock2ubi = ttk.Button(
            row2, text="④ 运行 stock2ubi 刷 U-Boot", width=26,
            command=self.on_stock2ubi)
        self.btn_stock2ubi.pack(side="left")
        ttk.Label(row2, text="文件：").pack(side="left", padx=(10, 0))
        self.stock2ubi_var = tk.StringVar(value="an758x-stock2ubi")
        ttk.Entry(row2, textvariable=self.stock2ubi_var, width=22).pack(side="left")
        ttk.Label(row2, text="（放在程序同目录）", foreground="#666").pack(
            side="left", padx=(4, 0))

        self.hint_labels["ops_hint"] = ttk.Label(
            ops, foreground="#666",
            text="提示：备份需要 root，程序会自动开启 Telnet 与 FTP；"
                 "若你已手动开过其中一个，点对应的单个按钮即可。")
        self.hint_labels["ops_hint"].pack(anchor="w", pady=(6, 0))

        linkrow = ttk.Frame(ops)
        linkrow.pack(anchor="w", pady=(4, 0))
        ttk.Label(linkrow, text="相关项目：", foreground="#666").pack(side="left")
        self._link_label(
            linkrow,
            "an758x-stock2ubi（在光猫上刷 U-Boot 的网页工具）",
            "https://github.com/pbs05/an758x-stock2ubi"
        ).pack(side="left")

        # U-Boot 固件下载：这里放的是「去哪儿下 bl31-u-boot.fip / web U-Boot」。
        # 用户明确要求把这两个地址写在界面上，省得再回帖子里翻。
        dlrow = ttk.Frame(ops)
        dlrow.pack(anchor="w", pady=(2, 0))
        ttk.Label(dlrow, text="U-Boot 固件：", foreground="#666").pack(side="left")
        self._link_label(
            dlrow,
            "pbs05 / uboot-an758x 发布页",
            "https://github.com/pbs05/uboot-an758x/releases"
        ).pack(side="left")
        ttk.Label(dlrow, text="　·　", foreground="#999").pack(side="left")
        self._link_label(
            dlrow,
            "Loong1996 / ImmortalWrt-Airoha（web U-Boot，支持 XG-040G 全系列、HG5382A）",
            "https://github.com/Loong1996/ImmortalWrt-Airoha/releases"
        ).pack(side="left")

        # ===== 刷机（tcboot.bin 写 mtd0，只对贝尔移动版有效）=====
        flashbox = ttk.LabelFrame(
            outer, text="刷机（危险 · tcboot.bin 写 mtd0，仅贝尔移动版适用）",
            padding=8)
        flashbox.pack(fill="x", pady=(8, 0))

        self.allow_flash = tk.BooleanVar(value=False)
        self.flash_check = ttk.Checkbutton(
            flashbox, text="我了解风险，启用刷机功能",
            variable=self.allow_flash, command=self.on_toggle_flash)
        self.flash_check.pack(anchor="w")

        fr = ttk.Frame(flashbox)
        fr.pack(fill="x", pady=(6, 0))
        ttk.Label(fr, text="U-Boot 文件：").pack(side="left")
        self.uboot_var = tk.StringVar(value="tcboot.bin")
        self.uboot_entry = ttk.Entry(fr, textvariable=self.uboot_var, width=30)
        self.uboot_entry.pack(side="left")
        ttk.Label(fr, text="（放在程序同目录）", foreground="#666").pack(
            side="left", padx=(4, 0))

        self.btn_flash = ttk.Button(
            flashbox, text="刷写 U-Boot", width=18, command=self.on_flash,
            state="disabled")
        self.btn_flash.pack(anchor="w", pady=(8, 0))

        # telnet 登录失败后自动重启：认证失败有阈值（贝尔实测 3 次锁 300 秒），
        # 触发后只能等或重启。重启 1~3 分钟，比干等 5 分钟更可控，所以默认开。
        self.auto_reboot = tk.BooleanVar(value=True)
        self.auto_reboot_check = ttk.Checkbutton(
            flashbox,
            text="telnet 登录失败时自动重启光猫解除锁定（推荐）",
            variable=self.auto_reboot, command=self.on_toggle_auto_reboot)
        self.auto_reboot_check.pack(anchor="w", pady=(8, 0))

        self.flash_note = ttk.Label(
            flashbox, foreground="#a00",
            text="刷写前请确认已按上方提示恢复出厂设置；点击后会弹出确认框。")
        self.flash_note.pack(anchor="w", pady=(6, 0))

        # ===== 机型不匹配警告（MF 已实测变砖，TF 未验证）=====
        # 用户明确要求：页面上要提醒，并且代码里要拦。
        # 这里只是「提醒」；真正的硬拦截在 flash.py 的
        # check_uboot_flash_allowed()，按设备自报的 ModelName 判断。
        mf_warn = tk.Frame(flashbox, bg="#fdecea",
                           highlightbackground="#d93025", highlightthickness=2)
        mf_warn.pack(fill="x", pady=(8, 0))
        tk.Label(
            mf_warn, bg="#fdecea", fg="#a50e0e", justify="left", anchor="w",
            wraplength=880, font=("", 9, "bold"),
            text="⚠️ 只适用于贝尔 XG-040G-MD / XG-140G-MD（移动版，AN7581）\n"
                 "已确认：XG-040G-MF 刷入 tcboot.bin 会直接变砖，"
                 "只能拆机接串口进 XMODEM 救砖。\n"
                 "XG-040G-TF 未验证，风险自担。\n"
                 "原因：MF / TF 都是 AN7583 芯片，和 tcboot.bin 对应的 AN7581 不是同一代。\n"
                 "程序会在刷写前读取设备机型名，MF 机型会被直接拒绝。"
        ).pack(fill="x", padx=10, pady=8)

        # 引导版本提示。v2.4.0 起贝尔这边不再是「华邦就会砖」的红框警告 ——
        # 贝尔这几款不在死局确认名单里，官方引导直接刷就行，所以降级成信息框；
        # 文案里留着「万一真出问题去哪找」的指路，免得用户自己乱试。
        self._note_box(flashbox, WINBOND_NOTE_BEL, kind="info", bold=False, pady=(8, 0))

    # ---------- 烽火 tab ----------
    def _build_fh_tab(self, outer):
        self.fh_tip = self._tip_bar(
            outer,
            "提示：请先恢复光猫出厂设置\n"
            "按住光猫 reset 键 20 秒以上，让网页超级密码回到默认值"
            "（telecomadmin / nE7jA%5m）。\n"
            "本页只需要四项：光猫地址、光猫 MAC、网页超级账号、网页超级密码 —— "
            "telnet 密码与提权密码都由程序按机型规则推导，不用你填。")

        acct = ttk.LabelFrame(
            outer, text="必填（保存在 config.ini 的 [fh]）", padding=8)
        acct.pack(fill="x")

        rows = [
            ("fh", "router_ip", "光猫地址", False,
             "光猫的管理地址，默认 192.168.1.1"),
            ("fh", "mac", "光猫 MAC", False,
             "用于推导提权密码（Fh@+MAC后6位）；点右边「自动读取」即可"),
            ("fh", "super_username", "网页超级账号", False,
             "默认 telecomadmin"),
            ("fh", "super_password", "网页超级密码", False,
             "恢复出厂后的默认超级密码，默认 nE7jA%5m"),
            ("fh", "telnet_password", "telnet 密码（选填）", True,
             "留空=自动推导；本工具解不开时，可填别的工具解出的密码"),
            ("fh", "su_password", "su 密码（选填）", True,
             "留空=自动推导（Fh@+MAC后6位）；解不开时手动填"),
        ]
        self._build_account_rows(acct, rows, mac_probe=True)

        note = ttk.Label(
            outer, foreground="#666", justify="left", wraplength=940,
            text="telnet 账号固定 telnetadmin，密码固定 FH-nE7jA%5m（程序会自动先试固定值，"
                 "再试带 MAC 后缀的流传写法）；提权 su root 的密码 = Fh@ + MAC 后 6 位大写。"
                 "本机型不需要开启 FTP。\n"
                 "上面两个「选填」框只在自动推导失败时才需要填 —— 比如本工具内置的密钥"
                 "覆盖不到你这台光猫的固件版本，而你自己用别的工具解出了真实密码。"
                 "填了之后程序会优先用它，不再试其他候选。")
        note.pack(anchor="w", pady=(6, 0))

        # ===== 操作 =====
        ops = ttk.LabelFrame(
            outer, text="操作（备份与刷 U-Boot 合并为一步，全部在 stock2ubi 网页里完成）",
            padding=8)
        ops.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(ops)
        row.pack(fill="x")
        self.btn_fh_telnet = ttk.Button(
            row, text="① 开启 Telnet", width=16,
            command=lambda: self.run_fh_mode("telnet"))
        self.btn_fh_telnet.pack(side="left")
        self.btn_fh_stock2ubi = ttk.Button(
            row, text="② 备份并刷 U-Boot（stock2ubi）", width=30,
            command=self.on_fh_stock2ubi)
        self.btn_fh_stock2ubi.pack(side="left", padx=6)
        self.btn_fh_reboot = ttk.Button(
            row, text="重启光猫", width=12, command=self.on_reboot)
        self.btn_fh_reboot.pack(side="left")

        # 诊断包：telnet 密码解不开时，把设备上的密文/哈希/密钥表打包给作者。
        # 放在这一行最右边，因为它是「出问题时才用」的兜底功能。
        self.btn_fh_report = ttk.Button(
            row, text="导出诊断包", width=13, command=self.on_fh_report)
        self.btn_fh_report.pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(ops)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="stock2ubi 文件：").pack(side="left")
        self.fh_stock2ubi_var = tk.StringVar(value="an758x-stock2ubi")
        ttk.Entry(row2, textvariable=self.fh_stock2ubi_var,
                  width=22).pack(side="left")
        ttk.Label(row2, text="（放在程序同目录）", foreground="#666").pack(
            side="left", padx=(4, 0))

        self.fh_hint = ttk.Label(ops, foreground="#666", wraplength=940,
                                 justify="left", text="")
        self.fh_hint.pack(anchor="w", pady=(6, 0))

        linkrow = ttk.Frame(ops)
        linkrow.pack(anchor="w", pady=(4, 0))
        ttk.Label(linkrow, text="相关项目：", foreground="#666").pack(side="left")
        self._link_label(
            linkrow,
            "an758x-stock2ubi（备份分区 + 刷 U-Boot 的网页工具）",
            "https://github.com/pbs05/an758x-stock2ubi"
        ).pack(side="left")

        # U-Boot 固件下载。HG5585F 只有 pbs05 的 uboot-an758x 能直接用；
        # Loong1996 那个 web U-Boot 目前**没适配 HG5585F**，所以单独一行
        # 并明确写出来，避免用户照着刷错固件。
        dlrow = ttk.Frame(ops)
        dlrow.pack(anchor="w", pady=(2, 0))
        ttk.Label(dlrow, text="U-Boot 固件：", foreground="#666").pack(side="left")
        self._link_label(
            dlrow,
            "pbs05 / uboot-an758x 发布页（本机型用这个）",
            "https://github.com/pbs05/uboot-an758x/releases"
        ).pack(side="left")
        ttk.Label(dlrow, text="　·　", foreground="#999").pack(side="left")
        self._link_label(
            dlrow,
            "Loong1996 / ImmortalWrt-Airoha（web U-Boot，暂未适配 HG5585F）",
            "https://github.com/Loong1996/ImmortalWrt-Airoha/releases"
        ).pack(side="left")

        ttk.Label(
            ops, foreground="#a50e0e", wraplength=940, justify="left",
            text="注意：Loong1996 的 web U-Boot 支持 XG-040G 全系列与 HG5382A，"
                 "但尚未适配 HG5585F。HG5585F 请用 pbs05 / uboot-an758x 的固件。"
        ).pack(anchor="w", pady=(2, 0))

        # ===== 刷机步骤（顺序不能乱，所以做成编号清单）=====
        # 用户要求把 stock2ubi 网页里的操作顺序写在界面上：网页里那几块
        # 是平铺的（刷写 BL2 / UBI 操作 / 板级数据 / 刷写 U-Boot / 刷写系统），
        # 新手很容易跳过「重建 UBI」或者先刷 U-Boot 再补 preloader —— 那样
        # 直接变砖。顺序写死在这里，比让用户自己猜可靠。
        steps = ttk.LabelFrame(
            outer, text="刷机步骤（进 stock2ubi 网页后按这个顺序做，顺序错了会变砖）",
            padding=8)
        steps.pack(fill="x", pady=(10, 0))

        step_text = (
            ("1", "刷写 BL2",
             "上传 preloader.bin，点「上传并写入」。\n"
             "    文件名形如 an7581-fiberhome-hg5585f-ct-preloader.bin。\n"
             "    ⚠️ HG5585F / HG5382A 请改用「修改版」里的临时版（见下方红框）：\n"
             "    " + UBOOT_MODIFIED_DIR + "/" + WINBOND_PRELOADER_TMP + "\n"
             "    ⚠️ 建议用 preloader.bin，不要用 firstblock.bin ——\n"
             "    两者写入位置不同：preloader.bin 从 NAND 偏移 0x800 写入，\n"
             "    前 0x800 字节保持擦除态；firstblock.bin 从偏移 0x0 写入，\n"
             "    会把开头那 0x800 字节一起覆盖。实测 firstblock 成砖率高。"),
            ("2", "重建 UBI",
             "点「重建 UBI」。这一步会清空 UBI 里的全部数据，属于正常现象 ——\n"
             "    首次刷机必须先重建，否则后面的分区写不进去。\n"
             "    ⚠️ 重建完 UBI 就是空的 —— 接着必须做完第 4 步刷入 U-Boot，\n"
             "    否则第 6 步启动时 BL2 在 UBI 里找不到 fip 卷，起不来。"),
            ("3", "刷入板级数据 factory",
             "上传备份目录里的 factory-1m.bin（正好 1.0 MiB，程序自动生成），\n"
             "    点「上传并写入」。这是本机唯一的校准数据（PON 校准 + WiFi EEPROM），\n"
             "    丢了 WiFi 和光模块都不工作，别的机器的 factory 也不能混用。\n"
             "    ⚠️ 不要上传 mtd13_factory.bin —— 那是 3 MiB 的原厂容器，\n"
             "    这一栏只认 1.0 MiB。"),
            ("4", "刷写 U-Boot",
             "上传 bl31-u-boot.fip，点「上传并写入」。\n"
             "    官方版就在程序目录的 " + UBOOT_OFFICIAL_DIR + "/ 里（随 zip 一起发）。\n"
             "    ⚠️ HG5585F / HG5382A 这一步先刷「修改版」的临时引导（见下方红框），\n"
             "    进系统后再换刷官方版：\n"
             "    " + UBOOT_MODIFIED_DIR + "/" + WINBOND_UBOOT_TMP),
            ("5", "刷写系统",
             "上传 QWRT 或 PONWRT 的固件（sysupgrade.bin / sysupgrade.itb），\n"
             "    点「上传并刷写」。"),
            ("6", "启动系统",
             "点「启动系统」。第一次启动较慢（约 1~3 分钟），不要因为没反应就断电。\n"
             "    ⚠️ 点之前确认第 4 步的 bl31-u-boot.fip 已经刷入成功 —— U-Boot 不在\n"
             "    UBI 里就起不来（这种情况接 TTL 用 XMODEM 送一份进去就能救回来）。"),
        )
        for num, title, detail in step_text:
            rowf = ttk.Frame(steps)
            rowf.pack(fill="x", pady=(0, 6))
            tk.Label(rowf, text=num, width=2, anchor="n",
                     font=("", 11, "bold"), fg="#0a66c2").pack(side="left")
            tk.Label(rowf, text=title, font=("", 10, "bold"),
                     anchor="nw").pack(side="left", padx=(4, 8))
            tk.Label(rowf, text=detail, justify="left", anchor="w",
                     wraplength=760).pack(side="left", fill="x", expand=True)

        brick = tk.Frame(steps, bg="#fdecea",
                         highlightbackground="#d93025", highlightthickness=1)
        brick.pack(fill="x", pady=(4, 0))
        tk.Label(
            brick, bg="#fdecea", fg="#a50e0e", justify="left", anchor="w",
            wraplength=920, font=("", 10, "bold"),
            text="⚠️ 顺序上最容易出事的一条：重建 UBI 之后必须刷入 U-Boot。\n"
                 "UBI 一重建，装在它 fip 卷里的 bl31-u-boot.fip 就没了；不补刷第 4 步，\n"
                 "第 6 步「启动系统」只会一直循环报 Stored BL31 + U-Boot FIP failed。\n"
                 "单独重建 UBI 本身不会把机器弄死：preloader 在 NAND 偏移 0x800 的\n"
                 "主引导区、UBI 在另一块区域，两边互不影响。补上 U-Boot 就能起来，\n"
                 "实在不行接一根 TTL 线用 XMODEM 把 fip 送进内存也救得回来。"
        ).pack(anchor="w", padx=8, pady=6)

        # firstblock.bin 的专项警告（用户反馈：这个成功率低、容易砖）。
        # 单独一块而不是塞进第 1 步里，是因为它和「重建后必须刷 U-Boot」不是
        # 一个性质：那条是「顺序别搞错」，这里是「刷了就可能砖，所以别用」。
        #
        # 两者的真正区别是写入位置（据 uboot-an758x 作者的说明）：
        #   firstblock.bin → 从 NAND 偏移 0x0 写，覆盖开头 0x800 字节；
        #   preloader.bin  → 保留开头 0x800 擦除态，从偏移 0x800 写。
        # 换句话说 firstblock 是把 BL2 连同它前面的 0x800 一起整块覆盖，
        # preloader 只补 BL2 那一段。前 0x800 是分区表/引导参数之类的位置，
        # 各机型不一致，所以 firstblock 更容易写坏 —— 这才是成砖率高的原因。
        fb = tk.Frame(steps, bg="#fff8e1",
                      highlightbackground="#e0c060", highlightthickness=1)
        fb.pack(fill="x", pady=(6, 0))
        tk.Label(
            fb, bg="#fff8e1", fg="#8a6d00", justify="left", anchor="w",
            wraplength=920, font=("", 10, "bold"),
            text="⚠️ 第 1 步请用 preloader.bin，不要用 firstblock.bin\n"
                 "两者的区别是写入 NAND 的位置不同：\n"
                 "  · firstblock.bin —— 从 NAND 偏移 0x0 开始整块写入，"
                 "会把开头那 0x800 字节一起覆盖；\n"
                 "  · preloader.bin  —— 前 0x800 字节保持擦除态，"
                 "只从 NAND 偏移 0x800 写入 BL2。\n"
                 "firstblock 要连带覆盖开头 0x800，而那一段各机型不一样，"
                 "写错就起不来，实测成砖率高得多。\n"
                 "除非你明确知道自己在做什么、且有 XMODEM 救砖条件，否则别碰 firstblock.bin。"
        ).pack(anchor="w", padx=8, pady=6)

        # ===== 华邦闪存专项（用户要求：刷机页面上必须提示）=====
        # 放在第 1/第 4 步的正下方：这两步分别要选 preloader 和 bl31-u-boot.fip，
        # 而华邦机型这两个文件都得换成临时版，提示贴着它们最不容易看漏。
        self._note_box(steps, WINBOND_NOTE_FULL, kind="danger", pady=(6, 0))

        ttk.Label(
            steps, foreground="#666", wraplength=940, justify="left",
            text="第 3 步用 backup_日期_时间/factory-1m.bin —— 这是本工具在备份完"
                 "自动从 mtd13_factory.bin 里提取 PON 校准和 WiFi EEPROM、\n"
                 "按 OpenWrt 的 1 MiB 布局重新拼好的文件。原厂的 mtd13_factory.bin 是"
                 "3 MiB 的 JFFS2/UBI 容器，不能直接上传。\n"
                 "务必用这台机器自己的备份，不要用别人的。"
        ).pack(anchor="w", pady=(2, 0))

        # ===== 闪存批次提醒 =====
        # 用户 m11298：读不到品牌名就算了，页面上提醒「不同批次闪存可能不一样」。
        # 之所以要提醒：U-Boot / OpenWrt 镜像是按闪存类型（SPI NAND / NOR / eMMC）
        # 和擦除块大小编的，同型号不同批次的料可能换过。本工具在真机上只能读到
        # 「是 NAND、128 KiB 擦除块」，读不到厂商 ID（这版固件没暴露 spi-nand 节点）。
        flash_note = tk.Frame(steps, bg="#eef4fb",
                              highlightbackground="#7aa7d9",
                              highlightthickness=1)
        flash_note.pack(fill="x", pady=(6, 0))
        tk.Label(
            flash_note, bg="#eef4fb", fg="#1b4a7a", justify="left", anchor="w",
            wraplength=920, font=("", 10),
            text="ℹ️ 闪存颗粒可能因批次不同而不同\n"
                 "同型号 HG5585F 的不同批次，板上焊的 SPI NAND 未必是同一颗 —— "
                 "容量、页大小、擦除块大小都可能不一样。\n"
                 "本工具只能读到「是 NAND、128 KiB 擦除块」这些参数，"
                 "读不到厂商和型号（固件没暴露）。\n"
                 "所以选 U-Boot / OpenWrt 镜像时，请按你自己这台机器的闪存类型选；"
                 "拿不准就先拆开看芯片丝印，\n"
                 "或者刷之前先确认镜像说明里的擦除块大小与设备一致"
                 "（擦除块对不上会写不进去，甚至起不来）。"
        ).pack(anchor="w", padx=8, pady=6)

        # ===== 断电警告（常驻，红底）=====
        # 用户明确要求：这条必须写在界面上，因为教程没写清楚导致群里很多人变砖。
        warn = tk.Frame(outer, bg="#fdecea", highlightbackground="#d93025",
                        highlightthickness=2)
        warn.pack(fill="x", pady=(10, 0))
        tk.Label(
            warn, bg="#fdecea", fg="#a50e0e", justify="left", anchor="w",
            wraplength=900, font=("", 10, "bold"),
            text="⚠️ 刷机过程中绝对不能断电！\n"
                 "重启后必须立刻访问 http://192.168.0.1 继续完成安装，"
                 "不能中途拔电或关浏览器。\n"
                 "若停在 192.168.0.1 恢复页却不继续安装、或中途断电，"
                 "光猫会变成砖头，只能用 XMODEM 模式救砖（需拆机接串口，"
                 "耗时且不保证成功）。\n"
                 "这是本项目作者和群里多位群友踩过的坑，请务必照做。"
        ).pack(fill="x", padx=10, pady=8)

    # ---------- 账号行（两个 tab 共用）----------
    # ---------- TTL / XMODEM 救砖 tab ----------
    def _build_ttl_tab(self, outer):
        self._tip_bar(
            outer,
            "用途：Winbond 颗粒（W29N02KVSIAF）的机型刷完 U-Boot 后起不来 —— "
            "BL2 读不出 fip 卷，会一直循环打印\n"
            "    ERROR: Stored BL31 + U-Boot FIP failed\n"
            "    Press x to load BL31 + U-Boot FIP via XMODEM\n"
            "这时接上 USB-TTL 线（GND→GND、RX→TX、TX→RX），用本页把 U-Boot "
            "通过 XMODEM 送进内存，设备就能起来进 Web 恢复模式。\n"
            "这条路不需要 telnet、不需要超级密码、也不需要网线 —— "
            "设备本身已经停在那儿等文件了。\n"
            "⚠️ 华邦（Winbond）闪存的机器，FIP 请改用本项目做的临时版"
            "（8bit 读 / 4bit 写），见本页最后的红框。")

        # ===== ① 串口 =====
        sp = ttk.LabelFrame(outer, text="① 串口（USB-TTL 线）", padding=8)
        sp.pack(fill="x", pady=(8, 0))

        r1 = ttk.Frame(sp)
        r1.pack(fill="x")
        ttk.Label(r1, text="串口：").pack(side="left")
        # 用可编辑的 Combobox 而不是只读下拉：既能从「刷新」列出的设备里选，
        # 也能手打 /dev/ttyUSB0 或 COM3（有些系统枚举不出来，只能手填）。
        self.serial_combo = ttk.Combobox(r1, textvariable=self.serial_port,
                                         width=24)
        self.serial_combo.pack(side="left")
        self.btn_rescue_refresh = ttk.Button(
            r1, text="刷新", width=8, command=self.on_refresh_ports)
        self.btn_rescue_refresh.pack(side="left", padx=6)
        ttk.Label(r1, text="波特率：").pack(side="left", padx=(10, 0))
        ttk.Entry(r1, textvariable=self.serial_baud, width=10).pack(side="left")
        ttk.Label(r1, text="（AN7581 只认 115200，改了会收不到 C）",
                  foreground="#777").pack(side="left", padx=6)

        self.serial_hint = ttk.Label(sp, text="", foreground="#666",
                                     justify="left", wraplength=840)
        self.serial_hint.pack(fill="x", pady=(6, 0))

        # ===== ② 文件 =====
        fp = ttk.LabelFrame(outer, text="② 要送进设备的文件", padding=8)
        fp.pack(fill="x", pady=(8, 0))

        r2 = ttk.Frame(fp)
        r2.pack(fill="x")
        self.fip_var = tk.StringVar()
        self.vars[("rescue", "fip")] = self.fip_var
        ttk.Entry(r2, textvariable=self.fip_var).pack(
            side="left", fill="x", expand=True)
        self.btn_rescue_browse = ttk.Button(
            r2, text="浏览…", width=8, command=self.on_rescue_browse)
        self.btn_rescue_browse.pack(side="left", padx=6)
        self.btn_rescue_find = ttk.Button(
            r2, text="自动查找", width=10, command=self.on_rescue_autofind)
        self.btn_rescue_find.pack(side="left")

        self.fip_hint = ttk.Label(fp, text="", foreground="#666",
                                  justify="left", wraplength=840)
        self.fip_hint.pack(fill="x", pady=(6, 0))
        self.fip_var.trace_add("write", lambda *_: self._update_fip_hint())

        # ===== ③ 操作 =====
        op = ttk.LabelFrame(outer, text="③ 操作", padding=8)
        op.pack(fill="x", pady=(8, 0))

        r3 = ttk.Frame(op)
        r3.pack(fill="x")
        self.btn_rescue_uboot = ttk.Button(
            r3, text="刷入 U-Boot（bl31 + u-boot.fip）", width=34,
            command=self.on_rescue_uboot)
        self.btn_rescue_uboot.pack(side="left")
        ttk.Label(r3, text="← 把 FIP 通过 XMODEM 送进内存并直接启动",
                  foreground="#777").pack(side="left", padx=6)

        r4 = ttk.Frame(op)
        r4.pack(fill="x", pady=(8, 0))
        ttk.Label(r4, text='等待 "Press x" 提示：').pack(side="left")
        wvar = tk.StringVar()
        self.vars[("rescue", "wait")] = wvar
        ttk.Entry(r4, textvariable=wvar, width=6).pack(side="left")
        ttk.Label(r4, text="秒").pack(side="left", padx=(2, 14))
        ttk.Label(r4, text="传完后继续监听：").pack(side="left")
        avar = tk.StringVar()
        self.vars[("rescue", "after")] = avar
        ttk.Entry(r4, textvariable=avar, width=6).pack(side="left")
        ttk.Label(r4, text="秒").pack(side="left")
        ttk.Label(r4, text="（设备早就停在提示符上时不会重复打印，"
                           "等够了会直接发 x）", foreground="#777").pack(
            side="left", padx=8)

        r5 = ttk.Frame(op)
        r5.pack(fill="x", pady=(8, 0))
        self.btn_rescue_monitor = ttk.Button(
            r5, text="只看日志（检查线接对没有）", width=28,
            command=self.on_rescue_monitor)
        self.btn_rescue_monitor.pack(side="left")
        ttk.Label(r5, text="不发任何数据，只把设备现在打印的东西显示出来；"
                           "全是乱码时会提示检查接线。", foreground="#777").pack(
            side="left", padx=6)

        r6 = ttk.Frame(op)
        r6.pack(fill="x", pady=(8, 0))
        self.btn_rescue_pre = ttk.Button(
            r6, text="刷入 preloader", width=20, state="disabled")
        self.btn_rescue_pre.pack(side="left")
        self.btn_rescue_fb = ttk.Button(
            r6, text="刷入 firstblock", width=20, state="disabled")
        self.btn_rescue_fb.pack(side="left", padx=6)
        ttk.Label(r6, text="（暂未实现；要短接闪存引脚才能用，"
                           "但一般来说你也用不到 —— 见下方说明）",
                  foreground="#a00").pack(side="left", padx=6)

        # 用户明确要求：这里必须讲清楚「不用去短接、也不用进 preloader 模式」。
        # 那两个按钮存在的最坏后果是用户以为「没刷 preloader 就不能救」，
        # 于是去拆机短接引脚 —— 而实际流程里 preloader 早就已经在了。
        self._note_box(op, NO_SHORT_NOTE, kind="info", bold=False, pady=(8, 0))
        self._note_box(op, WINBOND_NOTE_SHORT, kind="danger", pady=(6, 0))

        # ===== ④ 传完之后 =====
        nxt = ttk.LabelFrame(
            outer, text="④ 传完 FIP 之后（设备已经在 U-Boot 里，不用重启）",
            padding=8)
        nxt.pack(fill="x", pady=(8, 0))
        ttk.Label(nxt, justify="left", anchor="w", wraplength=840,
                  text=RESCUE_OK_NEXT).pack(fill="x")

    def _build_account_rows(self, parent, rows, mac_probe=False):
        """按 (section, key, label, secret, hint) 生成表单行。

        mac_probe=True 时在 MAC 那一行右边加「自动读取」按钮。
        """
        for r, (section, key, label, secret, hint) in enumerate(rows):
            ttk.Label(parent, text=label + "：").grid(
                row=r, column=0, sticky="e", padx=(0, 6), pady=3)
            var = tk.StringVar()
            self.vars[(section, key)] = var
            ent = ttk.Entry(parent, textvariable=var, width=38,
                            show="*" if secret else "")
            ent.grid(row=r, column=1, sticky="we", pady=3)
            cell = ttk.Frame(parent)
            cell.grid(row=r, column=2, sticky="w", padx=(8, 0), pady=3)
            hint_lbl = ttk.Label(cell, text=hint, foreground="#777")
            hint_lbl.pack(side="left")
            self.hint_labels[(section, key)] = hint_lbl
            if mac_probe and key == "mac":
                # MAC 输入框保留：自动读取失败时（换了管理口、页面改版、
                # 电脑没接在 LAN 口）用户仍然可以照背贴手填。
                self.btn_probe_mac = ttk.Button(
                    cell, text="自动读取", width=10, command=self.on_probe_mac)
                self.btn_probe_mac.pack(side="left", padx=(8, 0))
        parent.columnconfigure(1, weight=1)

        # 铭牌用户账号一变，提权账号自动跟着变成「账号_ftp」。
        # 只在用户没手工改过提权账号时才覆盖（_su_auto 记录上次自动填的值），
        # 免得把用户特意填的账号冲掉。
        # 这个方法两个 tab 各调一次，但联动只属于贝尔那组变量：
        # 只在贝尔 tab 建完时挂一次 trace，否则烽火 tab 会把同一个 trace
        # 再挂一遍，账号一改回调就触发两次。
        if not hasattr(self, "_su_auto"):
            self._su_auto = None
        if ("telnet", "username") in self.vars and not getattr(
                self, "_traces_bound", False):
            self.vars[("telnet", "username")].trace_add(
                "write", self._on_username_change)
            self.vars[("telnet", "su_user")].trace_add(
                "write", self._on_su_user_change)
            self._traces_bound = True

    # ---------- 可点击链接 ----------
    def _link_label(self, parent, text, url):
        """做一个蓝色下划线、可点击打开浏览器的链接标签。

        tkinter 没有原生超链接控件，用 Label + 手型光标 + 点击回调模拟。
        """
        lbl = tk.Label(parent, text=text, fg="#0a66c2", cursor="hand2",
                       font=("", 9, "underline"), anchor="w", justify="left")
        lbl.bind("<Button-1>", lambda _e, u=url: self._open_url(u))
        return lbl

    def _open_url(self, url):
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo(APP_TITLE, f"请手动打开：\n{url}\n\n（{e}）")

    # ---------- 机型 / tab ----------
    def _current_family(self):
        """当前 tab 对应哪个流程家族："bel" 或 "fh"。"""
        try:
            idx = self.notebook.index(self.notebook.select())
        except Exception:
            return "bel"
        return "fh" if idx == 1 else "bel"

    def _on_tab_change(self, *_):
        """切 tab 时同步 self.model 并存进配置（下次启动回到这个 tab）。"""
        try:
            idx = self.notebook.index(self.notebook.select())
        except Exception:
            idx = 0
        if idx == 2:
            # 第三个 tab（TTL 救砖）跟机型无关，切过去不能改用户的机型 ——
            # 否则去救砖页看一眼回来，贝尔/烽火的账号密码就被换掉了。
            return
        fam = self._current_family()
        self.model = models_mod.get("hg5585f" if fam == "fh" else "bel")
        self.cfg.set("device", "model", self.model.key)
        try:
            cfgmod.save(self.cfg, os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        except Exception:
            pass
        self._apply_model_to_ui()

    def _apply_model_to_ui(self):
        """把当前机型的特征反映到界面上（提示、功能开关）。

        tab 重构后，提示文案是**静态**写在各自 tab 里的（两个机型差别太大，
        没必要再动态改写）；这里只负责按钮可用性和刷机区灰化。
        """
        m = self.model
        self._flash_supported = m.supports_uboot_flash
        # 刷机区只存在于贝尔 tab；烽火 tab 里没有这些控件
        if getattr(self, "flash_check", None) is not None:
            self.flash_check.configure(
                state="normal" if m.supports_uboot_flash else "disabled")
        if getattr(self, "uboot_entry", None) is not None:
            self.uboot_entry.configure(
                state="normal" if m.supports_uboot_flash else "disabled")
        # 烽火 tab 的操作提示
        if getattr(self, "fh_hint", None) is not None:
            if m.supports_stock2ubi:
                t = ("提示：② 会自动完成这些事：上传并启动 an758x-stock2ubi → "
                     "把 17 个分区全部下载到 backup_日期_时间/ 并校验 SHA256 → "
                     "打开它的网页让你上传 preloader / U-Boot → 重启后访问 "
                     "192.168.0.1 继续安装。备份是本工具做的，不用去网页里点下载。")
            else:
                t = "提示：本机型暂不支持自动刷写。"
            self.fh_hint.configure(text=t)
        self.on_toggle_flash()

    def _model_mac(self):
        return self.vars[("fh", "mac")].get().strip()

    def _derive_passwords_if_needed(self):
        """烽火：校验 MAC 是否已填（密码由程序推导，不需要用户填）。

        telnet 密码是固定值，su 密码 = Fh@+MAC后6位，所以唯一必须有的是 MAC。
        MAC 不合法就拦下 —— 否则会拿空密码去连，白白烧认证机会
        （连续 3 次失败锁 300 秒）。
        """
        if self._current_family() != "fh":
            return True, ""
        mac = self._model_mac()
        if not models_mod.normalize_mac(mac):
            return False, ("烽火机型需要「光猫 MAC」（形如 70:D5:1E:04:6C:EC），"
                           "否则推导不出提权密码。\n\n"
                           "点 MAC 输入框右边的「自动读取」可从设备直接读回；"
                           "读不到就照光猫背面标签手填。")
        if len(models_mod.normalize_mac(mac)) != 12:
            return False, (f"MAC 格式不对：{mac}\n"
                           "需要 12 位十六进制（可带 : 或 - 分隔），"
                           "例如 70:D5:1E:04:6C:EC。")
        return True, ""

    # ---------- 提权账号自动跟随铭牌账号（贝尔）----------
    def _on_username_change(self, *_):
        """铭牌用户账号变化 → 提权账号自动设为「账号_ftp」。"""
        if not self.model.su_user_follows_telnet:
            return
        if ("telnet", "username") not in self.vars:
            return
        uname = self.vars[("telnet", "username")].get().strip()
        cur = self.vars[("telnet", "su_user")].get().strip()
        if cur in ("", self._su_auto):
            self._su_auto = f"{uname}_ftp" if uname else ""
            self.vars[("telnet", "su_user")].set(self._su_auto)

    def _on_su_user_change(self, *_):
        """用户手工改了提权账号 → 记住它，之后不再自动覆盖。"""
        if ("telnet", "su_user") not in self.vars:
            return
        cur = self.vars[("telnet", "su_user")].get().strip()
        if cur != self._su_auto:
            self._su_auto = None

    # ---------- 配置读写 ----------
    def _load_into_ui(self):
        for (section, key), var in self.vars.items():
            try:
                var.set(self.cfg.get(section, key))
            except Exception:
                pass
        # 载入后同步自动推导状态：若配置里的提权账号正好等于
        # 「铭牌账号_ftp」，就认作自动值，之后账号一改它还会跟着走。
        if ("telnet", "username") in self.vars:
            uname = self.vars[("telnet", "username")].get().strip()
            su = self.vars[("telnet", "su_user")].get().strip()
            self._su_auto = f"{uname}_ftp" if su == f"{uname}_ftp" else None
        # 上次用的是哪个 tab 就回到哪个 tab
        last = self.cfg.get("device", "model")
        idx = 1 if last == "hg5585f" else 0
        try:
            self.notebook.select(idx)
        except Exception:
            pass
        self.model = models_mod.get("hg5585f" if idx == 1 else "bel")
        if getattr(self, "auto_reboot", None) is not None:
            self.auto_reboot.set(cfgmod.get_bool(
                self.cfg, "safety", "auto_reboot_on_login_fail", True))
        # TTL 串口：控件暂时不显示，但变量仍要跟上配置（功能代码保留）
        self.serial_on.set(cfgmod.get_bool(self.cfg, "serial", "enabled", False))
        self.serial_baud.set(self.cfg.get("serial", "baudrate") or "115200")
        saved_port = self.cfg.get("serial", "port").strip()
        if saved_port:
            self.serial_port.set(saved_port)
        # 救砖 tab：把保存的 FIP 状态显示出来，并顺手枚举一次串口
        self._update_fip_hint()
        if getattr(self, "serial_combo", None) is not None:
            self.on_refresh_ports()
        self._apply_model_to_ui()

    def _save_from_ui(self):
        for (section, key), var in self.vars.items():
            self.cfg.set(section, key, var.get())
        self.cfg.set("device", "model", self.model.key)
        if getattr(self, "auto_reboot", None) is not None:
            self.cfg.set("safety", "auto_reboot_on_login_fail",
                         "true" if self.auto_reboot.get() else "false")
        self.cfg.set("serial", "enabled",
                     "true" if self.serial_on.get() else "false")
        self.cfg.set("serial", "port", self.serial_port.get().strip())
        self.cfg.set("serial", "baudrate", self.serial_baud.get() or "115200")
        # 临时 HTTP 端口由程序在运行时决定（8080 被占用会顺延并写盘）。这里
        # 以 flash 模块里的实际值为准，否则用户一点「保存配置」就把它打回
        # 8080，下次又得重新探测、还得重新去防火墙放行。
        self.cfg.set("net", "http_port",
                     str(getattr(self.flash, "LOCAL_HTTP_PORT", 8080)))
        cfgmod.save(self.cfg, os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))

    def on_toggle_auto_reboot(self):
        """自动重启勾选框：立刻写进配置，避免用户忘了点「保存配置」。"""
        self.cfg.set("safety", "auto_reboot_on_login_fail",
                     "true" if self.auto_reboot.get() else "false")
        try:
            cfgmod.save(self.cfg, os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        except Exception:
            pass
        if self.auto_reboot.get():
            self.log_line("[*] 已开启：telnet 登录失败时自动重启光猫解除锁定\n")
        else:
            self.log_line("[!] 已关闭自动重启：登录失败后程序会直接退出，"
                          "需要你手动重启光猫\n")

    def on_save(self):
        try:
            self._save_from_ui()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存失败：{e}")
            return
        self.log_line(f"[✓] 配置已保存到 {cfgmod.CONFIG_FILE}\n")

    def on_reload(self):
        self.cfg = cfgmod.load(os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        self._load_into_ui()
        self.log_line("[*] 已重新载入配置\n")

    def on_reset(self):
        if not messagebox.askyesno(
                APP_TITLE, "恢复为默认账号密码？当前修改会丢失。"):
            return
        self.cfg = cfgmod.load("__no_such_file__")  # 强制用默认值
        self._load_into_ui()
        self.log_line("[*] 已恢复默认配置（记得点保存）\n")

    # ---------- 自动读取 MAC（烽火 tab）----------
    def on_probe_mac(self):
        """从设备只读地读回 MAC 并填进表单（烽火 8080 的 web_get_brmad）。

        为什么要有这个按钮：烽火的提权密码由 MAC 推导，而 MAC 抄错一位算出来
        的 AES 密钥就全错，设备只回一个 403 —— 从错误信息完全看不出是
        「抄错」还是「协议不对」。设备自己就能报出 MAC，没理由让用户去背贴上找。
        但输入框仍然保留：换了管理口 / 页面改版 / 电脑没接 LAN 口时，
        自动读不到，用户还能照标签手填。
        """
        ip = self.vars[("fh", "router_ip")].get().strip() or "192.168.1.1"
        port = getattr(models_mod.get("hg5585f"), "web_port", 8080) or 8080
        self.log_line(f"[*] 正在从 {ip}:{port} 读取 MAC ...\n")
        self.btn_probe_mac.configure(state="disabled")

        # 结果先放盒子，由**主线程**的 after 轮询取走。
        # 不能在子线程里直接调 root.after()/控件方法：Tk 不是线程安全的，
        # 从非主线程调用会抛 "main thread is not in main loop"（实测踩过）。
        box = {}

        def _work():
            try:
                import fhtool
                box["mac"] = fhtool.probe_mac(ip, port=port, timeout=6.0)
            except Exception as e:  # noqa: BLE001
                box["err"] = f"{type(e).__name__}: {e}"
                box["mac"] = ""

        def _poll(tries=0):
            if "mac" not in box and "err" not in box:
                if tries < 80:                    # 最多等约 16 秒
                    self.root.after(200, lambda: _poll(tries + 1))
                else:
                    self.btn_probe_mac.configure(state="normal")
                    self._probe_mac_failed()
                return
            self.btn_probe_mac.configure(state="normal")
            if box.get("err"):
                self.log_line(f"[!] 读取失败：{box['err']}\n")
            mac = box.get("mac") or ""
            if mac:
                self._apply_probed_mac(mac)
            else:
                self._probe_mac_failed()

        threading.Thread(target=_work, daemon=True).start()
        self.root.after(200, _poll)

    def _apply_probed_mac(self, mac):
        self.vars[("fh", "mac")].set(mac)
        self.log_line(f"[✓] 读到 MAC: {mac}\n")
        self.log_line("[*] 提权密码 = Fh@ + 后 6 位 = "
                      f"Fh@{models_mod.mac_suffix(mac)}\n")

    def _probe_mac_failed(self):
        self.log_line("[!] 没能从设备读到 MAC。\n")
        self.log_line("    请确认电脑直连光猫 LAN 口、地址填的是光猫的管理地址；\n")
        self.log_line("    也可以照光猫背面标签手填（形如 70:D5:1E:04:6C:EC）。\n")
        messagebox.showwarning(
            APP_TITLE,
            "没能自动读到 MAC。\n\n"
            "请确认电脑直连光猫 LAN 口，并检查「光猫地址」是否填对；\n"
            "也可以照光猫背面标签手工填写 MAC。")

    # ---------- 日志 ----------
    # Text 控件最怕「一行一次 insert」：串口刷屏时几百行就是几百次重绘加滚动条
    # 更新，界面直接卡住。这里改成三条：攒一批再写、逐行折叠同一条报错、
    # 每种报错只提示一次。
    def log_line(self, text):
        self.log_queue.put(text)

    def _log_key(self, line):
        """抹掉地址/块号，让「同一个错、不同块号」算出同一个特征。"""
        return _LOG_ADDR.sub("#", line).strip()

    def _log_flush_repeat(self):
        """把重复次数补写到那一条的行尾（原地改一行，不新开行）。"""
        if self._log_idx <= 0 or self._log_repeat <= 1:
            return
        if not self._log_last.strip():
            # 连续空行折叠掉就行，别在空行后面再挂个「×N」
            return
        self.log.delete(f"{self._log_idx}.0 linestart",
                        f"{self._log_idx}.0 lineend")
        self.log.insert(f"{self._log_idx}.0 linestart",
                        f"{self._log_last}    … 同类重复 ×{self._log_repeat}\n")

    def _log_one(self, line):
        key = self._log_key(line)
        if key == self._log_key_last:
            self._log_repeat += 1
            self._log_dirty = True       # 先记下，等这批处理完只补写一次
            return
        self._log_key_last = key
        self._log_repeat = 1
        self._log_last = line
        # 先取行号再插入：插进去以后 end 就跑到下一行去了
        self._log_idx = int(self.log.index("end-1c").split(".")[0])
        self.log.insert("end", line + "\n")
        for name, pat, msg in _LOG_HINTS:
            if name in self._log_hinted or not pat.search(line):
                continue
            self._log_hinted.add(name)
            self.log.insert("end", msg + "\n", "hint")
            break

    def _log_feed(self, text):
        buf = self._log_buf + text
        if "\n" not in buf:
            self._log_buf = buf          # 半行先留着，等下一批补齐
            return
        parts = buf.split("\n")
        self._log_buf = parts.pop()      # split 的尾巴就是没写完的那半行
        # 用户往上翻看历史时别把他拽回底部
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        for line in parts:
            self._log_one(line)
        if self._log_dirty:              # 一批最多补写一次，不是每个重复都写
            self._log_flush_repeat()
            self._log_dirty = False
        self.log.configure(state="disabled")
        if at_bottom:
            self.log.see("end")

    def _pump_log(self):
        # 一次把队列里积压的输出全取走，合成一段一次写完
        chunk = []
        try:
            while True:
                text = self.log_queue.get_nowait()
                if text != "__DONE__":
                    chunk.append(text)
        except queue.Empty:
            pass
        if chunk:
            self._log_feed("".join(chunk))
        self.root.after(120, self._pump_log)

    # ---------- 线程与状态 ----------
    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        # 两个 tab 的按钮都要跟着忙闲切换（漏掉某个会让它在运行时仍可点）
        for name in ("btn_telnet", "btn_ftp", "btn_backup", "btn_reboot",
                     "btn_stock2ubi", "btn_probe_mac", "btn_fh_telnet",
                     "btn_fh_stock2ubi", "btn_fh_reboot", "btn_fh_report",
                     "btn_rescue_uboot", "btn_rescue_monitor",
                     "btn_rescue_refresh", "btn_rescue_browse",
                     "btn_rescue_find"):
            b = getattr(self, name, None)
            if b is not None:
                b.configure(state=state)
        # 刷机按钮还要看勾选框
        if busy:
            if getattr(self, "btn_flash", None) is not None:
                self.btn_flash.configure(state="disabled")
        else:
            self.on_toggle_flash()

    def _start_worker(self, fn, *a, **kw):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "已有任务在运行，请等它结束。")
            return
        self._set_busy(True)
        self.worker = threading.Thread(target=self._worker_wrap, args=(fn, a, kw),
                                       daemon=True)
        self.worker.start()

    def _worker_wrap(self, fn, a, kw):
        old = sys.stdout
        sys.stdout = LogRedirector(self.log_queue)
        try:
            fn(*a, **kw)
        except SystemExit as e:
            # flash.py 出错时用 sys.exit(1)，这里不该把整个 GUI 带走
            if e.code not in (0, None):
                self.log_queue.put(f"\n[!] 任务中止（退出码 {e.code}）\n")
        except Exception as e:
            self.log_queue.put(f"\n[!] 出错：{type(e).__name__}: {e}\n")
        finally:
            sys.stdout = old
            self.log_queue.put("__DONE__")

    def _load_flash(self):
        if self.flash is None:
            import importlib.util
            # 用 find_resource 而不是写死路径：打包成 exe 后 flash.py 在解包
            # 目录里，同时优先用 exe 同目录的那份，方便用户直接替换。
            path = find_resource("flash.py")
            if not path:
                raise FileNotFoundError("找不到 flash.py（应与程序放在一起）")
            spec = importlib.util.spec_from_file_location("flash", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.flash = mod
        # 两个 tab 用**两套完全独立**的写配置函数：
        #   贝尔 → apply_model（机型档案打底）+ apply_to_flash（[device]/[telnet]/[super]）
        #   烽火 → apply_fh_to_flash（只读 [fh] 段，绝不碰贝尔那组键）
        # 混用会让在一个 tab 保存的值冲掉另一个 tab 的账号密码。
        if self._current_family() == "fh":
            cfgmod.apply_fh_to_flash(self.cfg, self.flash)
        else:
            cfgmod.apply_model(self.cfg, self.flash, self.model)
            cfgmod.apply_to_flash(self.cfg, self.flash)
        return self.flash

    def _prepare(self):
        """保存配置 + 校验机型必需的字段 + 加载 flash 模块。

        返回 flash 模块；失败时弹框并返回 None。
        烽火 tab 没填 MAC 就直接拦下 —— 否则会拿着空密码去连 telnet，
        白白消耗认证机会（连续 3 次失败要锁 300 秒）。
        """
        try:
            ok, msg = self._derive_passwords_if_needed()
            if not ok:
                messagebox.showerror(APP_TITLE, msg)
                return None
            self._save_from_ui()
            return self._load_flash()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"初始化失败：{e}")
            return None

    # ---------- TTL 串口（界面暂时隐藏，代码保留）----------
    def on_refresh_ports(self):
        """重新枚举串口并填进下拉框（救砖 tab 的「刷新」按钮）。"""
        combo = getattr(self, "serial_combo", None)
        if combo is None:
            return
        ports = serial_shell.list_ports()
        combo.configure(values=[f"{dev}" for dev, _ in ports])
        hint = getattr(self, "serial_hint", None)
        if hint is None:
            return
        if not serial_shell.available():
            hint.configure(
                text="没有安装 pyserial，TTL 功能不可用。安装：pip install pyserial",
                foreground="#a00")
            self.serial_port.set("")
            return
        if not ports:
            # 只有本来就是空的才自动清空。用户手打过一个设备名（比如系统枚举
            # 不出来的 /dev/ttyUSB3）时不能因为一次「刷新」就把它抹掉。
            keep = (self.serial_port.get() or "").strip()
            hint.configure(
                text=("没有发现串口设备。请插上 USB-TTL 线，再点「刷新」。"
                      + (f"（保留手填的 {keep}）" if keep else "")),
                foreground="#a00")
            if not keep:
                self.serial_port.set("")
            return
        desc = "；".join(f"{dev} = {d}" for dev, d in ports)
        hint.configure(text=f"发现 {len(ports)} 个串口：{desc}", foreground="#666")

    def _on_serial_toggle(self):
        """勾上「走 TTL 串口」后，联网相关的按钮就该灰掉。"""
        on = bool(self.serial_on.get())
        if on:
            self.on_refresh_ports()
        for name in ("btn_telnet", "btn_ftp", "btn_fh_telnet"):
            b = getattr(self, name, None)
            if b is not None:
                b.configure(state="disabled" if on else "normal")
        self._apply_model_to_ui()

    def _serial_args(self):
        """把界面上的串口设置变成 run_flow 认的 args 片段。

        TTL 界面按用户要求暂时隐藏，serial_on 永远是 False，所以这里正常
        返回空 dict（= 走 telnet）。功能代码保留，将来取消注释即可启用。
        """
        if not self.serial_on.get():
            return {}
        port = (self.serial_port.get() or "").strip()
        if not port:
            messagebox.showerror(
                APP_TITLE,
                "已勾选「走 TTL 串口」，但没有选择串口。\n\n"
                "请点「刷新」，然后在下拉框里选一个。")
            return None
        return {"serial_port": port,
                "serial_baud": int(self.serial_baud.get() or 115200)}

    # ---------- 各操作 ----------
    def run_mode(self, mode):
        """贝尔 tab 的四个操作（telnet / ftp / backup）。"""
        flash = self._prepare()
        if flash is None:
            return
        serial = self._serial_args()
        if serial is None:
            return

        args = {"mode": mode, "enable_telnet": flash.AUTO_ENABLE_TELNET,
                "enable_ftp": flash.AUTO_ENABLE_FTP, "yes": False,
                "reboot": False,
                "force_backup": not flash.SKIP_BACKUP_IF_COMPLETE}
        args.update(serial)

        label = {"telnet": "开启 Telnet", "ftp": "开启 FTP",
                 "backup": "备份全部分区"}[mode]
        via = "（TTL 串口）" if serial else ""
        self.log_line(f"\n{'=' * 56}\n[*] 开始：{label}{via}"
                      f"（机型：{self.model.short}）\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def run_fh_mode(self, mode):
        """烽火 tab 的操作：目前只有「开启 Telnet」。

        备份和刷 U-Boot 合并进 stock2ubi（见 on_fh_stock2ubi），所以这里
        没有 backup / flash 分支 —— 烽火刷 tcboot 没有意义（tcboot 只适配
        贝尔移动版）。
        """
        flash = self._prepare()
        if flash is None:
            return
        args = {"mode": mode, "enable_telnet": True, "enable_ftp": False,
                "yes": False, "reboot": False, "force_backup": False}
        args.update(self._serial_args() or {})
        label = {"telnet": "开启 Telnet"}.get(mode, mode)
        self.log_line(f"\n{'=' * 56}\n[*] 开始：{label}"
                      f"（机型：{self.model.short}）\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_reboot(self):
        if not messagebox.askyesno(
                APP_TITLE,
                "确认重启光猫？\n\n设备会断开 1~3 分钟，期间无法访问。\n"
                "重启也会清掉所有网页会话和 telnet 锁定。"):
            return
        flash = self._prepare()
        if flash is None:
            return
        args = {"mode": "backup", "reboot": True,
                "enable_telnet": False, "enable_ftp": False,
                "yes": False, "force_backup": False}
        args.update(self._serial_args() or {})
        self.log_line(f"\n{'=' * 56}\n[*] 开始：重启光猫"
                      f"（机型：{self.model.short}）\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_toggle_flash(self):
        # 机型不支持刷 tcboot（烽火）时，勾选也不给解锁
        if getattr(self, "btn_flash", None) is None:
            return
        if self.allow_flash.get() and getattr(self, "_flash_supported", True):
            self.btn_flash.configure(state="normal")
        else:
            self.btn_flash.configure(state="disabled")

    def _known_device_identity(self):
        """尽量在不连设备的情况下猜出「这台机器是什么型号」，返回 (型号名, 物料号)。

        数据来源优先级：
          1. 最近一次备份目录里的 `device.json`（本工具自己写的，字段可靠）
          2. 都没有 → 返回 ("", "")，由调用方按「未知机型」处理

        为什么不去连设备实时读：`on_flash` 里连设备要走完整个 telnet + su 流程，
        等读到机型名时用户已经点了「确认刷写」——太晚了。而备份是**刷机前
        必然做过**的一步（run_flow 里备份在刷写之前），所以这里读旧备份
        既快又几乎总能命中。

        真正的硬拦截仍在 flash.py：那边用设备**实时**上报的 ModelName 判断，
        就算这里猜错也绕不过去。
        """
        import glob
        import json

        try:
            base = _resource_dir()
            pats = [os.path.join(base, "backup_*", "device.json"),
                    os.path.join(base, "backup_*", "*", "device.json")]
            cands = []
            for pat in pats:
                cands.extend(glob.glob(pat))
            # 按修改时间倒序，取最新那份（用户最近刷的机器最可能是现在这台）
            cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for path in cands[:8]:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue
                dev = data.get("device") or data
                # 只认设备**真实自报**的型号。两个坑都要防：
                #   1. 新版会把档案补位名单独记在 ModelNameFromProfile，
                #      ModelName 只放真实值 —— 直接用即可。
                #   2. 旧版备份把档案名（「烽火 HG5585F CT（电信版）」这种
                #      中文描述）写进了 ModelName。那不是设备型号，拿去比对
                #      黑名单永远不匹配，等于白拦。所以这里只接受「像设备
                #      型号」的值：纯 ASCII 字母数字加连字符。
                model = str(dev.get("ModelName") or "").strip()
                if model and not all(
                        c.isascii() and (c.isalnum() or c in "-_ ")
                        for c in model):
                    model = ""
                part = str(dev.get("PartNumber") or "").strip()
                if model or part:
                    return model, part
        except Exception:  # noqa: BLE001
            pass
        return "", ""

    def on_flash(self):
        # 恢复出厂设置的提醒放在 tab 顶部的固定提示里（不跟刷机按钮绑定）。
        # 这里只做刷机本身该做的确认。
        flash = self._prepare()
        if flash is None:
            return

        uboot = self.uboot_var.get().strip() or "tcboot.bin"
        path = find_resource(uboot)
        if not path:
            messagebox.showerror(
                APP_TITLE,
                f"找不到 U-Boot 文件：{uboot}\n\n"
                f"请把它放在程序同目录：\n{_resource_dir()}")
            return
        size = os.path.getsize(path)
        if size > 0x80000:
            messagebox.showerror(
                APP_TITLE, f"U-Boot 文件过大（{size} 字节），mtd0 仅 512KB。")
            return
        flash.UBOOT_FILE = uboot

        # === 机型预检（能提前判断就提前拦，别等跑到一半才中止）===
        # 优先用最近一次备份里的 device.json（不用连设备，快且零风险）；
        # 没有备份就让用户确认「我知道我这台是 MD」。真正的硬拦截仍在
        # flash.py：那边用设备实时上报的 ModelName 判断，绕不过去。
        known_model, known_part = self._known_device_identity()
        allowed, reason, warn_only = flash.check_uboot_flash_allowed(
            known_model, known_part)
        if not allowed:
            messagebox.showerror(APP_TITLE, f"已拒绝刷写\n\n{reason}")
            self.log_line(f"[✗] 已拒绝刷写（机型 {known_model}）：未做任何写入。\n")
            return
        if warn_only:
            if not messagebox.askyesno(
                    APP_TITLE, f"机型未验证\n\n{reason}\n\n仍要继续吗？"):
                self.log_line("[*] 已取消刷机（未做任何写入）。\n")
                return

        # 最终确认
        if not messagebox.askyesno(
                APP_TITLE,
                "即将刷写 U-Boot\n\n"
                f"文件：{uboot}（{size} 字节）\n"
                "目标：/dev/mtdblock0\n\n"
                "⚠️ 写入仅适配 XG-040G-MD / XG-140G-MD（移动版）！\n"
                "⚠️ XG-040G-MF 刷入会变砖，已有人踩过！\n"
                "⚠️ 写入不可逆，断电或写错会导致设备变砖！\n"
                "⚠️ 刷写过程中绝对不能断电！\n"
                "程序会先做全分区备份，但仍请确认已备份重要数据。\n\n"
                "确定现在刷写吗？"):
            self.log_line("[*] 已取消刷机（未做任何写入）。\n")
            return

        args = {"mode": "flash", "enable_telnet": flash.AUTO_ENABLE_TELNET,
                "enable_ftp": flash.AUTO_ENABLE_FTP, "yes": True,
                "reboot": False, "force_backup": False}
        serial = self._serial_args()
        if serial is None:
            return
        args.update(serial)
        via = "（TTL 串口）" if serial else ""
        self.log_line(f"\n{'=' * 56}\n[!] 开始刷写 U-Boot：{uboot}{via}\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def _stock2ubi_args(self, name, flash):
        # force_backup=False：让 flash.py 先找「同一台设备且校验完整」的旧备份，
        # 找到就跳过下载（烽火全量备份 400MB+，重复传没必要）。
        # no_browser=False：备份做完后由 flash.py 打开 stock2ubi 网页。
        return {"mode": "stock2ubi",
                "enable_telnet": True,
                "enable_ftp": bool(flash.NEED_FTP_FOR_SU),
                "yes": False, "reboot": False, "force_backup": False,
                "stock2ubi_file": name,
                "stock2ubi_ignore_kernel": False,
                "no_browser": False}

    def on_stock2ubi(self):
        """贝尔 tab：上传 an758x-stock2ubi 并后台运行。

        本工具只负责把它送上去跑起来；真正的刷写动作在它的网页里完成，
        所以这里不锁定、也不需要勾选「启用刷机功能」。
        """
        flash = self._prepare()
        if flash is None:
            return

        name = self.stock2ubi_var.get().strip() or "an758x-stock2ubi"
        path = find_resource(name)
        if not path:
            messagebox.showerror(
                APP_TITLE,
                f"找不到文件：{name}\n\n"
                f"请把 an758x-stock2ubi 放在程序同目录：\n{_resource_dir()}\n\n"
                f"下载地址：\n{flash.STOCK2UBI_PROJECT_URL}")
            return

        if not messagebox.askyesno(
                APP_TITLE,
                "即将上传并在光猫后台运行 an758x-stock2ubi\n\n"
                f"文件：{name}（{os.path.getsize(path)} 字节）\n"
                f"运行位置：光猫 /tmp，监听端口 {flash.STOCK2UBI_PORT}\n\n"
                "启动后本工具会先把全部分区自动备份下来（带 SHA256 清单，\n"
                "同一台设备已有完整备份则跳过），再打开它的网页。\n\n"
                "网页里的刷写顺序（顺序错了会变砖）：\n"
                "  ① 刷写 BL2 —— 上传 preloader.bin\n"
                "     （别用 firstblock.bin：整块 boot 区原样镜像，\n"
                "      成功率低、很容易砖，除非你有 XMODEM 救砖条件）\n"
                "  ② 重建 UBI（首次刷机必须先做）\n"
                "  ③ 板级数据 —— 上传刚生成的 factory-1m.bin（1.0 MiB，\n"
                "     不是 mtd13_factory.bin）\n"
                "  ④ 刷写 U-Boot —— 上传 bl31-u-boot.fip\n"
                "  ⑤ 刷写系统 —— 上传 QWRT / PONWRT 的 sysupgrade\n"
                "  ⑥ 启动系统\n\n"
                "⚠️ 刷写过程中绝对不能断电！\n"
                "⚠️ 重启后必须访问 http://192.168.0.1 继续安装，不能中途放弃，\n"
                "    否则光猫会变砖，只能用 XMODEM 模式救砖。\n\n"
                "这一步本身不写入任何分区，确定继续吗？"):
            self.log_line("[*] 已取消。\n")
            return

        args = self._stock2ubi_args(name, flash)
        self.log_line(f"\n{'=' * 56}\n"
                      f"[*] 开始：运行 an758x-stock2ubi（{name}）\n"
                      f"{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_fh_stock2ubi(self):
        """烽火 tab：备份 + 刷 U-Boot 合并为一步（全部走 stock2ubi 网页）。

        为什么合并：烽火没有 tcboot.bin 那套（tcboot 只适配贝尔移动版），
        备份分区和写 preloader / U-Boot 都由 an758x-stock2ubi 的网页完成，
        对本工具来说就是「把工具送上去跑起来 + 打开网页」一件事。
        后续适配 AN7581 家族（HG5382A 等）也走这条。
        """
        flash = self._prepare()
        if flash is None:
            return

        name = self.fh_stock2ubi_var.get().strip() or "an758x-stock2ubi"
        path = find_resource(name)
        if not path:
            messagebox.showerror(
                APP_TITLE,
                f"找不到文件：{name}\n\n"
                f"请把 an758x-stock2ubi 放在程序同目录：\n{_resource_dir()}\n\n"
                f"下载地址：\n{flash.STOCK2UBI_PROJECT_URL}")
            return

        if not messagebox.askyesno(
                APP_TITLE,
                "即将自动备份全部 17 个分区，然后进入刷 U-Boot 流程\n\n"
                f"文件：{name}（{os.path.getsize(path)} 字节）\n"
                f"运行位置：光猫 /tmp，监听端口 {flash.STOCK2UBI_PORT}\n\n"
                "程序会自己完成：\n"
                "  1. 上传并启动 an758x-stock2ubi；\n"
                "  2. 逐个分区下载到 backup_日期_时间/，边下边算 SHA256，"
                "并写 device.json 清单（stock2ubi 自己不做校验）；\n"
                "  3. 备份完打开它的网页，你在网页里按这个顺序刷：\n"
                "       ① 刷写 BL2 —— 上传 preloader.bin\n"
                "          （别用 firstblock.bin：整块 boot 区原样镜像，\n"
                "           成功率低、很容易砖）\n"
                "       ② 重建 UBI（会清空 UBI 内数据，首次刷机必须先做）\n"
                "       ③ 板级数据 —— 上传 factory-1m.bin（1.0 MiB，自动生成）\n"
                "       ④ 刷写 U-Boot —— 上传 bl31-u-boot.fip\n"
                "       ⑤ 刷写系统 —— 上传 QWRT / PONWRT 的 sysupgrade\n"
                "       ⑥ 启动系统（首次约 1~3 分钟，别断电）\n"
                "  4. 重启后立刻访问 http://192.168.0.1 继续完成安装。\n\n"
                "⚠️ 顺序上最容易出事的一条：\n"
                "   重建 UBI 之后必须刷入 bl31-u-boot.fip（第 4 步）——\n"
                "   UBI 一重建，里面 fip 卷的 U-Boot 就没了，不补刷就直接\n"
                "   启动，只会一直循环报 Stored BL31 + U-Boot FIP failed。\n"
                "   （单独重建 UBI 不会弄死机器，补上 U-Boot 就能起来。）\n\n"
                "同一台设备（SN 相同）已有完整备份时会跳过下载。\n\n"
                "⚠️ 整个过程绝对不能断电！\n"
                "⚠️ 重启后若停在 192.168.0.1 恢复页却不继续安装，"
                "光猫会变砖，\n"
                "    只能用 XMODEM 模式救砖（需拆机接串口）。\n\n"
                "确定开始吗？"):
            self.log_line("[*] 已取消。\n")
            return

        args = self._stock2ubi_args(name, flash)
        self.log_line(f"\n{'=' * 56}\n"
                      f"[*] 开始：烽火 备份 + 刷 U-Boot（{name}）\n"
                      f"{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_fh_report(self):
        """烽火 tab：把设备上和密码相关的文件打包成诊断 zip，交给作者分析。

        什么时候用：本工具解不出 telnet 密码（换了密钥表的新固件），而用户
        又没有别的办法拿到密码。包里是设备上真实的密文、哈希、以及本地用过
        的密钥和解密结果 —— 作者据此能判断是密钥不对、算法变了，还是容器头
        是新的。全程只读下载，不改设备任何状态。
        """
        flash = self._prepare()
        if flash is None:
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "从光猫下载密码相关文件并打包？\n\n"
                "会做这些事（都是只读，不改设备状态）：\n"
                "  · 读取设备信息（型号 / SN / MAC / 固件版本）\n"
                "  · 下载 /var/tel_passwd、/fhdata/pre_usrconfig_conf 等密码相关文件\n"
                "  · 从里面切出密文片段，逐个试本地密钥并记录结果\n"
                "  · 把本工具内置的密钥表一并写进去\n"
                "  · 取回设备上的解密库 libfhcfg.so / libfhcrypto_new.so\n"
                "    （含 AES 工作密钥的那几个，作者换固件后靠它重新抓密钥）\n\n"
                "最后一步要登录 telnet：会先把 so 复制到 /var 再下载，\n"
                "下完立刻删除临时副本。telnet 登不上就跳过，其余照常。\n\n"
                "⚠️ 包里有 MAC、PON SN、整机 SN、telnet 密码哈希，\n"
                "   可能还有 Wi-Fi 密码等配置。只发给作者，不要公开贴到论坛。\n\n"
                "确定开始吗？"):
            self.log_line("[*] 已取消。\n")
            return
        self.log_line(f"\n{'=' * 56}\n[*] 开始：导出诊断包\n{'=' * 56}\n")

        def _worker():
            import fhreport
            path = fhreport.collect(flash)
            print(f"\n[✓] 诊断包已生成：{path}")
            print("    把这个文件发给作者即可。")
            return path

        self._start_worker(_worker)


# ---------- TTL / XMODEM 救砖 ----------
    # 每个机型在 GitHub Release 里的 FIP 文件名都带机型串。自动查找时按当前
    # 机型的串优先，免得贝尔的机器被刷进烽火的 U-Boot（U-Boot 带设备树，
    # 刷错了网口、LED、reset 键都会不对）。
    _FIP_TOKENS = {
        "hg5585f": ("hg5585f",),
        "bel": ("xg-040g-md", "xg-140g-md"),
    }

    def _load_rescue(self):
        if self.rescue is None:
            import importlib.util
            # 和 _load_flash 一样用 find_resource：打包成 exe 后 ttl_rescue.py
            # 在解包目录里，同时优先用 exe 同目录那份，方便用户直接替换。
            path = find_resource("ttl_rescue.py")
            if not path:
                raise FileNotFoundError("找不到 ttl_rescue.py（应与程序放在一起）")
            spec = importlib.util.spec_from_file_location("ttl_rescue", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.rescue = mod
        return self.rescue

    def _find_fips(self):
        """在程序目录 / uboot-bin / _src/rel 里递归找 *bl31-u-boot.fip。

        **故意不返回「修改版」的临时引导**（文件名带 -tmp-8bit-read-4bit-write，
        或者躺在 uboot-bin/修改版/ 里）：救砖这条路要的是官方引导。
        会砖到需要救砖，多半就是「原厂按 8bit 写、官方 BL2 按 4bit 读」那个
        组合；而 XMODEM 是把 FIP 直接送进内存、根本不经过 NAND 读取，官方版
        在这一步照样跑得起来。把修改版塞进救砖流程只会让用户在两种引导之间
        来回横跳。修改版留给「烽火 HG5585F 电信版」tab 的刷机流程用。
        """
        seen, out = set(), []
        # 注意：**不要**用 glob 的 `**` 递归。程序目录在开发机上就是整个仓库
        # （_fw_rootfs / backup_* 有好几万个文件），递归 glob 会让界面卡住
        # 十几秒甚至更久。这里用 os.walk + 深度上限 + 剪掉明显无关的目录。
        skip_dirs = {".git", "__pycache__", "node_modules", ".venv", ".idea"}

        def _walk_fips(root_dir, max_depth=3):
            root_dir = os.path.abspath(root_dir)
            base = root_dir.rstrip(os.sep).count(os.sep)
            found = []
            for dirpath, dirnames, filenames in os.walk(root_dir):
                depth = dirpath.rstrip(os.sep).count(os.sep) - base
                if depth >= max_depth:
                    dirnames[:] = []          # 到底了，不再往下钻
                else:
                    dirnames[:] = [d for d in dirnames
                                   if d not in skip_dirs and not d.startswith(".")]
                for fn in filenames:
                    if fn.endswith("bl31-u-boot.fip"):
                        found.append(os.path.join(dirpath, fn))
            return sorted(found)

        for d in (os.path.join(_resource_dir(), "uboot-bin"),
                  _resource_dir(),
                  os.path.join(_resource_dir(), "_src", "rel"),
                  os.path.join(_bundle_dir(), "uboot-bin"),
                  _bundle_dir()):
            if not os.path.isdir(d):
                continue
            # uboot-bin/ 拆成了「修改版 / 官方版」两个子目录，所以要往下找。
            for p in _walk_fips(d):
                rp = os.path.realpath(p)
                if rp in seen or _is_modified_uboot(p):
                    continue
                seen.add(rp)
                out.append(p)
        return out

    def _fip_path(self, raw=None):
        """把输入框里的路径还原成真实可用的路径。

        「自动查找」写进去的是**相对程序目录**的短路径（见 _preferred_fip），
        这里要按程序目录还原；用户手输或用「浏览…」选的通常是绝对路径，
        原样返回。相对当前工作目录的写法也保留，别把用户填的东西改没。
        """
        p = (raw if raw is not None else (self.fip_var.get() or "")).strip()
        p = os.path.expanduser(p)
        if not p or os.path.isabs(p):
            return p
        cand = os.path.join(_resource_dir(), p)
        return cand if os.path.exists(cand) else p

    def _preferred_fip(self):
        """按当前机型挑一个 FIP；一个都没找到返回 None。

        返回**相对程序目录**的路径（能相对就相对）：这个值会被存进
        `config.ini`，写成绝对路径换台机器就失效了；界面上也会显示成
        `/home/xxx/下载/...` 一长串，很难看。
        """
        cands = self._find_fips()
        if not cands:
            return None
        tokens = self._FIP_TOKENS.get(self.model.key, ())
        pick = None
        for c in cands:
            base = os.path.basename(c).lower()
            if any(t in base for t in tokens):
                pick = c
                break
        if pick is None:
            pick = cands[0]
        try:
            rel = os.path.relpath(pick, _resource_dir())
        except ValueError:            # Windows 上跨盘符会抛，直接用原路径
            return pick
        if rel.startswith(".."):      # 不在程序目录里，保留绝对路径
            return pick
        return rel.replace(os.sep, "/")

    def _update_fip_hint(self):
        lbl = getattr(self, "fip_hint", None)
        if lbl is None:
            return
        p = (self.fip_var.get() or "").strip()
        if not p:
            lbl.configure(text="留空 = 运行时自动找 *bl31-u-boot.fip",
                          foreground="#666")
            return
        # 自动查找填的是相对程序目录的短路径，显示短的那个、检查长的那个
        real = self._fip_path(p)
        if not os.path.isfile(real):
            lbl.configure(text=f"文件不存在：{p}", foreground="#a00")
            return
        try:
            size = os.path.getsize(real)
            with open(real, "rb") as fh:
                magic = fh.read(4)
        except OSError as e:
            lbl.configure(text=f"读不了这个文件：{e}", foreground="#a00")
            return
        # 所有 bl2/fip 容器的头 4 字节都是小端 0xAA640001。刷错文件是这里
        # 唯一能在传送前就拦下来的错误，所以值得单独提示。
        ok = len(magic) == 4 and int.from_bytes(magic, "little") == 0xAA640001
        tail = ("开头是 FIP magic 0xAA640001 ✓" if ok else
                f"⚠ 开头不是 FIP magic（实际 {magic.hex()}），可能不是 FIP 文件")
        # 「自动查找」和列表里已经把修改版挡在外面了，但用户还能用「浏览…」手动
        # 指到它，所以这里再拦一道：救砖要的是官方引导，选中修改版直接报警。
        base = os.path.basename(p)
        if _is_modified_uboot(p):
            tail += (" · ⚠ 这是「修改版」临时引导，救砖请换成官方版："
                     + UBOOT_OFFICIAL_DIR + "/")
            ok = False
        lbl.configure(text=f"{base} · {size:,} 字节 · {tail}",
                      foreground="#666" if ok else "#a00")

    def on_rescue_browse(self):
        init = os.path.dirname(self._fip_path())
        if not init or not os.path.isdir(init):
            init = os.path.join(_resource_dir(), "uboot-bin")
            if not os.path.isdir(init):
                init = _resource_dir()
        p = filedialog.askopenfilename(
            parent=self.root, title="选择 bl31 + u-boot 的 FIP 文件",
            initialdir=init,
            filetypes=[("FIP 固件", "*.fip"), ("所有文件", "*.*")])
        if p:
            self.fip_var.set(p)

    def on_rescue_autofind(self):
        p = self._preferred_fip()
        if not p:
            messagebox.showwarning(
                APP_TITLE,
                "没找到 *bl31-u-boot.fip。\n\n"
                "请把 FIP 放进程序目录或 uboot-bin/ 里，"
                "或者点「浏览…」手动选。")
            return
        self.fip_var.set(p)

    def _rescue_run(self, mod, argv, done):
        """后台线程里跑救砖，成功就切回主线程回调。

        messagebox 只能在主线程弹，后台线程里直接弹会把界面搞崩。
        """
        rc = mod.main(argv)
        if rc == 0:
            self.root.after(0, done)
        return rc

    def _rescue_done_dialog(self):
        """传完 FIP 之后弹一次提示 —— 别让用户自己猜下一步。"""
        messagebox.showinfo(APP_TITLE, RESCUE_OK_NEXT)

    def _rescue_common(self, extra, label, done=None):
        """救砖页所有按钮的公共前半段：找模块、校串口、存配置、起线程。

        done 不为空时表示「跑成功之后要在主线程里做一件事」（现在是弹提示）。
        """
        try:
            mod = self._load_rescue()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(
                APP_TITLE,
                f"无法加载救砖模块 ttl_rescue.py：{e}\n\n"
                "这是打包时漏带文件造成的（不是你的操作问题）。\n"
                "应急办法：把源码里的 ttl_rescue.py 复制到程序（exe）所在目录，"
                "重新打开本工具即可。\n"
                "也请到项目主页反馈一下，好把这个包修掉：\n"
                f"{PROJECT_URL}")
            return
        port = (self.serial_port.get() or "").strip()
        if not port:
            messagebox.showerror(
                APP_TITLE,
                "还没有选串口。\n\n插上 USB-TTL 线，点「刷新」，"
                "再在下拉框里选一个（列不出来就直接手打，"
                "如 /dev/ttyUSB0 或 COM3）。")
            return
        baud = (self.serial_baud.get() or "115200").strip() or "115200"
        self._save_from_ui()
        # --log 不带文件名：ttl_rescue 会自己写成 _out/ttl_rescue_时间戳.log，
        # 乱码现场就算关了窗口也还留着证据。
        argv = ["--port", port, "--baud", baud, "--log"] + list(extra)
        self.log_line(f"\n{'=' * 56}\n[*] 开始：{label}"
                      f"（串口 {port} @ {baud}）\n{'=' * 56}\n")
        if done is None:
            self._start_worker(mod.main, argv)
        else:
            self._start_worker(self._rescue_run, mod, argv, done)

    def on_rescue_uboot(self):
        """把 bl31 + u-boot 的 FIP 通过 XMODEM 送进内存。"""
        fip = (self.fip_var.get() or "").strip()
        if not fip:
            fip = self._preferred_fip() or ""
            if fip:
                self.fip_var.set(fip)
        fip = self._fip_path(fip)
        if not fip or not os.path.isfile(fip):
            messagebox.showerror(
                APP_TITLE,
                "没找到要送进设备的 FIP 文件。\n\n"
                "点「自动查找」，或「浏览…」选一个 *bl31-u-boot.fip。")
            return
        if not messagebox.askyesno(
                APP_TITLE,
                f"确认通过 TTL 串口把下面这个文件送进光猫？\n\n"
                f"{fip}\n{os.path.getsize(fip):,} 字节\n\n"
                "传送过程中不要拔线、不要断电。\n"
                "这一步只把 U-Boot 送进内存直接跑起来，不写 flash，"
                "所以砖不会更严重 —— 但也还没真修好。"):
            return
        wait = (self.vars[("rescue", "wait")].get() or "30").strip() or "30"
        after = (self.vars[("rescue", "after")].get() or "20").strip() or "20"
        self._rescue_common(
            ["--file", fip, "--wait", wait, "--after", after],
            "XMODEM 刷入 U-Boot", done=self._rescue_done_dialog)

    def on_rescue_monitor(self):
        """只听不发：用来确认线接对了、波特率对了、设备在说什么。"""
        secs = (self.cfg.get("rescue", "monitor_seconds") or "15").strip() or "15"
        self._rescue_common(["--monitor", "--monitor-seconds", secs],
                            f"监听串口 {secs} 秒")

def _missing_optional(problems, what, feature, howto):
    """报告一个「源码可选、打包必需」的依赖缺失。

    源码直接跑时缺 pyserial / fh_tool_cli 是正常的（按机型、按接入方式装），
    所以只打 [!] 提示；但打成 exe 之后用户没法自己 pip install，缺了就是
    功能残缺，必须算失败 —— 否则 CI 会带着坏包一路发到 Release
    （实测踩过：CI 漏装 fh_tool-cli，产物比本地小 4.5MB，烽火功能是坏的）。
    """
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        problems.append(f"打包缺 {what} —— {feature}功能不可用（{howto}）")
        print(f"  [✗] {what} —— {feature}功能不可用，打包时必须带上（{howto}）")
    else:
        print(f"  [!] {what} 不可用 —— {feature}功能不可用，"
              f"源码运行时可忽略（{howto}）")


def self_test():
    """检查运行环境：flash.py 能否加载、依赖是否齐全。

    打包成 exe 后 flash.py 是运行时动态加载的，PyInstaller 看不到它的 import，
    漏掉某个模块要等真正用到处才炸（曾漏掉 http.server，备份时才报错）。
    这里把所有依赖提前导一遍，早暴露问题。

    命令行运行：`xe-pon-flash-toolkit.exe --selftest`（GUI 版也能跑，会弹窗显示）
    """
    problems = []
    print(f"{APP_TITLE_FULL}")
    print(f"程序目录: {_resource_dir()}")
    print(f"打包资源: {_bundle_dir()}")

    # 1. flash.py 里的标准库依赖
    for m in ("http.server", "http.cookiejar", "urllib.request",
              "urllib.parse", "urllib.error", "posixpath", "base64",
              "hashlib", "subprocess", "threading", "socket", "json", "re"):
        try:
            __import__(m)
            print(f"  [✓] {m}")
        except Exception as e:
            problems.append(f"缺模块 {m}: {type(e).__name__}: {e}")
            print(f"  [✗] {m} — {e}")

    # 2. 第三方依赖
    for m in ("telnetlib3", "telnetlib3.telnetlib"):
        try:
            __import__(m)
            print(f"  [✓] {m}")
        except Exception as e:
            problems.append(f"缺模块 {m}: {type(e).__name__}: {e}")
            print(f"  [✗] {m} — {e}")

    # 2a. TTL 串口。源码运行时是「可选依赖」；但**打包后的 exe** 必须自带，
    # 否则用户拿到的是个功能残缺的包 —— 所以冻结环境下缺了直接判失败。
    try:
        import serial_shell
        if serial_shell.available():
            print("  [✓] pyserial（TTL 串口可用）")
        else:
            _missing_optional(problems, "pyserial", "TTL 串口", "pip install pyserial")
    except Exception as e:  # noqa: BLE001
        _missing_optional(problems, f"serial_shell（{type(e).__name__}: {e}）",
                          "TTL 串口", "pip install pyserial")

    # 2b. 烽火机型后端。同理：打包时必须带上，否则烽火 tab 是坏的。
    try:
        import fhtool
        if fhtool.available():
            print("  [✓] fh_tool_cli（烽火机型可用）")
        else:
            _missing_optional(problems, "fh_tool_cli", "烽火机型",
                              "pip install fh_tool-cli")
    except Exception as e:  # noqa: BLE001
        _missing_optional(problems, f"fhtool（{type(e).__name__}: {e}）",
                          "烽火机型", "pip install fh_tool-cli")

    # 2b-2. 烽火 factory 镜像生成（备份后自动调用，缺了烽火刷机就少一步）
    try:
        import fhfactory
        print(f"  [✓] fhfactory（1 MiB factory 生成可用，"
              f"布局 {fhfactory.FACTORY_SIZE} 字节）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"缺模块 fhfactory: {type(e).__name__}: {e}")
        print(f"  [✗] fhfactory — {e}")

    # 2b-3. 烽火 telnet 密码找回（认证被拒时的兜底通道）
    try:
        import fhdecrypt
        # 真解一次固定向量，确认密钥表和 AES 实现都真的能跑
        probe = fhdecrypt.decrypt_value_auto(
            "2506000049ECBE32C8DE760F0E960B0F0A53953F")[0]
        if probe != "FH-nE7jA%5m":
            raise RuntimeError(f"固定向量解出来是 {probe!r}，应为 FH-nE7jA%5m")
        # md5-crypt 也验一次：登录前靠它把候选密码和设备上的哈希比对，
        # 算错了会导致「明明密码对却一直说不匹配」，比解不出更隐蔽。
        if fhdecrypt.md5_crypt("1234", "") != "$1$$iC.dUsGpxNNJGeOm1dFio/":
            raise RuntimeError("md5-crypt 实现自检失败")
        print("  [✓] fhdecrypt（telnet 密码找回可用，AES + md5-crypt 校验通过）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"缺模块 fhdecrypt: {type(e).__name__}: {e}")
        print(f"  [✗] fhdecrypt — {e}")

    # 2b-4. 诊断包（telnet 密码解不开时，把材料打包给作者）
    try:
        import fhreport
        # 真切一段密文，确认「找密文 + 试解密」这条链路是通的。
        # 用固件里的真实 UCI 片段（2502 容器的 TelnetPassword）。
        sample = (b"option TelnetPassword "
                  b"'250200008554F8E46C6E87B9A4DB8311D3A1EFEE'")
        frags = fhreport._find_ciphertexts(
            sample, fhdecrypt.ALL_MAGICS, limit=8)
        if not frags:
            raise RuntimeError("从 UCI 文本里切不出密文（切分逻辑失效）")
        if frags[0].get("field") != "TelnetPassword":
            raise RuntimeError(f"字段名识别错：{frags[0].get('field')!r}")
        # 诊断包要带设备的解密 so，SO_FILES 空了就说明这功能被删残了
        if not getattr(fhreport, "SO_FILES", ()):
            raise RuntimeError("SO_FILES 是空的，诊断包不会带解密 so")
        print(f"  [✓] fhreport（诊断包可用，密文切分 + 字段识别正常，"
              f"带 {len(fhreport.SO_FILES)} 个解密 so）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"缺模块 fhreport: {type(e).__name__}: {e}")
        print(f"  [✗] fhreport — {e}")

    # 2c. 机型注册表
    try:
        import models
        print("  [✓] models.py（机型: "
              + " / ".join(m.short for m in models.all_models()) + "）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"机型注册表 models.py 加载失败: {type(e).__name__}: {e}")
        print(f"  [✗] models.py — {type(e).__name__}: {e}")

    # 3. tkinter（GUI 自身依赖）
    try:
        import tkinter  # noqa: F401
        print("  [✓] tkinter")
    except Exception as e:
        problems.append(f"缺模块 tkinter: {type(e).__name__}: {e}")
        print(f"  [✗] tkinter — {e}")

    # 4. 真正加载 flash.py（这一步能暴露它内部的所有 import 问题）
    path = find_resource("flash.py")
    if not path:
        problems.append("找不到 flash.py")
        print("  [✗] flash.py 未找到")
    else:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("flash_selftest", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            print(f"  [✓] flash.py 加载成功"
                  f"（版本号 {getattr(mod, 'VERSION', '未知')}，"
                  f"内置回退分区表 {len(mod.MTD_PARTITIONS)} 个，"
                  f"运行时以设备 /proc/mtd 为准）")
        except Exception as e:
            problems.append(f"flash.py 加载失败: {type(e).__name__}: {e}")
            print(f"  [✗] flash.py — {type(e).__name__}: {e}")

    # 4a. TTL / XMODEM 救砖模块。救砖 tab 用 spec_from_file_location 从**磁盘**
    # 加载它（不是 import），所以它属于「数据文件」，必须写进 build_exe.spec 的
    # datas；只写 hiddenimports 是不够的（那样磁盘上根本没有这个 .py）。
    # 踩过：v2.4.0 的 exe 漏了这一行，用户点救砖报「找不到 ttl_rescue.py」。
    _path = find_resource("ttl_rescue.py")
    if not _path:
        problems.append("找不到 ttl_rescue.py —— TTL / XMODEM 救砖功能不可用"
                        "（build_exe.spec 的 datas 里必须带上它）")
        print("  [✗] ttl_rescue.py 未找到 —— 救砖功能不可用")
    else:
        try:
            import importlib.util
            _spec = importlib.util.spec_from_file_location("ttl_rescue_selftest", _path)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            # 真跑一遍内存回环的 XMODEM 收发，确认分块/CRC/重传/EOT 链路是通的
            _rc = _mod.selftest(verbose=False)
            if _rc != 0:
                raise RuntimeError(f"内置回环自检返回 {_rc}")
            print(f"  [✓] ttl_rescue.py 加载成功（XMODEM 回环自检通过，"
                  f"默认波特率 {_mod.DEFAULT_BAUD}）")
        except Exception as e:  # noqa: BLE001
            problems.append(f"ttl_rescue.py 加载失败: {type(e).__name__}: {e}")
            print(f"  [✗] ttl_rescue.py — {type(e).__name__}: {e}")

    # 4b. 闪存 ECC 判定。flash.py 里是 `import nandcheck`，漏了不会崩
    # （有 try/except 兜底，只会打印「跳过闪存兼容性检查」），但华邦机型那道闸
    # 会**静默失效** —— 这比崩了更糟，所以冻结环境下缺了也要判失败。
    try:
        import nandcheck
        _chip = nandcheck.KNOWN_PARALLEL_NAND.get((0xEF, 0xDA, 0x069510))
        if not _chip:
            raise RuntimeError("颗粒表里查不到 W29N02KVSIAF")
        print(f"  [✓] nandcheck.py（颗粒表 {len(nandcheck.KNOWN_PARALLEL_NAND)} 条，"
              f"{_chip[1]} 在华邦 ECC 黑名单里）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"nandcheck.py 加载失败: {type(e).__name__}: {e} —— "
                        f"华邦机型的 ECC 拦截会失效")
        print(f"  [✗] nandcheck.py — {type(e).__name__}: {e}")

    # 5. tcboot.bin（刷机才需要，缺了只提示）
    if find_resource("tcboot.bin"):
        print("  [✓] tcboot.bin 已找到")
    else:
        print("  [!] 未找到 tcboot.bin（只有刷机需要，放 exe 同目录即可）")

    print()
    if problems:
        print("发现问题：")
        for p in problems:
            print("  -", p)
    else:
        print("自检通过：依赖齐全，flash.py 可正常加载。")
    return problems


def main():
    # flash.py 的备份目录、tcboot.bin 都是相对路径，统一切到程序所在目录
    os.chdir(_resource_dir())

    # 命令行自检：--selftest / -t（在 GUI 起来前跑，方便定位打包问题）
    if any(a in ("--selftest", "-t", "--test") for a in sys.argv[1:]):
        lines = []

        class _Cap:
            def write(self, t):
                lines.append(t)

            def flush(self):
                pass

        old = sys.stdout
        sys.stdout = _Cap()
        try:
            problems = self_test()
        finally:
            sys.stdout = old
        report = "".join(lines)
        # 打包成 GUI 程序后没有控制台，把结果写到文件，方便用户回传
        try:
            with open(os.path.join(_resource_dir(), "selftest.log"),
                      "w", encoding="utf-8") as f:
                f.write(report)
        except Exception:
            pass
        # 只在**失败**时弹窗：成功还弹会挡住自动化调用（踩过）。
        # --quiet（CI 用）则连失败也不弹，只靠 selftest.log 和退出码，
        # 否则弹窗会在无人值守的环境里把流程挂住。
        quiet = any(a in ("--quiet", "-q") for a in sys.argv[1:])
        if problems and not quiet:
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror(
                    APP_TITLE + " · 自检",
                    "发现问题：\n\n" + "\n".join(problems) +
                    "\n\n详情见程序目录的 selftest.log")
                root.destroy()
            except Exception:
                pass
        return 1 if problems else 0

    root = tk.Tk()
    app = App(root)

    # 结束后恢复按钮（轮询 worker 状态，避免跨线程操作 tk）
    def watch():
        if app.worker and not app.worker.is_alive():
            app.worker = None
            app._set_busy(False)
        root.after(300, watch)

    watch()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
