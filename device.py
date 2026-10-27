#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备运行期状态：光猫地址 / 机型档案 / telnet 凭据 / 网页凭据 / MAC。

## 为什么有这个文件

以前这些东西全是 ``flash.py`` 配置区里的模块级全局变量（``ROUTER_IP``、
``MODEL_TELNET_USER``、``PASSWORD``、``MODEL_MAC`` …一共 30 多个），靠
``_load_model_globals()`` 一条条 ``global`` 赋值。两个问题：

1. **谁忘了灌就静默用默认值。** 命令行走 ``main()`` 会灌，GUI 走 ``run_flow()``
   没有灌 —— 于是贝尔机型在 GUI 里拿到的是空的机型账号，跑到「从登录页探测 +
   逐个试 user/useradmin」那条路上去了，用户填的铭牌密码也从来没生效
   （2026-10 用户实测报障，见 HANDOFF「机型/凭据状态」一节）。
2. **读代码看不出归属。** 40 个全局名字挤在一个配置区里，分不清哪个是「机型
   档案带的」、哪个是「用户填的」、哪个是「运行时从设备找回来的」。

现在按**类别**拆成几个小对象，每个只管一类：

    NET      光猫地址              NetConfig
    PROFILE  机型档案与能力开关     ModelProfile
    TELNET   telnet / su 凭据      TelnetAuth
    WEB      网页超级账号           WebAuth
    MAC      设备 MAC              DeviceMac

字段名就是原来那个全局（去掉类别前缀），调用处从 ``ROUTER_IP`` 变成
``NET.router_ip``、从 ``MODEL_TELNET_USER`` 变成 ``PROFILE.telnet_user``，
多打几个字，但一眼能看出它属于哪一类。

**唯一入口**是 ``configure_device()``：命令行在 ``main()`` 里调一次，GUI 在
``run_flow()`` 里调一次（带界面上的值和当前机型）。它把「机型给的」和
「界面/config 给的」按固定顺序灌进去，顺序是：

    1. 机型档案（models.py）—— 只覆盖机型确实定义了的项
    2. 界面 / config.ini 的值 —— 用户填了的才覆盖，空的保持机型值

