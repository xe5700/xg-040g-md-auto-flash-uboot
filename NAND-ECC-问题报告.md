# HG5585F / HG5382A Winbond 256M 闪存机型刷 U-Boot 变砖 —— 根因与解法

> **根因（已实测确认）**：uboot-an758x 从 **v2026.07-3** 起，把
> Winbond `W29N02KVSIAF` 的 `soc_ecc_ability` 从 **8 bit 改成 4 bit**。
> 而**厂商固件整条链都是 8 bit** —— 厂商内核自己的并行 NAND 表、以及厂商
> BL2 的表，两侧都是 8 bit（两份表都在本文第三节里解出来了）。
>
> stock2ubi 刷机路径是「**厂商内核写**（8 bit）→ **新 BL2 读**（4 bit）」，
> 两边错位，BL2 扫不到 UBI 里的 `fip` 卷，于是：
> `ERROR: Stored BL31 + U-Boot FIP failed (-2)` → 卡 XMODEM。
>
> **为什么 TTL + XMODEM 反而正常**：那条路径里写和读**都是 BL2 自己**
> （4 bit），全程自洽。这不是反例，是对根因最强的旁证。见 3.4。
>
> **已确认的解法（2026-10-24 更新）**：上游已经出了适配**所有**颗粒的
> 「修改版」引导（`xe5700/uboot-an758x-winbond`，文件名带
> `-tmp-8bit-read-4bit-write`，语义是「**8bit 读 / 4bit 写**」）。
> Winbond 机型的正确姿势是 **先刷 `uboot-bin/修改版/` 临时引导 →
> 进系统后立刻换刷 `uboot-bin/官方版/`**；**TTL 救砖 tab 则反过来，
> 只送官方版**。见第六节路线 A / A2，以及 5.4 节的合集目录。
>
> 早先那条「Winbond 只能走 XMODEM、不要走 stock2ubi」的结论仍然成立，
> 只是现在多了一条不用拆机能进系统的临时引导；XMODEM 保留为救砖兜底
> （那也是唯一有实机成功记录的路径，上游 issue #22）。
>
> **为什么不走「把整条链统一到 8 bit」**：只改 preloader 是不够的 ——
> 那只会把砖挪到下一次重启（见 3.6）。修改版用「8bit 读」把这个断层绕开了。
>
> 影响机型：**HG5585F CT、HG5382A**（都是 Winbond 颗粒批次）。

---

## 一、现象

- 同一台 HG5585F，**Toshiba 颗粒的正常，Winbond 颗粒的刷完必砖**。
- 砖的表现：重启后 BL2 打印
  `ERROR: Stored BL31 + U-Boot FIP failed (-2)`，
  然后 `Press x to load ... via XMODEM`，只能 TTL 救。
- 群里 Winbond 机型**全军覆没**，说明是系统性问题，不是个别主板。

---

## 二、两颗颗粒的几何参数是一样的

| | 厂商 | mfr:dev:ext | 页大小 | OOB | 块大小 | 容量 |
|---|---|---|---|---|---|---|
| `TC58NVG1S3HTAI0` | Toshiba | `98:DA:761590` | 2048 | 64 | 0x20000 | 256 MiB |
| `W29N02KVSIAF` | Winbond | `EF:DA:069510` | 2048 | 64 | 0x20000 | 256 MiB |

**完全同构**。区别只在 ECC：出厂固件按 **8 bit/512B** 的 OOB 排布写入整片闪存。

---

## 三、根因：v2026.07-3 改错了 ECC 强度

### 3.1 preloader 里的闪存表

`preloader.bin` 是个容器，里面第 2 个 LZMA 流是 BL2，BL2 里有一张并行 NAND 表。
实测四个 release 的解码结果（表项字段偏移：`+0x34` ext_id、`+0x3c` min_ecc_req、
`+0x42` soc_ecc_ability）：

| 型号 | v2026.07-1 | v2026.07-2 | v2026.07-3 | v2026.07-4 |
|---|---|---|---|---|
| `TC58NVG1S3HTAI0`（Toshiba） | 8 | 8 | 8 | 8 |
| `W29N02KVSIAF`（Winbond） | **8** | **8** | **4** ❌ | **4** ❌ |

`v2` 和 `v4` 对同一颗 Winbond 的差别**只有这一个字节**（`0x42`：`08` → `04`）
外加名字指针的两个字节。

