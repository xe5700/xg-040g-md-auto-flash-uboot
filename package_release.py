#!/usr/bin/env python3
"""把 exe、tcboot.bin、an758x-stock2ubi 打包成 zip，并生成可直接上传 Release 的附件。

为什么要这个脚本（而不是在 CI 的 YAML 里内联一段 python）：
  - CI 和 build_exe.sh 都调用它，本地和线上产出一致，不会各写一份逻辑；
  - 用 Python 的 zipfile 直接读写最稳，不受 YAML 块标量 + shell 引号影响。

产出（都在 dist 目录下）：
  1. dist/xe-pon-flash-toolkit-v<版本>.zip
     解压即用的完整包：exe + tcboot.bin + an758x-stock2ubi + uboot-bin/。
     uboot-bin/ 是 uboot_release.py 下好的 U-Boot 合集（修改版 / 官方版 /
     loong1996 三个子目录），界面的「自动查找」就在 exe 同级的这个目录里找
     FIP，所以跟着 zip 一起发，用户不用自己再去上游 Release 里翻。
  2. dist/release/ —— 专门给 GitHub Release 用的副本，文件名带版本号：
       xe-pon-flash-toolkit-v<版本>.exe
       xe-pon-flash-toolkit-v<版本>.zip
       SHA256SUMS.txt

为什么文件名全用 ASCII：GitHub 上传 Release 附件时会**直接删掉文件名里的
非 ASCII 字符**，附件名和我们写进 SHA256SUMS.txt 的名字就会对不上，用户
下载后跑 `sha256sum -c` 必然报「没有那个文件或目录」。所以干脆自己先把
上传用的名字定成 ASCII，不去依赖 GitHub 的隐式清洗。

为什么带版本号：Release 页面上会同时躺着多个 tag 的附件，用户从下载目录
里翻出几个同名文件根本分不清哪个是哪个。带版本号后文件名本身就是标识。

an758x-stock2ubi 的来源：直接从 pbs05/an758x-stock2ubi 的 GitHub Release
取最新版（用 GitHub API 查 latest release，再下同名 asset）。不把它提交进
本仓库 —— 它是别人维护的 aarch64 静态 ELF，跟着上游走才不会过期。
网络失败时回退到仓库根目录的本地副本（如果存在），两者都没有就跳过它，
不让发版挂掉。
"""

import glob
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile

from version import VERSION

# 项目产物名（ASCII，不带版本号的部分）
TOOLKIT = "xe-pon-flash-toolkit"

# 上游 an758x-stock2ubi 的 Release 仓库与 asset 名
STOCK2UBI_REPO = "pbs05/an758x-stock2ubi"
STOCK2UBI_ASSET = "an758x-stock2ubi"
STOCK2UBI_LOCAL = "an758x-stock2ubi"

# 上游 U-Boot 合集：由 uboot_release.py 从三个上游仓库下到仓库根的 uboot-bin/，
# 这里原样进 zip。为什么要带上：界面的「自动查找」就是按 exe 同级的 uboot-bin/
# 找 FIP 的，解压即用才不用用户自己再去 GitHub Release 里翻 —— 翻错一份、
# 尤其是华邦机型拿错了引导，就是一次变砖。
UBOOT_DIR = "uboot-bin"