这一层顺序就是刷机安全的关键：机型值先落地，用户值最后压上去，绝不会出现
「用户明明填了铭牌密码，程序却拿机型默认密码去试」这种白烧认证机会的事
（光猫连错 3 次锁 300 秒）。
"""

# telnet 密码默认值（= 贝尔移动版铭牌密码），没有 models.py 时也保持老行为。
_DEFAULT_TELNET_PASSWORD = "j3ba73@8"


class NetConfig:
    """光猫地址：IP、网页端口、telnet 端口。

    网页端口随机型变（贝尔 80 / 烽火 8080），所以它同时也由机型档案覆盖，
    但归属上它是「地址」，跟密码无关，单独一类。
    """

    def __init__(self):
        self.router_ip = "192.168.1.1"
        self.telnet_port = 23
        self.web_port = 80

    def web_base(self):
        """管理网页的根地址（80 端口不写端口号，与旧 flash.web_base() 一致）。"""
        port = int(self.web_port or 80)
        if port == 80:
            return "http://%s" % self.router_ip
        return "http://%s:%d" % (self.router_ip, port)

    def apply_override(self, router_ip=None, web_port=None, telnet_port=None):
        """界面 / config.ini 的值（空值忽略）。"""
        if router_ip:
            self.router_ip = router_ip
        if web_port:
            self.web_port = int(web_port)
        if telnet_port:
            self.telnet_port = int(telnet_port)
        return self

    def describe(self):
        return "%s（网页 %s，telnet %d）" % (
            self.router_ip, self.web_base(), self.telnet_port)


class ModelProfile:
    """机型档案（``models.py`` 的 ``Model``）与由它展开出来的固定值 / 能力开关。

    为什么要抄一份而不是直接用 ``Model`` 对象：``models.py`` 可能缺失或读失败
    （打包不全、用户删了文件），老代码遇到这种情况会「沿用配置区默认值」继续
    跑。这里保留同样的默认值，行为就不变。
    """

    def __init__(self):
        self.model = None                # models.Model 对象（None = 用默认值）
        self.family = "bel"              # bel / fh：决定是否生成 1 MiB factory
        # --- telnet / su 的机型固定值 ---
        self.telnet_user = ""            # 机型固定的 telnet 账号（空=按候选列表试）
        self.su_user = ""                # 机型固定的 su 账号（空=用 TELNET.su_user）
        self.telnet_password = ""        # 机型固定 telnet 密码（不含 MAC 后缀）
        self.telnet_password_prefix = ""  # 「固定值 + MAC 后 6 位」规则里的前缀
        self.telnet_password_from_mac = False   # 机型密码本来就由 MAC 推导
        self.telnet_password_try_mac_suffix = False  # 额外试「固定值+MAC后6位」
        self.su_password = ""            # 机型固定 su 密码
        self.su_password_prefix = ""     # su 密码的 MAC 前缀（烽火 Fh@）
        self.su_password_from_mac = False  # su 密码是否由 MAC 推导（烽火 True）
        # --- 网页超级账号（机型有定义时才覆盖 WEB）---
        self.super_user = ""
        self.super_password = ""
        # --- 能力开关 ---
        self.need_ftp_for_su = True      # 提权前是否必须先开 FTP（贝尔 True）
        self.telnet_enable = "web"       # 开 telnet 的方式：web / fhtool / manual
        self.backup_transport = "auto"   # 备份传输：curl / tftp / auto
        self.supports_web_telnet = True
        self.supports_stock2ubi = True
        self.supports_uboot_flash = True

    @property
    def su_password_from_telnet(self):
        """机型的 su 密码是不是**就是** telnet 密码。

        贝尔：su user_ftp 的密码等于 telnet 密码（机型没给固定 su 密码，
        也没有 MAC 前缀）→ True。
        烽火：su root 的密码是 ``Fh@ + MAC 后 6 位`` → False。
        """
        return not self.su_password and not self.su_password_prefix

    @property
    def name(self):
        return getattr(self.model, "name", "") or ""

    @property
    def key(self):
        return getattr(self.model, "key", "") or ""

    def load(self, model_key=None):
        """按机型把固定值写进本对象，返回机型对象（读不到 models.py 时返回 None）。"""
        try:
            import models as models_mod
        except Exception:
            return None
        m = models_mod.get(model_key or models_mod.DEFAULT_MODEL)
        self.model = m
        self.family = getattr(m, "family", "bel")
        self.telnet_user = m.telnet_user
        self.su_user = m.su_user
        self.telnet_password = m.telnet_password
        self.telnet_password_prefix = m.telnet_password_prefix
        self.telnet_password_from_mac = m.telnet_password_from_mac
        self.telnet_password_try_mac_suffix = m.telnet_password_try_mac_suffix
        self.su_password = m.su_password
        self.su_password_prefix = m.su_password_prefix
        self.su_password_from_mac = getattr(m, "su_password_from_mac", False)
        self.super_user = m.super_user
        self.super_password = m.super_password
        self.need_ftp_for_su = m.need_ftp_for_su
        self.telnet_enable = m.telnet_enable
        self.backup_transport = m.backup_transport
        self.supports_web_telnet = m.supports_web_telnet
        self.supports_stock2ubi = m.supports_stock2ubi
        self.supports_uboot_flash = m.supports_uboot_flash
        return m

    def describe(self):
        return "%s（%s 家族）" % (self.name or "未指定机型", self.family)


class TelnetAuth:
    """telnet 登录与 su 提权用的凭据，以及候选顺序。

    候选项很多都是「猜」出来的，而光猫连错 3 次就锁 300 秒，所以顺序有讲究，
    每条候选为什么排在那里都写在方法注释里，别随手调整。
    """

    def __init__(self):
        # 账号：username 是「界面/命令行给的」，机型的固定账号在 PROFILE.telnet_user
        self.username = "user"               # XG-040G-MD 移动版实测为 user
        self.username_candidates = ["user", "useradmin"]
        # 不要加 admin：错的候选会白耗一次认证机会
        self.auto_detect_username = True     # 从登录页 JS 里读真实账号
        self.username_explicit = False       # 界面/命令行明确给过账号
        # 密码
        self.password = _DEFAULT_TELNET_PASSWORD
        self.password_explicit = False       # 界面/config 明确给过 telnet 密码
        self.su_user = "user_ftp"
        self.su_password = _DEFAULT_TELNET_PASSWORD
        self.su_password_explicit = False    # 界面/config 明确给过 su 密码
        self.manual_password = ""            # 用户用别的工具解出来的真实密码
        self.manual_su_password = ""         # 同上，su 用
        self.recovered_password = ""         # 运行时从设备取回的密码（不写盘）

    # ---------- 应用来源 ----------

    def apply_model(self, profile):
        """机型档案里非空的项覆盖默认值（与旧 _load_model_globals 一致）。"""
        if profile.telnet_user:
            self.username = profile.telnet_user
        if profile.su_user:
            self.su_user = profile.su_user
        if profile.telnet_password:
            self.password = profile.telnet_password
        if profile.su_password:
            self.su_password = profile.su_password
        elif profile.su_password_from_mac:
            self.su_password = ""            # 由 MAC 推导，见 su_password_candidates
        elif profile.su_password_from_telnet:
            self.su_password = self.password
        return self

    def apply_override(self, username=None, password=None, su_user=None,
                       su_password=None, manual_password=None,
                       manual_su_password=None, profile=None):
        """界面 / config.ini 的值：填了的才覆盖。

        ``profile`` 只用来判断「su 密码是否跟随 telnet 密码」（贝尔）。
        """
        if username:
            self.username = username
            self.username_explicit = True
        if password:
            self.password = password
            self.password_explicit = True
            # 贝尔：su 密码就是 telnet 密码，用户改了铭牌密码必须跟着改，
            # 否则 su 会拿旧密码去试（这是 2026-10 那次报障的一部分）。
            if not su_password and profile is not None \
                    and profile.su_password_from_telnet:
                self.su_password = password
        if su_user:
            self.su_user = su_user
        if su_password:
            self.su_password = su_password
            self.su_password_explicit = True
        if manual_password:
            self.manual_password = manual_password
        if manual_su_password:
            self.manual_su_password = manual_su_password
        return self

    def prepend_recovered(self, password):
        """把运行时找回的密码插到候选最前面（只在本进程内生效，不写 config.ini）。"""
        if password:
            self.recovered_password = password

    # ---------- 候选顺序 ----------

    def accounts(self, profile, detected=None, reenter_only=False):
        """返回 telnet 账号候选（顺序 = 尝试顺序，每个账号只试一次）。

        顺序：界面明确指定的账号 > 机型固定账号 > 登录页探测到的 > 候选列表。
        机型固定了账号时不再探测、不再试错 —— 那个账号是机型属性，
        探测出来的通常是网页账号，试错还会白耗认证机会。
        """
        out = []

        def _add(u):
            if u and u not in out:
                out.append(u)

        if self.username_explicit:
            _add(self.username)
        if profile.telnet_user:
            _add(profile.telnet_user)
        if detected:
            _add(detected)
        for u in [self.username] + ([] if profile.telnet_user else
                                    list(self.username_candidates)):
            _add(u)
        return out[:1] if reenter_only else out

    def wants_detect(self, profile, reenter_only=False):
        """是否需要从登录页探测账号（机型固定了账号就不用）。"""
        return bool(self.auto_detect_username and not reenter_only
                    and not profile.telnet_user)

    def password_candidates(self, profile, mac):
        """telnet 密码候选：界面明确给的 > 用户手填的 > 运行时找回的 > 机型静态候选。

        界面明确给的排最前：贝尔机型的 telnet 密码就是机器底部铭牌上印的那一个，
        用户在界面填了它，就不该再拿机型默认值去烧一次认证机会（光猫连错 3 次
        锁 300 秒，见 m03174 那次报障）。
        手填的排其次是因为「本工具解不开、用户用别的工具解开了」这种情况，
        用户的答案比任何猜测都可靠。
        """
        out = []

        def _add(p):
            if p and p not in out:
                out.append(p)

        if self.password_explicit:
            _add(self.password)
        _add(self.manual_password)
        _add(self.recovered_password)
        for p in self.static_password_candidates(profile, mac):
            _add(p)
        return out

    def static_password_candidates(self, profile, mac):
        """静态候选（机型固定值 / 用户配置 / MAC 后缀变体）。"""
        out = []

        def _add(p):
            if p and p not in out:
                out.append(p)

        _add(profile.telnet_password)
        _add(self.password)
        if profile.telnet_password and (profile.telnet_password_try_mac_suffix
                                        or profile.telnet_password_from_mac):
            tail = mac.tail6()
            if tail:
                if profile.telnet_password_from_mac:
                    # 机型规则本来就是「前缀+MAC后6位」，派生值才是首选
                    _add(profile.telnet_password_prefix + tail)
                    _add(profile.telnet_password_prefix + tail.lower())
                else:
                    # 机型规则是固定值，MAC 后缀只是流传的另一种说法，排后面
                    _add(profile.telnet_password + tail)
                    _add(profile.telnet_password + tail.lower())
                    if profile.telnet_password_prefix:
                        _add(profile.telnet_password_prefix + tail)
                        _add(profile.telnet_password_prefix + tail.lower())
        return out or [self.password]

    def su_password_candidates(self, profile, mac):
        """su（提权）密码候选。

        与 telnet 密码分开的原因：两个密码的**来源不同**。
          - 贝尔：su user_ftp 的密码就是 telnet 密码（同一个）；
          - 烽火：su root 的密码是 ``Fh@ + MAC 后 6 位大写``，与 telnet 无关。
        混在一个列表里，烽火会先拿 telnet 密码去 su，多烧一次机会。
        """
        out = []

        def _add(p):
            if p and p not in out:
                out.append(p)

        if self.su_password_explicit:
            _add(self.su_password)
        _add(self.manual_su_password)
        if profile.su_password_prefix:
            tail = mac.tail6()
            if tail:
                _add(profile.su_password_prefix + tail)
                _add(profile.su_password_prefix + tail.lower())
        _add(profile.su_password)
        _add(self.su_password)
        if profile.need_ftp_for_su or not profile.su_password_prefix:
            _add(self.password)
        return out or [self.su_password or self.password]

    def su_target(self, profile):
        """su 的目标账号（机型固定值优先）。"""
        return profile.su_user or self.su_user

    def describe(self, profile):
        user = profile.telnet_user or self.username
        return "账号 %s，su %s" % (user, self.su_target(profile))


class WebAuth:
    """网页超级账号（开 telnet / 备份前的网页登录）。"""

    def __init__(self):
        self.username = "CMCCAdmin"      # 移动版恢复出厂默认
        self.password = "aDm8H%MdA"
        self.username_explicit = False
        self.password_explicit = False

    def apply_model(self, profile):
        if profile.super_user:
            self.username = profile.super_user
        if profile.super_password:
            self.password = profile.super_password
        return self

    def apply_override(self, username=None, password=None):
        if username:
            self.username = username
            self.username_explicit = True
        if password:
            self.password = password
            self.password_explicit = True
        return self

    def describe(self):
        return "账号 %s" % (self.username or "（未填）")


class DeviceMac:
    """光猫 MAC：推导 telnet/su 密码、开 telnet 都要用。

    统一存成**无分隔大写**（``70D51E046CEC``）—— 设备页面上怎么显示不重要，
    推导密码时只用这一种形式，省得到处 replace(":", "")。
    """

    def __init__(self):
        self.value = ""

    def set(self, mac):
        """填入 MAC（接受任意分隔形式）；空值忽略。"""
        if mac:
            self.value = str(mac).replace(":", "").replace("-", "") \
                .replace(" ", "").upper()
        return self

    def normalized(self):
        return self.value.replace(":", "").replace("-", "").upper()

    def tail6(self):
        """MAC 后 6 位（不足 6 位返回空串）。"""
        n = self.normalized()
        return n[-6:] if len(n) >= 6 else ""

    def describe(self):
        return self.value or "（未读取）"


# ============ 唯一的五个实例（flash.py / gui.py 都用这一份）============
# 为什么用模块级单例：flash.py 里几百处调用都要读它们，而且备份/刷机流程是
# 一条长链，把对象当参数层层传下去只会让签名更难读。状态共享的代价靠
# configure_device() 只有一个入口 + 单元测试（_check_device.py）来兜。
NET = NetConfig()
PROFILE = ModelProfile()
TELNET = TelnetAuth()
WEB = WebAuth()
MAC = DeviceMac()


def configure_device(model_key=None, overrides=None, log=None):
    """把机型档案 + 界面/config 的凭据灌进上面那五个对象。

    :param model_key: config.ini 或命令行给的机型 key（``bel`` / ``hg5585f``）。
                      **None 表示不改动机型档案** —— 命令行 main() 已经设过一次，
                      run_flow() 再调一次时不能把机型清掉。
    :param overrides: 界面 / config.ini 的值，只认下面这些键，空值忽略：
                      router_ip / web_port / telnet_port / mac /
                      telnet_username / telnet_password / su_user / su_password /
                      manual_password / manual_su_password /
                      super_username / super_password
    :param log: 可选，``log(msg)`` 打印一行「[i] 机型: …」之类的说明。
    :returns: 当前生效的 :class:`ModelProfile`
    """
    overrides = overrides or {}

    if model_key is not None:
        profile = PROFILE.load(model_key)
        if profile is not None:
            NET.web_port = PROFILE.model.web_port
            NET.telnet_port = PROFILE.model.telnet_port
            TELNET.apply_model(PROFILE)
            WEB.apply_model(PROFILE)

    # 界面的 MAC 先垫上（机型密码推导要用），最后再用同一份覆盖一次，
    # 这样「机型给的 + 用户填的」都走同一条路。
    if overrides.get("mac"):
        MAC.set(overrides["mac"])

    NET.apply_override(router_ip=overrides.get("router_ip"),
                       web_port=overrides.get("web_port"),
                       telnet_port=overrides.get("telnet_port"))
    TELNET.apply_override(username=overrides.get("telnet_username"),
                          password=overrides.get("telnet_password"),
                          su_user=overrides.get("su_user"),
                          su_password=overrides.get("su_password"),
                          manual_password=overrides.get("manual_password"),
                          manual_su_password=overrides.get("manual_su_password"),
                          profile=PROFILE)
    WEB.apply_override(username=overrides.get("super_username"),
                       password=overrides.get("super_password"))

    if log is not None:
        log("[i] 机型: %s；%s；%s；%s" % (PROFILE.describe(), NET.describe(),
                                          TELNET.describe(PROFILE),
                                          "MAC " + MAC.describe()))
    return PROFILE


def reset():
    """恢复出厂状态（测试用；正常流程不要调）。

    注意是**原地**重置：flash.py 里 ``from device import NET`` 拿到的是对象
    引用，换成新实例的话那些引用还指着旧对象，这里的重置就白做了。
    """
    for obj in (NET, PROFILE, TELNET, WEB, MAC):
        obj.__init__()
    return NET, PROFILE, TELNET, WEB, MAC