> 顺带一提，这张表里 Winbond 的 `min_ecc_req` 一直是 **4**（没变过），
> 变的是 `soc_ecc_ability`（控制器实际使用的强度）。所以不是「芯片要求变了」，
> 是「有人把控制器设置改小了」。

### 3.1b 厂商内核自己的表也是 8 bit（关键实证）

光看 BL2 还不够 —— 真正**往闪存里写**的是厂商内核（`an758x-stock2ubi`
就跑在厂商 Linux 上）。在 `_src/vendor_kernel.bin` 里找到了它自己的并行
NAND 表（偏移 `0x9cf494` 起，表项步长 `0x68`，`min_ecc_req` 在 `+0x08`、
`addr_cycle` 在 `+0x09`、`soc_ecc_ability` 在 `+0x0e`）：

```
offset     ext_id     min_ecc  soc_ecc   型号
0x9cf494   0x589591   1        4
0x9cf4fc   0x069510   4        8         W29N02KVSIAF   ← 就是它
0x9cf564   0x049580   4        4
0x9cf5cc   0x069590   8        12
0x9cf634   0x64a690   8        12
0x9cf69c   0x762691   8        12
0x9cf704   0x761590   8        8         TC58NVG1S3HTAI0
0x9cf76c   0x469590   4        8         S34ML02H200TFI000
0x9cf7d4   0x469500   4        8         S34ML02G300TFI00
```

**厂商内核给 `W29N02KVSIAF` 用的也是 8 bit。** 而且这张表跟厂商 BL2 那张
在**四颗可交叉核对的芯片上 `min_ecc_req` / `soc_ecc_ability` 全部一致**
（`W29N02KVSIAF` 4/8、`TC58NVG1S3HTAI0` 8/8、`S34ML02H200TFI000` 4/8、
`S34ML02G300TFI00` 4/8）—— 两侧互证，不是巧合。

所以「写的一方是 8 bit」现在是**实测**而不是推断了：

| | 厂商 BL2（读） | 厂商内核（写） | uboot-an758x v3/v4 BL2（读） |
|---|---|---|---|
| `W29N02KVSIAF` | **8** | **8** | **4** ❌ |

### 3.2 上游的两次提交

| 提交 | 时间 | 内容 |
|---|---|---|
| `4b7a0b6e` | 2026-09-21 14:58 | `Fix W29N02KV ECC requirement and add W29N02GV entry` —— 依据 Winbond AN0000025 与 ONFI 参数页 byte 112 = `04h`，把 `NAND_ECC_INFO` 从 `(8, SZ_512)` 改成 `(4, SZ_512)` |
| `6e646263` | v2026.07-3 附近 | `changed min_ecc_req to 4 and left soc_ecc_ability at 8` |

也就是说：上游先按数据手册把「Winbond 这颗芯片最大支持 4 bit」写进表里
（**这是芯片的能力上限，不是出厂固件的排布**），随后又有人把
`soc_ecc_ability` 一起改成了 4。第二步才是致命的。

issue #10 的原话：

> Commit 6e646263 changed `min_ecc_req` to 4 and left `soc_ecc_ability` at 8.
> The correct values are `min_ecc_req = 1` … and `soc_ecc_ability = 4`

上游自己也承认这里改串了 —— 但**改成 4 同样不对**，因为出厂固件是 8 bit 排布。

### 3.3 为什么「读 4 bit 的数据」会卡在 fip

`tf_par.c` 的逻辑是：

```c
if (result->soc_ecc_ability < result->min_ecc_req)
    ERROR("parallel NAND: ECC%d is below device requirement ECC%d", ...);
...
ecc.encode_block_size = nfi.fdm_ecc_num + 512;   /* tf_par.c:509 */
```

ECC 强度和 OOB 排布是**绑死**的。用 4 bit 解 8 bit 写的数据，OOB 里校验码的
位置和长度都对不上 → 大量 `-EBADMSG` → BL2 扫不到 UBI 里的 `fip` 卷 →
`Stored BL31 + U-Boot FIP failed (-2)` → 进 XMODEM。

issue #9 里同机型（HG5585F-CU）的日志是同一类问题的镜像版本：

```
mtk-nand 1fa11000.nand-controller: eccsize 512 eccstrength 4
Writing 8 bit/512B pages that Linux reads at 4 bit/512B made
ubi_attach() fail with -74 (EBADMSG)
```

