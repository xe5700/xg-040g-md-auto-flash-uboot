#!/usr/bin/env python3
"""版本号的**唯一来源**。

改版本号不要手改这里 —— 用 `./release.sh` 自动改（它会同时提交并打 tag）。
其它地方一律 `from version import VERSION` 读取，避免多处不一致。

版本号规则：语义化版本 MAJOR.MINOR.PATCH
  - MAJOR：不兼容的大改（例如配置项含义变化、刷机流程变更）
  - MINOR：加功能，向后兼容
  - PATCH：修 bug，向后兼容
"""

# 注意：仓库里还有一个手工发布的旧 Release「V2.0」（大写 V，tag 指向 9b14035，
# 那个提交里还没有 version.py）。所以版本号从 2.1.0 起步，避免新版本反而比它旧。
VERSION = "2.4.2"

# GitHub 仓库（release.sh 用来拼 release 页面地址，Actions 里也用）
REPO = "xe5700/xg-040g-md-auto-flash-uboot"


def version_tuple():
    """把 '1.2.3' 拆成 (1, 2, 3)；带后缀（如 1.2.3-beta.1）时后缀被忽略。"""
    core = VERSION.split("-", 1)[0].split("+", 1)[0]
    parts = []
    for p in core.split(".")[:3]:
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def version_tag():
    """git tag 名，带 v 前缀。"""
    return f"v{VERSION}"


if __name__ == "__main__":
    print(VERSION)
