#!/usr/bin/env python3
"""从三个上游仓库下载 an758x 系列 U-Boot，按用途分目录摆好，供 package_release.py 打进 zip。

为什么要有这个脚本
------------------
本仓库只维护刷机工具，U-Boot 本身是别人在维护。以前是让用户自己去 GitHub
Release 里翻，翻错一份刷进去就是砖 —— 华邦颗粒那批尤其危险（原厂按 8bit ECC
写、新 BL2 按 4bit 读，读出来全是坏块）。现在发版时把需要的 U-Boot 一起下好、
按「先刷哪份、再刷哪份」分目录放进 zip，用户拿到手不用再挑。

三个上游
--------
  uboot-bin/修改版/       xe5700/uboot-an758x-winbond
      「临时引导」。针对华邦 W29N02KV 的 8bit 读 / 4bit 写，先把机器从死局里
      拉起来。**它只是过渡，进去之后必须再刷官方版。** 只有 HG5585F / HG5382A 有。
  uboot-bin/官方版/       pbs05/uboot-an758x
      上游正式发布的引导，长期使用的那份。
  uboot-bin/loong1996/    Loong1996/ImmortalWrt-Airoha
      ImmortalWrt 自己的 OpenWrt U-Boot（带网页救砖界面），上游按机型分开
      发 release（tag 形如 XG-040G-MD-ubi-auto-<日期>-<号>），所以要按机型找。
      跟上面两份不是一套东西，别混着刷。

下载哪几个机型由下面的 MODELS 决定，加机型改那一个列表即可。

用法
----
    python3 uboot_release.py [输出目录=uboot-bin] [--force] [--allow-partial]

本地要过代理：
    proxychains4 -q python3 uboot_release.py

网络失败时的取舍：单个文件下不动只警告 + 记账（写进 MANIFEST.txt），
但只要**某个机型在官方版里一个文件都没下到**，就整体失败退出 —— 发一个
缺机型的包比不发更糟，用户会以为「这包里有我的机型」。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 复用 package_release.py 里的下载/哈希/请求头实现，别在这再写一份
from package_release import _download, _gh_headers, _sha256
from version import VERSION

# ── 上游仓库 ────────────────────────────────────────────────────────────
REPO_MODIFIED = "xe5700/uboot-an758x-winbond"
REPO_OFFICIAL = "pbs05/uboot-an758x"
REPO_LOONG = "Loong1996/ImmortalWrt-Airoha"

# zip 里的目录名。用中文是因为用户是照着这几行去翻文件的，
# 「官方版」「修改版」比 official/modified 一眼就分得清。
# 这些只是 zip 内部的路径，不是 Release 附件名，不受 GitHub 删非 ASCII 的影响。
DIR_MODIFIED = "修改版"
DIR_OFFICIAL = "官方版"
DIR_LOONG = "loong1996"

# 要覆盖的机型。
#   official: pbs05/uboot-an758x 里该机型的文件名前缀。一个机型可能有多个
#             硬件变体（HG5585F 分 ct / cu，文件不通用），都列进来。
#   loong:    Loong1996/ImmortalWrt-Airoha 的 release tag 前缀；
#             None 表示上游没有这个机型（实测 HG5585F 就没有）。
MODELS = [
    {"name": "XG-040G-MD", "official": ["an7581-nokia-xg-040g-md"], "loong": "XG-040G-MD"},
    {"name": "XG-040G-MF", "official": ["an7583-nokia-xg-040g-mf"], "loong": "XG-040G-MF"},
    {"name": "XG-040G-TF", "official": ["an7581-nokia-xg-040g-tf"], "loong": "XG-040G-TF"},
    {"name": "HG5585F", "official": ["an7581-fiberhome-hg5585f-ct",
                                     "an7581-fiberhome-hg5585f-cu"],
     "loong": None},
    {"name": "HG5382A", "official": ["an7581-fiberhome-hg5382a"], "loong": "HG5382A"},
]

# 单个文件超时（二进制几十 KB ~ 几百 KB，300 秒够宽裕；
# 走代理时慢是常态，宁可等也别半途失败）
TIMEOUT = 300
RETRIES = 3


# ── GitHub API ─────────────────────────────────────────────────────────
def _api(url):
    """带 token 查 api.github.com。"""
    req = urllib.request.Request(url, headers=_gh_headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _latest_release(repo):
    """仓库的最新 Release。上游 tag 换版本号时这里自动跟着走。"""
    return _api("https://api.github.com/repos/%s/releases/latest" % repo)


def _all_releases(repo):
    """全部 Release（最多 100 个）。loong1996 按机型发 release，要自己筛。"""
    return _api("https://api.github.com/repos/%s/releases?per_page=100" % repo)


def _assets(rel):
    return rel.get("assets") or []


def _match(assets, prefixes):
    """挑出文件名以 <前缀>- 开头的附件。

    加这个 '-' 是防前缀互相吃掉：'an7581-fiberhome-hg5585f-c' 这种半截前缀
    会把 ct 和 cu 混在一起。
    """
    out = []
    for a in assets:
        name = a.get("name") or ""
        for p in prefixes:
            if name.startswith(p + "-"):
                out.append(a)
                break
    return sorted(out, key=lambda a: a["name"])


def _download_retry(url, dest):
    """下不动就重试。走代理时偶发超时很常见，别一次失败就丢掉整个文件。"""
    last = None
    for i in range(1, RETRIES + 1):
        try:
            return _download(url, dest, timeout=TIMEOUT)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
            print("      第 %d/%d 次失败：%s" % (i, RETRIES, e), file=sys.stderr)
            if i < RETRIES:
                time.sleep(3 * i)
    raise last


# ── 落盘 ───────────────────────────────────────────────────────────────
class Collector:
    """负责把附件落到 out_dir 下，并记账。"""

    def __init__(self, out_dir, force=False):
        self.out_dir = out_dir
        self.force = force
        self.entries = []
        self.failed = []
        # key = (kind, model) -> 成功落盘的文件数
        self.count = {}

    def put(self, kind, model, sub, asset, src):
        """下载一个附件到 out_dir/<sub>/<asset>。sub 可以是机型子目录。"""
        name = asset["name"]
        url = asset.get("browser_download_url")
        rel = os.path.join(kind, sub) if sub else kind
        dest = os.path.join(self.out_dir, rel, name)
        key = (kind, model)
        try:
            if name.endswith(".zip"):
                # loong1996 的 bootloader.zip 里是 preloader+fip+说明，
                # 这里不用它 —— 直接取单个附件，少一层解压，文件名也更清楚。
                return
            if os.path.isfile(dest) and not self.force:
                size = os.path.getsize(dest)
                digest = _sha256(dest)
                print("      跳过（已存在）%s（%d 字节）" % (name, size))
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                digest, size = _download_retry(url, dest)
                print("      下载 %s（%d 字节）" % (name, size))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print("      警告：%s 下载失败：%s" % (name, e), file=sys.stderr)
            self.failed.append({"kind": kind, "model": model, "asset": name,
                                "src": src, "url": url, "error": str(e)})
            return
        self.entries.append({
            "kind": kind, "model": model, "asset": name, "src": src,
            "path": os.path.relpath(dest, self.out_dir).replace(os.sep, "/"),
            "size": size, "sha256": digest,
        })
        self.count[key] = self.count.get(key, 0) + 1


# ── 各来源 ─────────────────────────────────────────────────────────────
def fetch_modified(col, force_flat_hint):
    """修改版（临时引导）。只有 HG5585F / HG5382A，其余机型上游就没有。"""
    print("── 修改版（临时引导）：%s ──" % REPO_MODIFIED)
    rel = _latest_release(REPO_MODIFIED)
    src = "%s %s" % (REPO_MODIFIED, rel.get("tag_name"))
    print("   tag: %s" % rel.get("tag_name"))
    for m in MODELS:
        got = _match(_assets(rel), m["official"])
        if not got:
            print("   [--] %s：上游没有修改版（正常，该机型不在华邦死局里）" % m["name"])
            continue
        print("   [%s]" % m["name"])
        for a in got:
            col.put(DIR_MODIFIED, m["name"], "", a, src)
    return src


def fetch_official(col):
    """官方版。"""
    print("── 官方版：%s ──" % REPO_OFFICIAL)
    rel = _latest_release(REPO_OFFICIAL)
    src = "%s %s" % (REPO_OFFICIAL, rel.get("tag_name"))
    print("   tag: %s" % rel.get("tag_name"))
    missing = []
    for m in MODELS:
        got = _match(_assets(rel), m["official"])
        if not got:
            print("   [!!] %s：上游官方版里一个文件都没匹配到！" % m["name"])
            missing.append(m["name"])
            continue
        print("   [%s] %d 个文件" % (m["name"], len(got)))
        for a in got:
            col.put(DIR_OFFICIAL, m["name"], "", a, src)
    return src, missing


def fetch_loong(col, releases):
    """loong1996 的 OpenWrt U-Boot，按机型找 tag 最新的那个 release。"""
    print("── loong1996 网页版：%s ──" % REPO_LOONG)
    for m in MODELS:
        pre = m["loong"]
        if not pre:
            print("   [--] %s：上游没有该机型（不发 release）" % m["name"])
            continue
        hit = None
        for r in releases:
            # tag 形如 XG-040G-MD-ubi-auto-20260927-59。
            # 必须带结尾的 '-'：'XG-040G-M' 这种半截前缀会把 MD / MF 混起来。
            if (r.get("tag_name") or "").startswith(pre + "-"):
                hit = r
                break  # release 列表是按时间倒序的，第一个就是最新
        if not hit:
            print("   [!!] %s：在 %d 个 release 里没找到 %s-* 的 tag"
                  % (m["name"], len(releases), pre))
            continue
        # 只要引导相关的那三个：preloader / bl31-uboot.fip / tcboot 用的 uboot。
        # 其余是固件本体（几十 MB），跟本工具无关。
        want = [a for a in _assets(hit)
                if a["name"].endswith("-preloader.bin")
                or a["name"].endswith("-bl31-uboot.fip")
                or a["name"].endswith("-tcboot-to-ubi-uboot.bin")]
        src = "%s %s" % (REPO_LOONG, hit.get("tag_name"))
        print("   [%s] tag %s，%d 个文件" % (m["name"], hit.get("tag_name"), len(want)))
        # 单独开一层机型子目录：loong1996 的文件名带 an758x 前缀但一眼看不出
        # 是哪个机型，散在一起会挑错。
        for a in sorted(want, key=lambda a: a["name"]):
            col.put(DIR_LOONG, m["name"], m["name"], a, src)


# ── 说明文件 ───────────────────────────────────────────────────────────
README = """\
an758x 系列 U-Boot 合集（由刷机工具箱自动下载整理）
======================================================
工具版本：v{ver}
生成时间：{ts}