**方向相反、结果一样**：写和读的 ECC 强度必须一致，否则 UBI 挂不上。

---

### 3.4 为什么「TTL + XMODEM 刷入是正常的」恰好证明了根因

用户反馈：同一台 Winbond 机器，**工具刷会砖，但 TTL 用 XMODEM 刷就正常**。
这条观察不是反例，而是**对根因最强的一条旁证**。区别在于「谁在写」：

| | 写 flash 的是谁 | 用的是谁的 ECC | 读 flash 的是谁 | 结果 |
|---|---|---|---|---|
| **XMODEM 救砖** | BL2 自己（在 RAM 里跑） | BL2 的 4 bit | 同一个 BL2，4 bit | **自洽 → 正常** |
| **stock2ubi 工具** | **厂商内核**（跑在厂商 Linux 上） | **8 bit** | 新的 v3/v4 BL2，4 bit | **错位 → 必砖** |

XMODEM 路径里**写和读是同一个 BL2**，它用几 bit 都自洽，所以怎么刷都行 ——
这正好解释了「为什么救砖能救回来，但工具刷就砖」。

而 stock2ubi 路径里写读双方是**两个不同的软件**：厂商内核（8 bit）和
新 BL2（4 bit）。**只要这两者不一致就必砖**，跟 BL2 自己选 4 还是 8 无关。

这也意味着一个重要推论：

> **ECC 强度本身没有「对错」，只有「一致不一致」。**
> uboot-an758x 选 4 bit 在它自己的生态里（BL2 写、BL2 读、U-Boot 写读）
> 是自洽的；它唯一撞上的就是「厂商内核按 8 bit 写的那片闪存」。
> 所以修法是要么让 BL2 回到 8 bit 去适配厂商内核（本报告的补丁），
> 要么绕开厂商内核、让 BL2/U-Boot 自己写（XMODEM 路径就是这样）。

### 3.5 真实案例：issue #22（HG5382A，同样 Winbond）

上游 issue #22 记录了一台 HG5382A（**同样的 Winbond 颗粒、同样的现象**）：

> 按流程指引到刷入 `*-preloader.bin` 或 `*-firstblock.bin`，还有
> `bl31-u-boot.fip`，点击刷入显示绿色刷入成功后无法启动，电源指示灯不亮，
> 无法进入 uboot，网卡可以正常连接无分配 ip，手动分配 ip 无效。

pbs05（项目作者）的回复是：**「华邦机型不能用这个，不兼容」**。

而报告者后来自己救回来了：

> 在 Claude 指导下，TTL 提示 `Press x to load BL31 + U-Boot FIP via XMODEM`。
> 按 x，然后用 XMODEM 发送 `bl31-u-boot.fip`，成功启动 uboot，
> 正常刷入 openwrt。**重启正常**。

**从这条记录能推出一个我之前不敢下结论的关键事实：**

BL2 提示「Press x to load BL31 + U-Boot FIP via XMODEM」说明
**BL2 本身跑起来了**（它是被 BootROM 从闪存载入 RAM 执行的），
它只是**读不出** `firstblock` 里那个 UBI 卷中的 FIP —— 和本报告的机理完全一致。

然后报告者把 FIP 通过串口送进 RAM 启动 U-Boot，U-Boot 重新刷入 OpenWrt，
**「重启正常」**。重启后读闪存的是 **BL2（4 bit）**，而闪存里的 UBI 是
**U-Boot 写的** —— BL2 能正常读出来，就说明：

> **U-Boot 也是 4 bit。整个 uboot-an758x 栈（BL2 + U-Boot）统一在 4 bit。**

于是问题变成了一个**三方一致性问题**：

| 软件 | `W29N02KVSIAF` 用几 bit | 谁写的闪存 |
|---|---|---|
| 厂商内核 + 厂商 BL2 | **8** | 厂商内核写（stock2ubi 路径） |
| uboot-an758x v3/v4 的 BL2 | **4** | —— |
| uboot-an758x v3/v4 的 U-Boot | **4**（由 #22 反推） | U-Boot 写（XMODEM 后的 OpenWrt） |

- **XMODEM 路径**：写和读**全在 4 bit 阵营内部** → 自洽 → 正常 ✓
- **stock2ubi 路径**：厂商内核按 8 bit 写 → BL2 按 4 bit 读 → **必砖** ✗

