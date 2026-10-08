#!/usr/bin/env python3
"""XG-040G-MD 光猫工具 · 图形界面

只用 Python 标准库 tkinter，不需要额外安装依赖（Windows 版 exe 也是这套）。

界面分区：
  1. 账号密码设置（存 config.ini，可保存/重载）
  2. 功能按钮：开 Telnet / 开 FTP / 备份 / 重启
  3. 刷机（默认锁定，需先勾选"启用刷机功能"解锁；点击后弹出多重确认）
  4. 运行日志
"""
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import config as cfgmod

APP_TITLE = "XG-040G-MD 光猫工具"


def _resource_dir():
    """可写的工作目录：exe 所在目录（或源码目录）。

    config.ini、backup_* 都放这里，因为打包后 _MEIPASS 是只读临时目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


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


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1000x820")
        self.root.minsize(900, 760)

        self.log_queue = queue.Queue()
        self.worker = None
        self.flash = None          # 延迟导入 flash，避免启动时就做网络探测

        self.cfg = cfgmod.load(os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        self.vars = {}
        self._build_ui()
        self._load_into_ui()
        self._pump_log()

    # ---------- 界面 ----------
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # ===== 顶部提示：恢复出厂设置 =====
        # 这是使用前提的说明，不跟刷机按钮绑定（用户明确要求）。
        tip = tk.Frame(outer, bg="#fff8e1", highlightbackground="#e0c060",
                       highlightthickness=1)
        tip.pack(fill="x", pady=(0, 8))
        tk.Label(
            tip, bg="#fff8e1", fg="#8a6d00", justify="left", anchor="w",
            wraplength=820,
            text="提示：请先恢复光猫出厂设置\n"
                 "按住光猫 reset 键 20 秒以上，让超级管理员密码回到默认值"
                 "（CMCCAdmin / aDm8H%MdA），程序才能自动登录网页开启 "
                 "Telnet 与 FTP。"
        ).pack(fill="x", padx=8, pady=6)

        # ===== 账号密码 =====
        acct = ttk.LabelFrame(outer, text="账号密码（保存在 config.ini）", padding=10)
        acct.pack(fill="x")

        rows = [
            ("device", "router_ip", "光猫地址", False,
             "光猫的管理地址，默认 192.168.1.1"),
            ("telnet", "username", "铭牌用户账号", False,
             "光猫底部铭牌上的用户账号（本机型为 user，不同型号可能不同）"),
            ("telnet", "password", "铭牌用户密码", True,
             "光猫底部铭牌上的用户密码；telnet 登录用它"),
            ("telnet", "su_user", "提权账号（su）", False,
             "自动填成「铭牌用户账号_ftp」，即拿到 root 的账号"),
            ("telnet", "root_password", "提权密码（留空=同上）", True,
             "留空表示与「铭牌用户密码」相同"),
            ("super", "username", "网页超级账号", False,
             "登录管理网页用的超级账号，默认 CMCCAdmin"),
            ("super", "password", "网页超级密码", True,
             "恢复出厂后的默认超级密码，用于自动开启 Telnet / FTP"),
        ]
        for r, (section, key, label, secret, hint) in enumerate(rows):
            ttk.Label(acct, text=label + "：").grid(
                row=r, column=0, sticky="e", padx=(0, 6), pady=3)
            var = tk.StringVar()
            self.vars[(section, key)] = var
            ent = ttk.Entry(acct, textvariable=var, width=38,
                            show="*" if secret else "")
            ent.grid(row=r, column=1, sticky="we", pady=3)
            ttk.Label(acct, text=hint, foreground="#777").grid(
                row=r, column=2, sticky="w", padx=(8, 0), pady=3)
        acct.columnconfigure(1, weight=1)

        btns = ttk.Frame(acct)
        btns.grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="保存配置", command=self.on_save).pack(side="left")
        ttk.Button(btns, text="重新载入", command=self.on_reload).pack(
            side="left", padx=6)
        ttk.Button(btns, text="恢复默认", command=self.on_reset).pack(side="left")

        # 铭牌用户账号一变，提权账号自动跟着变成「账号_ftp」。
        # 只在用户没手工改过提权账号时才覆盖（_su_auto 记录上次自动填的值），
        # 免得把用户特意填的账号冲掉。
        self._su_auto = None
        self.vars[("telnet", "username")].trace_add("write", self._on_username_change)
        self.vars[("telnet", "su_user")].trace_add("write", self._on_su_user_change)

        # ===== 功能 =====
        ops = ttk.LabelFrame(outer, text="操作（默认只读，不写入任何分区）", padding=10)
        ops.pack(fill="x", pady=(10, 0))

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

        ttk.Label(
            ops,
            text="提示：备份需要 root，程序会自动开启 Telnet 与 FTP；"
                 "若你已手动开过其中一个，点对应的单个按钮即可。",
            foreground="#666").pack(anchor="w", pady=(6, 0))

        # ===== 刷机 =====
        flashbox = ttk.LabelFrame(outer, text="刷机（危险）", padding=10)
        flashbox.pack(fill="x", pady=(10, 0))

        self.allow_flash = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            flashbox, text="我了解风险，启用刷机功能",
            variable=self.allow_flash, command=self.on_toggle_flash
        ).pack(anchor="w")

        fr = ttk.Frame(flashbox)
        fr.pack(fill="x", pady=(6, 0))
        ttk.Label(fr, text="U-Boot 文件：").pack(side="left")
        self.uboot_var = tk.StringVar(value="tcboot.bin")
        ttk.Entry(fr, textvariable=self.uboot_var, width=30).pack(side="left")
        ttk.Label(fr, text="（放在程序同目录）", foreground="#666").pack(
            side="left", padx=(4, 0))

        self.btn_flash = ttk.Button(
            flashbox, text="③ 刷写 U-Boot", width=18, command=self.on_flash,
            state="disabled")
        self.btn_flash.pack(anchor="w", pady=(8, 0))

        ttk.Label(
            flashbox,
            text="刷写前请确认已按上方提示恢复出厂设置；点击后会弹出确认框。",
            foreground="#a00").pack(anchor="w", pady=(6, 0))

        # ===== 日志 =====
        logbox = ttk.LabelFrame(outer, text="运行日志", padding=6)
        logbox.pack(fill="both", expand=True, pady=(10, 0))
        self.log = scrolledtext.ScrolledText(
            logbox, height=14, wrap="word", state="disabled",
            font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    # ---------- 提权账号自动跟随铭牌账号 ----------
    def _on_username_change(self, *_):
        """铭牌用户账号变化 → 提权账号自动设为「账号_ftp」。

        仅当提权账号当前值等于上次自动填入的值（或为空）时才覆盖，
        这样用户手工改过的提权账号不会被冲掉。
        """
        uname = self.vars[("telnet", "username")].get().strip()
        cur = self.vars[("telnet", "su_user")].get().strip()
        if cur in ("", self._su_auto):
            self._su_auto = f"{uname}_ftp" if uname else ""
            self.vars[("telnet", "su_user")].set(self._su_auto)

    def _on_su_user_change(self, *_):
        """用户手工改了提权账号 → 记住它，之后不再自动覆盖。"""
        cur = self.vars[("telnet", "su_user")].get().strip()
        if cur != self._su_auto:
            self._su_auto = None

    # ---------- 配置读写 ----------
    def _load_into_ui(self):
        for (section, key), var in self.vars.items():
            var.set(self.cfg.get(section, key))
        # 载入后同步自动推导状态：若配置里的提权账号正好等于
        # 「铭牌账号_ftp」，就认作自动值，之后账号一改它还会跟着走。
        uname = self.vars[("telnet", "username")].get().strip()
        su = self.vars[("telnet", "su_user")].get().strip()
        self._su_auto = f"{uname}_ftp" if su == f"{uname}_ftp" else None

    def _save_from_ui(self):
        for (section, key), var in self.vars.items():
            self.cfg.set(section, key, var.get())
        cfgmod.save(self.cfg, os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))

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
        if not messagebox.askyesno(APP_TITLE, "恢复为默认账号密码？当前修改会丢失。"):
            return
        self.cfg = cfgmod.load("__no_such_file__")  # 强制用默认值
        self._load_into_ui()
        self.log_line("[*] 已恢复默认配置（记得点保存）\n")

    # ---------- 日志 ----------
    def log_line(self, text):
        self.log_queue.put(text)

    def _pump_log(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                if text == "__DONE__":
                    continue
                self.log.configure(state="normal")
                self.log.insert("end", text)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._pump_log)

    # ---------- 线程与状态 ----------
    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        # 四个操作按钮都要跟着忙闲切换（漏掉 btn_ftp 会让它在运行时仍可点）
        for b in (self.btn_telnet, self.btn_ftp, self.btn_backup, self.btn_reboot):
            b.configure(state=state)
        # 刷机按钮还要看勾选框
        if busy:
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
            # flash.py 内部用 __file__ 决定工作目录，这里已在 main() 里 chdir
            self.flash = mod
        cfgmod.apply_to_flash(self.cfg, self.flash)
        return self.flash

    # ---------- 各操作 ----------
    def run_mode(self, mode):
        try:
            self._save_from_ui()
            flash = self._load_flash()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"初始化失败：{e}")
            return

        args = {"mode": mode, "enable_telnet": flash.AUTO_ENABLE_TELNET,
                "enable_ftp": flash.AUTO_ENABLE_FTP, "yes": False,
                "reboot": False, "force_backup": not flash.SKIP_BACKUP_IF_COMPLETE}

        label = {"telnet": "开启 Telnet", "ftp": "开启 FTP",
                 "backup": "备份全部分区"}[mode]
        self.log_line(f"\n{'=' * 56}\n[*] 开始：{label}\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_reboot(self):
        if not messagebox.askyesno(
                APP_TITLE,
                "确认重启光猫？\n\n设备会断开 1~3 分钟，期间无法访问。\n"
                "重启也会清掉所有网页会话和 telnet 锁定。"):
            return
        try:
            self._save_from_ui()
            flash = self._load_flash()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"初始化失败：{e}")
            return
        self.log_line(f"\n{'=' * 56}\n[*] 开始：重启光猫\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, {"mode": "backup", "reboot": True,
                                            "enable_telnet": False,
                                            "enable_ftp": False, "yes": False,
                                            "force_backup": False})

    def on_toggle_flash(self):
        if self.allow_flash.get():
            self.btn_flash.configure(state="normal")
        else:
            self.btn_flash.configure(state="disabled")

    def on_flash(self):
        # 恢复出厂设置的提醒放在界面顶部的固定提示里（不跟刷机按钮绑定）。
        # 这里只做刷机本身该做的确认。
        try:
            self._save_from_ui()
            flash = self._load_flash()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"初始化失败：{e}")
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

        # 最终确认
        if not messagebox.askyesno(
                APP_TITLE,
                "即将刷写 U-Boot\n\n"
                f"文件：{uboot}（{size} 字节）\n"
                "目标：/dev/mtdblock0\n\n"
                "⚠️ 写入不可逆，断电或写错会导致设备变砖！\n"
                "程序会先做全分区备份，但仍请确认已备份重要数据。\n\n"
                "确定现在刷写吗？"):
            self.log_line("[*] 已取消刷机（未做任何写入）。\n")
            return

        args = {"mode": "flash", "enable_telnet": flash.AUTO_ENABLE_TELNET,
                "enable_ftp": flash.AUTO_ENABLE_FTP, "yes": True,
                "reboot": False, "force_backup": False}
        self.log_line(f"\n{'=' * 56}\n[!] 开始刷写 U-Boot：{uboot}\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)


def self_test():
    """检查运行环境：flash.py 能否加载、依赖是否齐全。

    打包成 exe 后 flash.py 是运行时动态加载的，PyInstaller 看不到它的 import，
    漏掉某个模块要等真正用到处才炸（曾漏掉 http.server，备份时才报错）。
    这里把所有依赖提前导一遍，早暴露问题。

    命令行运行：`XG-040G-MD工具.exe --selftest`（GUI 版也能跑，会弹窗显示）
    """
    problems = []
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
            print(f"  [✓] flash.py 加载成功（{len(mod.MTD_PARTITIONS)} 个分区定义）")
        except Exception as e:
            problems.append(f"flash.py 加载失败: {type(e).__name__}: {e}")
            print(f"  [✗] flash.py — {type(e).__name__}: {e}")

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
        # 只在**失败**时弹窗：成功还弹会挡住自动化调用（踩过）
        if problems:
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