三个目录分别是什么
------------------
  修改版/      「临时引导」，来自 {repo_mod}
      专门解决华邦（Winbond）W29N02KV 256M 颗粒的死局：原厂 BL2 和内核按
      8bit ECC 写数据，新的 BL2 却按 4bit 读，读出来全是坏块，于是卡死在
      「Stored BL31 + U-Boot FIP failed」。
      这份临时引导两头都兼容，先把机器拉起来。
      ***它只是过渡，进去之后必须马上刷官方版。***
      只有 HG5585F / HG5382A 有 —— 其它机型本来就不在这个死局里。

  官方版/      长期使用的那份，来自 {repo_off}

  loong1996/   ImmortalWrt 的 OpenWrt U-Boot（自带网页救砖界面），
               来自 {repo_loong}，按机型分目录。
               跟上面两份不是一套东西，别混着刷。

该刷哪份
--------
  HG5585F / HG5382A：
      1) 先刷 修改版/ 里对应机型的那几个文件；
      2) 进去之后立刻刷 官方版/ 里同机型的那几个文件。
      先临时、后官方，能把「刷完进不去系统、只能 TTL 救砖」的概率压到最低。

  XG-040G-MD / XG-040G-MF / XG-040G-TF：
      直接刷 官方版/ 里对应机型的那份即可。

  loong1996/ 里的只在你要装 ImmortalWrt 那一套（网页 U-Boot 布局）时才用，
      照上游刷机教程走，不要拿它去替换上面两份。