### 3.6 由此得到的推论：只补 preloader 是不够的

这一点修正了本报告早先的说法：

> **ECC 强度必须在「所有碰这片闪存的软件」之间完全一致，改一层不够。**

如果只把 BL2 改成 8 bit：

1. BL2（8）读厂商内核写的 bootstrap UBI（8）→ ✓ 能读出 FIP，U-Boot 起来了；
2. 但 U-Boot（4）接着去读同一片 UBI（8）→ ✗ 在 U-Boot 阶段挂；
3. 就算 U-Boot 不读旧数据、直接重新刷机，它**是按 4 bit 写的** ——
   于是下一次重启时，**已经被改成 8 bit 的 BL2 又读不了 4 bit 的数据** → 再次变砖。

也就是说，单纯补 preloader 只是**把砖挪了个位置**，甚至会变成「刷完能进 U-Boot
但一重启就砖」，比现在更难排查。

**要真正走通 stock2ubi 路径，必须把整条链（BL2 + U-Boot + 之后读 UBI 的
OpenWrt 内核）都统一到 8 bit**，去适配厂商内核写入的那片闪存。
这需要 `bl31-u-boot.fip`（以及 OpenWrt 那边对应的设置），目前我手上没有这些文件。

## 四、为什么「用 v2026.07-2」不是完整答案

v2026.07-4 带了一个 v1/v2/v3 都**没有**的修复：

```
tf-a: skip FIP preload when UBI owns FIP, add web NAND scrub button
（2026-09-23 12:33）
```

特征是 BL2 里多出字符串 `FIP source: UBI volume`。v1/v2/v3 里搜不到这个串。

这条修复针对的是「FIP 已经在 UBI 卷里、boot 分区窗口预读失败就直接进 XMODEM」
的路径。也就是说：

| | ECC 正确 | FIP 预读修复 |
|---|---|---|
| v2026.07-1 / -2 | ✅ 8 bit | ❌ 没有 |
| v2026.07-3 / -4 | ❌ 4 bit | ✅ 有 |

**没有任何一个现成版本同时满足两点** —— 这才是真正需要打补丁的原因。

> ~~好消息：ECC 正确时预读本来就能成功，这条修复就不是必需的。
> 所以对 Winbond 机型，v2026.07-2 是可用的、零风险的选择。~~
>
> **上面这句已被 3.5 / 3.6 推翻。** v2 的 **BL2** 确实是 8 bit，但
> 「BL2 是 8 bit」并不等于「v2 整套能配合厂商内核」：链条上还有 U-Boot。
> 如果 v2 的 U-Boot 已经是 4 bit（U-Boot 侧那次改动
> `4b7a0b6e` 是 2026-09-21，很可能早于 v2），那 v2 的 **BL2(8) 和
> U-Boot(4) 自己就打架**，同样走不通。
>
> 所以 **v2026.07-2 不再作为推荐方案** —— 除非能确认它整套（含 FIP）
> 都是 8 bit。这需要拿到 v2 的 `bl31-u-boot.fip` 来核对。

---

## 五、工具改动

### 5.1 新增 `nandcheck.py`

| 函数 | 作用 |
|---|---|
| `parse_preloader(path)` | 解开容器，定位 BL2 流，解析闪存表 |
| `find_entry(info, mfr, dev, ext)` | 按 ID 取某个型号的表项 |
| `winbond_ecc_verdict(info)` | 判定这份 preloader 对 Winbond 是否安全 |
| `detect_nand_chip(run_cmd)` | 从设备的 `dmesg` / `/sys/class/mtd` 读颗粒型号 |
| `chip_is_winbond_ecc_trap(chip)` | 判断本机是不是踩坑颗粒 |
| `preloader_safe_for_chip(path, chip)` | 颗粒 × preloader 的最终判定 |
| `patch_preloader_ecc(src, dst, ecc=8)` | **把 ECC 字段改回 8** |

用法：

```bash
# 只看结论
python3 nandcheck.py preloader.bin

# 检查 + 自动补好（原文件不动，另存一份）
python3 nandcheck.py preloader.bin --fix _out/preloader_ecc8.bin
```

四个 release 的实测判定：

```
pre_v2026.07-1.bin       [OK]
pre_v2026.07-2.bin       [OK]
pre_v2026.07-3.bin       [危险]
pre_ct.bin   (v2026.07-4)[危险]
```