RELEASE_DIR = "release"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _gh_headers():
    """GitHub 请求头。

    有 GITHUB_TOKEN / GH_TOKEN 就带上 —— CI 里 github.token 的额度是
    5000 次/小时，匿名只有 60 次/小时，被 403 rate limit 挡过（实测）。
    本地跑没有 token 也照样能用，只是额度小，偶尔被限流时重跑一次即可。
    """
    h = {"User-Agent": TOOLKIT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        h["Authorization"] = "Bearer %s" % token
    return h


def _download(url, dest, timeout=120):
    """下载到 dest，返回 (sha256, 字节数)。失败抛异常。

    这里**故意不带 token**：release asset 的 browser_download_url 会 302 到
    objects.githubusercontent.com，urllib 跟随重定向时会把 Authorization
    头一起带过去 —— 等于把 token 送给另一个域名。asset 本身是公开的，
    不带认证也能下，没必要冒这个险。
    """
    req = urllib.request.Request(url, headers={"User-Agent": TOOLKIT})
    h = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def fetch_stock2ubi(dist):
    """取 an758x-stock2ubi，返回本地路径；拿不到返回 None。

    顺序：
      1. GitHub API 查 latest release → 下同名 asset
      2. 回退到 dist / 仓库根目录里已有的本地副本
    """
    api = "https://api.github.com/repos/%s/releases/latest" % STOCK2UBI_REPO
    try:
        req = urllib.request.Request(api, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.load(r)
        tag = rel.get("tag_name", "?")
        url = None
        for a in rel.get("assets") or []:
            if a.get("name") == STOCK2UBI_ASSET:
                url = a.get("browser_download_url")
                break
        if not url:
            print(
                "警告：%s 的 %s 里没有名为 %s 的附件，改用本地副本"
                % (STOCK2UBI_REPO, tag, STOCK2UBI_ASSET),
                file=sys.stderr,
            )
        else:
            dest = os.path.join(dist, STOCK2UBI_ASSET)
            digest, size = _download(url, dest)
            print("已从 %s %s 下载 %s（%d 字节）" % (STOCK2UBI_REPO, tag, STOCK2UBI_ASSET, size))
            print("  sha256 %s" % digest)
            return dest
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print("警告：从 GitHub 取 %s 失败（%s），改用本地副本" % (STOCK2UBI_ASSET, e), file=sys.stderr)

    # 回退：dist 里的优先，其次仓库根目录
    for cand in (os.path.join(dist, STOCK2UBI_LOCAL), STOCK2UBI_LOCAL):
        if os.path.isfile(cand):
            print("使用本地副本 %s（sha256 %s）" % (cand, _sha256(cand)))
            return cand
    print("警告：找不到 %s，zip 里将不含它" % STOCK2UBI_LOCAL, file=sys.stderr)
    return None


def collect_uboot(root=UBOOT_DIR):
    """收集 uboot-bin/ 下所有要进 zip 的文件，返回 [(绝对路径, zip 内路径)]。

    递归：修改版 / 官方版 / loong1996 是三个子目录，loong1996 下面还有一层机型。
    目录本身也保留（arcname 带着相对路径），用户解压后能一眼看出哪份是哪份。

    目录不存在只警告不报错 —— 本地跑 `build_exe.sh` 时未必先下过 U-Boot，
    没下过就打一个不含引导的包，不该因此发不了版。
    """
    out = []
    if not os.path.isdir(root):
        print("警告：没有 %s 目录，zip 里将不含 U-Boot 合集"
              "（先跑 python uboot_release.py）" % root, file=sys.stderr)
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        # 排序 + 跳过 __pycache__：让 zip 里的顺序稳定，两次构建结果可比对
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if fn.startswith("."):      # 隐藏文件（本地的 .gitignore 之类）不进去
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            out.append((full, os.path.join(root, rel)))
    return out


def main(argv):
    dist = argv[1] if len(argv) > 1 else "dist"

    # dist 里的 exe 是 PyInstaller 的输出（不带版本号），用 glob 拿，别写死。
    exes = sorted(glob.glob(os.path.join(dist, "*.exe")))
    if not exes:
        print("错误：%s 里没有 .exe" % dist, file=sys.stderr)
        return 1
    if len(exes) > 1:
        print("错误：%s 里有多个 exe，不知道该打哪个：%s" % (dist, exes), file=sys.stderr)
        return 1
    exe = exes[0]

    # tcboot.bin 优先用 dist 里的（可能被换成了别的版本），否则退回仓库根目录的。
    # exe 自己内置了一份，但同目录的文件优先级更高 —— 用户想换 uboot 时
    # 直接替换 zip 里的这个文件即可，不用重新打包 exe。
    tcboot = os.path.join(dist, "tcboot.bin")
    if not os.path.isfile(tcboot):
        tcboot = "tcboot.bin"
    if not os.path.isfile(tcboot):
        print("错误：找不到 tcboot.bin", file=sys.stderr)
        return 1

    stock2ubi = fetch_stock2ubi(dist)
    uboots = collect_uboot()

    # 本地用的完整包：解压即用，文件名带版本号
    zip_name = "%s-v%s.zip" % (TOOLKIT, VERSION)
    out = os.path.join(dist, zip_name)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(exe, os.path.basename(exe))
        z.write(tcboot, "tcboot.bin")
        if stock2ubi:
            # 用标准名写进 zip，不带上游 tag —— flash.py 就是按这个名字找的
            z.write(stock2ubi, STOCK2UBI_LOCAL)
        for full, arc in uboots:
            z.write(full, arc)

    print("已生成 %s（%d 字节）" % (out, os.path.getsize(out)))
    with zipfile.ZipFile(out) as z:
        # U-Boot 合集有十几个文件，全列出来会把日志刷屏，只报数量和小计
        for info in z.infolist():
            if info.filename.startswith(UBOOT_DIR + "/"):
                continue
            print("  包含 %s（%d 字节）" % (info.filename, info.file_size))
        if uboots:
            n_bytes = sum(os.path.getsize(f) for f, _ in uboots)
            print("  包含 %s/ 下 %d 个 U-Boot 文件（压缩前 %d 字节）"
                  % (UBOOT_DIR, len(uboots), n_bytes))
        else:
            print("  （不含 U-Boot 合集）")

    # 给 Release 用的 ASCII 名副本，带版本号。
    # 先整个清掉，免得上次的残留被 CI 的 dist/release/* 一起传上去。
    rel = os.path.join(dist, RELEASE_DIR)
    if os.path.isdir(rel):
        shutil.rmtree(rel)
    os.makedirs(rel)

    rel_exe_name = "%s-v%s.exe" % (TOOLKIT, VERSION)
    rel_zip_name = "%s-v%s.zip" % (TOOLKIT, VERSION)
    rel_exe = os.path.join(rel, rel_exe_name)
    rel_zip = os.path.join(rel, rel_zip_name)
    shutil.copyfile(exe, rel_exe)
    shutil.copyfile(out, rel_zip)

    # SHA256SUMS.txt 里写的必须是**上传后的附件名**，也就是上面这两个名字。
    # 两个空格是 `sha256sum -c` 要求的文本模式分隔符，别改成单个空格。
    names = [rel_exe_name, rel_zip_name]
    sums = ["%s  %s" % (_sha256(os.path.join(rel, n)), n) for n in names]
    sums_path = os.path.join(rel, "SHA256SUMS.txt")
    with open(sums_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sums) + "\n")

    print("已生成 Release 附件目录 %s：" % rel)
    for line in sums:
        h, name = line.split("  ")
        print("  %-34s %10d 字节  sha256 %s" % (name, os.path.getsize(os.path.join(rel, name)), h))
    print("  %-34s %10d 字节" % ("SHA256SUMS.txt", os.path.getsize(sums_path)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