硬约束
------
  * 引导文件按机型编译，**绝对不要跨机型刷**。文件名里的机型后缀要对上。
  * HG5585F 分 ct / cu 两个硬件版本，文件不通用，看清楚自己的机器再刷。
  * preloader.bin 和 bl31-u-boot.fip 要**一起刷**：BootROM 只把 BL2 收进
    片上 SRAM，那里放不下 FIP，FIP 得由 BL2 再搬进 DRAM。
  * 刷之前先备份原厂分区（尤其 ri 出厂卷，里面有 MAC）。
  * 文件名与上游完全一致，没有改过。要核对来源看同目录的 MANIFEST.txt，
    里面有每个文件的仓库、tag、大小和 sha256。
"""

MANIFEST_HEAD = """\
# 上游清单 —— 由 uboot_release.py 生成，不要手改
# 工具版本 v{ver}    生成时间 {ts}
# 文件名与上游 Release 附件完全一致；同一文件在多个机型下出现时是同一份。
#
# 分类\t机型\t来源\t文件\t大小\tsha256
"""


def write_docs(out_dir, ok_entries, failed, ts):
    """写 说明.txt 和 MANIFEST.txt。"""
    readme = README.format(ver=VERSION, ts=ts, repo_mod=REPO_MODIFIED,
                           repo_off=REPO_OFFICIAL, repo_loong=REPO_LOONG)
    p = os.path.join(out_dir, "说明.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(readme)
    print("已写 %s" % p)

    lines = [MANIFEST_HEAD.format(ver=VERSION, ts=ts)]
    for e in sorted(ok_entries, key=lambda e: (e["kind"], e["model"], e["asset"])):
        lines.append("%s\t%s\t%s\t%s\t%d\t%s\n"
                     % (e["kind"], e["model"], e["src"], e["asset"], e["size"], e["sha256"]))
    if failed:
        lines.append("#\n# 以下文件这次没下到，包里缺（下次发版会重试）\n")
        for e in failed:
            lines.append("# 失败\t%s\t%s\t%s\t%s\n"
                         % (e["kind"], e["model"], e["asset"], e["error"].replace("\n", " ")))
    p = os.path.join(out_dir, "MANIFEST.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)
    print("已写 %s" % p)


def report_coverage(col):
    """打一张覆盖表。发布前一眼看出哪个机型缺东西。"""
    print("\n覆盖情况：")
    print("  %-14s %8s %8s %10s" % ("机型", "修改版", "官方版", "loong1996"))
    for m in MODELS:
        mine = col.count.get((DIR_MODIFIED, m["name"]), 0)
        off = col.count.get((DIR_OFFICIAL, m["name"]), 0)
        loong = col.count.get((DIR_LOONG, m["name"]), 0)
        mark = "" if m["loong"] else "  (上游没有)"
        print("  %-14s %8d %8d %10d%s" % (m["name"], mine, off, loong, mark))


def warn_flat_files(out_dir):
    """旧布局提醒。

    这个目录以前是「所有引导文件平铺在 uboot-bin/ 根下」，现在改成分子目录。
    用户本地很可能还留着平铺的那几个，界面的下拉框会把同一份文件列两遍，
    所以提醒一句（不自动删 —— 那是用户自己放的文件）。
    """
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    flat = [n for n in names
            if os.path.isfile(os.path.join(out_dir, n))
            and (n.endswith(".fip") or n.endswith(".bin"))]
    if flat:
        print("\n提示：%s 根目录下还平铺着 %d 个引导文件（旧布局）。"
              % (out_dir, len(flat)))
        print("      现在它们都在子目录里，同一份文件会被界面列出两遍，建议把平铺的删掉。")
        for n in flat:
            print("        %s" % n)


# ── 主流程 ─────────────────────────────────────────────────────────────
def main(argv):
    ap = argparse.ArgumentParser(description="下载并整理 an758x 系列 U-Boot")
    ap.add_argument("out", nargs="?", default="uboot-bin",
                    help="输出目录（默认 uboot-bin）")
    ap.add_argument("--force", action="store_true",
                    help="已存在的文件也重新下载")
    ap.add_argument("--allow-partial", action="store_true",
                    help="有机型在官方版里完全没下到也算成功（默认失败退出）")
    args = ap.parse_args(argv[1:])

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("输出目录：%s" % out_dir)
    print("工具版本：v%s\n" % VERSION)

    col = Collector(out_dir, force=args.force)

    # 两个「最新 release」的查询失败就直接退出：拿不到 tag 说明网络或 token
    # 有问题，继续下去只会打一个空包。
    fetch_modified(col, None)
    print()
    official_src, missing = fetch_official(col)
    print()
    try:
        releases = _all_releases(REPO_LOONG)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # loong1996 是可选的第三方来源，拿不到不该挡住发版 —— 警告即可。
        print("警告：取 %s 的 release 列表失败（%s），跳过 loong1996"
              % (REPO_LOONG, e), file=sys.stderr)
        releases = []
    fetch_loong(col, releases)

    # 先把说明写了，这样即使后面判定失败，用户手上也有一份能看的目录。
    write_docs(out_dir, col.entries, col.failed, ts)
    report_coverage(col)
    warn_flat_files(out_dir)

    ok = True
    if missing:
        print("\n错误：官方版里这些机型一个文件都没下到：%s" % "、".join(missing),
              file=sys.stderr)
        ok = False
    n_off = sum(1 for e in col.entries if e["kind"] == DIR_OFFICIAL)
    n_mod = sum(1 for e in col.entries if e["kind"] == DIR_MODIFIED)
    if not n_off:
        print("\n错误：官方版一个文件都没有，包是空的", file=sys.stderr)
        ok = False
    if not n_mod:
        print("警告：修改版一个文件都没有（HG5585F/HG5382A 的临时引导会缺）",
              file=sys.stderr)
    if col.failed:
        print("警告：有 %d 个文件没下到，详见 MANIFEST.txt" % len(col.failed),
              file=sys.stderr)

    if not ok and not args.allow_partial:
        return 1
    total = sum(e["size"] for e in col.entries)
    print("\n共 %d 个文件，%d 字节（约 %.1f MB）"
          % (len(col.entries), total, total / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))