### 5.2 补丁为什么是安全的

patch 的做法是**原地改一个字节 + 重新压缩 + 补零**，不是重新打包：

```
已把 W29N02KVSIAF 的 soc_ecc_ability 从 4 改成 8。
    压缩流 0x1037a：45772/45807 字节（余 35）
    其它流与末尾签名区未改动。
```

回读校验（对 v2026.07-4 实测）：

```
size equal: True                      115712
header 0x00..0x400 identical: True
trailing 0x1bc00..end identical: True
stream1 identical: True
stream3 identical: True
stream 1 @0x3c24    usize=98924  end=0x1037a ok
stream 2 @0x1037a   usize=78532  end=0x1b646 ok    ← 唯一改动的流
stream 3 @0x1b669   usize=20204  end=0x1ba5a ok
```

关键是**补零保住下游偏移**：新压缩流比原来短 35 字节，用 `\x00` 补齐到原长度，
所以第 3 个流仍在 `0x1b669`，末尾签名区仍在 `0x1bc00`，一个字节都没移位。

### 5.3 关于「签名会不会校验失败」

容器尾部 `0x1bc00` 是一段 DER（`30 82 07 3a`），每个 release 内容都不同。
我判断**它不参与启动校验**，依据有三条：

1. 容器头 `0x10` / `0x40` 那两段 16 字节，在**四个 payload 完全不同的版本里
   字节完全相同**（`5ff9ec0b4d223e4d` / `8d8c9fbabe9956a5`）—— 说明它们不是
   对 payload 的完整性摘要。
2. 容器头的 `bl2_crc` 是 `0x12345678`，一个明显的占位值 —— 说明这个容器
   本身不走 CRC 强校验。
3. 社区自编译的 preloader 能在 Toshiba 机器上正常启动。如果 BootROM 强制
   校验厂商签名，社区构建根本进不去。

**但这一条我无法在没有 winbond 实机的情况下最终确认，必须实机验证。**

### 5.4 新增 `uboot_release.py`：把三家的引导合集一起发

光有「该用哪份引导」的结论不够 —— 用户手里得有那份文件，而且不能拿错。
`uboot_release.py`（约 340 行）在发版时把三家的产物拉下来整理成固定目录：

| 目录 | 来源 | 说明 |
|---|---|---|
| `uboot-bin/修改版/` | `xe5700/uboot-an758x-winbond` | `-tmp-8bit-read-4bit-write`，8bit 读 / 4bit 写 |
| `uboot-bin/官方版/` | `pbs05/uboot-an758x` | 官方 preloader / firstblock / FIP |
| `uboot-bin/loong1996/<机型>/` | `Loong1996/ImmortalWrt-Airoha` | 上游按机型分 release 的 web U-Boot |

同时写 `uboot-bin/说明.txt`（给用户看的用哪份）和 `uboot-bin/MANIFEST.txt`
（记录每份的来源 tag，方便对账）。实测 36 个文件 / 10.2 MB，覆盖
XG-040G-MD / MF / TF / HG5585F / HG5382A；**上游没有 HG5585F 的 loong1996 包**。
CI（`.github/workflows/build.yml` 第 9 步）在打包前跑它，
`package_release.py` 把整棵树塞进 zip。`uboot-bin/` 已 gitignore。

> 之前用户手工放在 `uboot-bin/` 根目录的 6 个文件里，有 3 个的 sha256 与
> 上游 release 里的同名文件**对不上**（临时版 FIP 是 330752 字节，而上游的
> 两份 release 都是 331776）—— 那批是 10-24 凌晨、release 发布之前拿到的
> 旧构建。已挪到 `_out/uboot-bin-flat-stale/` 留档，不参与打包。

---

## 六、可行的路线（按可靠程度排序）

### 路线 A（现在推荐，2026-10-24 更新）：用上游的「修改版」引导

上游（`xe5700/uboot-an758x-winbond`，tag `v2026.07-4-winbond`）已经出了
适配**所有**颗粒的修改版引导，文件名带 `-tmp-8bit-read-4bit-write`，
语义是「**8bit 读 / 4bit 写**」：读的时候按原厂那套 8 bit 排布解，写下去的
是新的 4 bit 排布 —— 正好把 3.6 说的那个断层补上（读旧的能懂，写新的不留坑）。

| 场景 | 用哪份 | 为什么 |
|---|---|---|
| 机型 tab 正常刷机 | **先刷 `uboot-bin/修改版/` 临时引导 → 进系统后立刻换刷 `uboot-bin/官方版/`** | 临时版读得懂原厂写进去的 boot 区，所以能进系统；长期跑还是要回到官方版 |
| TTL / XMODEM 救砖 tab | **只送 `uboot-bin/官方版/`** | 会砖多半就是刷了官方版；而 XMODEM 是把 FIP 直接送进内存、不经过 NAND 读取，官方版在这一步照样起得来 |

> **救砖 tab 为什么不送修改版**（用户明确要求）：砖掉的机器上，UBI 里那套
> 数据往往已经被官方 4 bit 引导写过了，再塞一份 8bit 读的临时版容易两边不搭；
> 而且会让用户在两种引导之间来回横跳。代码里已经把修改版从救砖 tab 的
> 候选列表里过滤掉了（`gui.py:_find_fips` + `_is_modified_uboot`）。

引导合集由 `uboot_release.py` 从三个上游拉下来（见 5.4），CI 打包时自动带上。

### 路线 A2（救砖兜底，已验证）：TTL + XMODEM 发官方 FIP

这就是 issue #22 里报告者自己救回来、并最终装上 OpenWrt 的那套流程：

1. 用工具正常**备份**（备份是纯读取，不受 ECC 影响，安全）。
2. 照常刷 preloader / firstblock / FIP。刷完进不去系统是**预期现象**，
   不是失败 —— BL2 已经在跑了。
3. 接 TTL，看到 `Press x to load BL31 + U-Boot FIP via XMODEM` 时按 `x`，
   用 XMODEM 把 `bl31-u-boot.fip` 发进去。
4. U-Boot 起来后，正常走网页恢复刷 OpenWrt。之后**重启正常**。

**为什么这条路能通**：写和读全在 uboot-an758x 自己的 4 bit 阵营里，全程自洽
（见 3.5）。这是目前**唯一有实机成功记录**的路径。

代价：需要 TTL 线，且步骤比一键刷多。验收标准是**拔掉 TTL 线、断电重启三次
都能自起** —— issue #21 里 erroljay-stack 就是「能进系统但重启又报 -2」，
别把那个当成功。

### 路线 B（未完成，需要更多文件）：把整条链统一到 8 bit

**不能只补 preloader**（原因见 3.6）—— 那只会把砖挪到下一次重启。
要真正走通 stock2ubi 的一键路径，需要把 **BL2 + U-Boot（+ 之后读 UBI 的
OpenWrt 内核）** 全部统一到 8 bit，去配合厂商内核写入的那片闪存。

目前**只完成了 BL2 这一层的工具**：

```bash
python3 nandcheck.py an7581-fiberhome-hg5585f-ct-preloader.bin \
    --fix _out/preloader_ecc8.bin
```

> ⚠️ **单独用这个补好的 preloader 去刷，预期会在「U-Boot 阶段」或
> 「刷完第一次重启」时再砖。** 不要把它当成已完成的解法。

要往下做，需要这些文件（发我即可）：

- `an7581-*-bl31-u-boot.fip`（**最关键**）—— 用于核对/修补 U-Boot 那一层
- `an7581-*-firstblock.bin` —— 确认 bootstrap UBI 的卷布局
- 如果 OpenWrt 内核也内置了同一张表，还需要它对应的设置

### 路线 C：等上游修

pbs05 已明确回复「**华邦机型不能用这个，不兼容**」（issue #22），
等于宣告 Winbond 机型不在支持范围内。issue #10 虽然在讨论 ECC，
但上游目前的结论（`soc_ecc_ability = 4`）**方向仍然是错的** ——
他们盯着数据手册的「芯片最大 4 bit」，没意识到厂商固件是 8 bit 排布。
要说服他们改回来，需要拿本报告第三节的**厂商内核表 + 厂商 BL2 表**
（两侧都是 8 bit）作为证据。

## 七、工具侧的防线（已实现）

`flash.py` 的 `stock2ubi` 流程在**动任何数据之前**新增了一段检查
（`if mode == "stock2ubi":` → `4a-0. 闪存颗粒 / ECC 兼容性检查`）：

```python
chip, _ok, _msg = check_nand_ecc_compat(
    tn, preloader_path=(args.get("preloader") or None))
...
print("[!] 未向设备写入任何数据，设备状态未改变。")
tn.close(); sys.exit(1)
```

新增命令行参数：

| 参数 | 作用 |
|---|---|
| `--preloader 路径` | 提前把用户打算上传的 preloader 拿来做判定 |
| `--preloader-fix` | 判定为危险时自动补好，另存到 `_out/preloader_ecc8.bin` |

相关常量：

```python
NAND_CHECK_ENABLED = True        # False 则完全跳过闪存兼容性检查
NAND_CHECK_BLOCK   = True        # True: 不兼容时拒绝继续；False: 只警告
NAND_FIX_DIR       = "_out"      # --preloader-fix 补好的文件放这里
```

之所以要拦：拦下来时**一个字节都没写进设备**，用户拔电重来即可；
放过去就是 TTL + XMODEM 救砖，代价完全不对等。

---

## 八、待确认 / 待实机验证

1. **把整条链统一到 8 bit 后能否一次刷通** —— 这是唯一还没验证的解法方向。
   前置条件：拿到 `bl31-u-boot.fip` 并确认/修改它的 ECC 表。
2. **BootROM 是否真的不校验尾部 DER** —— 见 5.3，三条间接证据支持「不校验」。
3. **U-Boot（FIP）那一层的 ECC** —— **已由 issue #22 反推为 4 bit**
   （推理过程见 3.5：U-Boot 写入 OpenWrt 后 BL2 能正常读取，说明两者同为
   4 bit）。仍建议拿到 FIP 后**直接查表确认**，而不是只靠推理。
4. **`_src/nand_ids.c` 属于哪棵树** —— 这个文件里 `W29N02KVSIAF` 是
   `NAND_ECC_INFO(4, SZ_512)`，但它跟上游 issue 的文字对不上：
   issue #5 说 U-Boot 当时是 **8**、issue #10 说是 **1**、这个文件里是 **4**。
   三个数字互不相同，说明 U-Boot 实际生效的 ECC **不一定**来自 `nand_ids.c`。
   （注：厂商内核那张表已经在 `_src/vendor_kernel.bin` 里直接找到了，
   所以这条只影响「U-Boot 源码怎么读」，不影响主结论。）
5. **OpenWrt 内核的 ECC** —— 如果它内置的表也是 4 bit，那么即使 BL2 和
   U-Boot 都改成 8 bit，刷完 OpenWrt 之后**还是读不了自己写的 UBI**。
   这是路线 B 的最后一环，必须一起确认。
6. **本机闪存型号的实测读取** —— `detect_nand_chip()` 依赖设备在线
   （`dmesg` 或 `/sys/class/mtd/mtd0`），当前设备离线，未在真机上验证。

## 九、哪些是实测、哪些是推断

为了不把推断说成事实，明确区分：

| 结论 | 证据强度 |
|---|---|
| v3/v4 preloader 里 Winbond 是 4 bit，v1/v2 是 8 bit | **实测** —— 直接解开四个 release 的二进制读出来的 |
| v2 与 v4 对这颗芯片只差 `+0x42` 一个字节 | **实测** —— 逐字节 diff |
| 厂商 BL2 里 Winbond 是 8 bit | **实测** —— 从 `_src/vendor_bl2_1c9e3.bin` 解出的表 |
| 厂商内核按 8 bit 写整片闪存 | **实测** —— `_src/vendor_kernel.bin` 里厂商内核自己的表就是 8 bit（见 3.1b） |
| 4 bit 读 8 bit 数据会导致 UBI 挂不上 | **实测（同机型旁证）** —— issue #9 里 HG5585F-CU 的 `-74 (EBADMSG)` 日志，方向相反但机理相同 |
| 补丁后容器结构完好、下游偏移未变 | **实测** —— 三个流全部干净解码，头/尾字节完全一致 |
| BootROM 不校验尾部 DER | **推断（三条间接证据，未实机确认）** |
| U-Boot 对 Winbond 用几 bit | **由 #22 反推为 4 bit** —— U-Boot 写入的东西 BL2（4 bit）能读，故两者同为 4 bit；建议拿到 FIP 后直接查表确认 |
| XMODEM 路径能通、stock2ubi 路径必砖 | **实测（实机记录）** —— issue #22 报告者的完整过程 |
| 只补 preloader 不足以解决问题 | **推断** —— 由「链条一致性」推出，见 3.6；这也正是需要 FIP 去验证的原因 |