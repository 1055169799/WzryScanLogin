"""扫码登录辅助插件的配置定义。

配置项会通过 ``StringConfig`` 注册进 GsCore 的配置中心，
在 GsCore 管理控制台（/app）的插件详情页里可以直接修改。
"""

from typing import Dict

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsFloatConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

PLUGIN_NAME = "WzryScanLogin"

CONFIG_PATH = get_res_path(PLUGIN_NAME) / "config.json"

CONFIG_DEFAULT: Dict[str, GSC] = {
    # ---------------- 基础 ----------------
    "divider_basic": GsDivider(
        "基础设置",
        "扫码登录的通用参数",
    ),
    "default_platform": GsStrConfig(
        "默认扫码方式",
        "发送「王者扫码登录」时默认使用哪种方式；也可以发送「王者微信扫码登录」或「王者QQ扫码登录」临时指定",
        "wx",
        options=["wx", "qq"],
    ),
    "enable_wx": GsBoolConfig(
        "启用微信扫码",
        "关闭后「王者微信扫码登录」会提示未启用",
        True,
    ),
    "enable_qq": GsBoolConfig(
        "启用QQ扫码",
        "关闭后「王者QQ扫码登录」会提示未启用。QQ 扫码需要本机安装 Chrome / Edge 浏览器",
        True,
    ),
    "allow_group": GsBoolConfig(
        "允许群聊发起",
        "关闭时只允许私聊发起扫码登录，避免登录二维码被群里其他人扫走。默认关闭",
        False,
    ),
    "login_timeout": GsIntConfig(
        "二维码有效期",
        "等待扫码/确认的最长秒数，超时后本次登录结束",
        180,
        max_value=600,
        options=[120, 180, 240, 300, 600],
    ),
    "recall_delay": GsIntConfig(
        "扫码消息撤回",
        "二维码和三条扫码提示语在发送后多少秒自动撤回；0 表示不撤回",
        90,
        max_value=600,
        options=[0, 30, 60, 90, 120, 180, 300],
    ),
    "poll_interval": GsFloatConfig(
        "轮询间隔",
        "查询扫码状态的间隔（秒），网络较差时可适当调大",
        1.5,
        min_value=1.0,
        max_value=5.0,
    ),
    # ---------------- 保存 ----------------
    "divider_save": GsDivider(
        "保存设置",
        "登录成功后如何写入 WzryUID 的账号池",
    ),
    "auto_save_ck": GsBoolConfig(
        "自动保存CK",
        "登录成功后自动把营地 UID 和 token 写入 WzryUID 的 WzryUser 表（即「王者添加CK」保存的位置）",
        True,
    ),
    "auto_bind_uid": GsBoolConfig(
        "自动绑定UID",
        "登录成功后自动把营地 UID 绑定到发起人的 WzryUID 账号上，之后可直接查战绩/皮肤",
        True,
    ),
    "auto_switch_uid": GsBoolConfig(
        "自动切换UID",
        "如果发起人之前绑定过别的 UID，登录成功后把新 UID 切换为当前 UID",
        True,
    ),
    "verify_before_save": GsBoolConfig(
        "保存前校验CK",
        "保存前调用一次营地接口确认 token 有效；明确失效时不写入账号池",
        True,
    ),
    # ---------------- QQ ----------------
    "divider_qq": GsDivider(
        "QQ 扫码设置",
        "QQ 登录需要浏览器完成授权，这里只影响 QQ 方式",
    ),
    "qq_browser": GsStrConfig(
        "QQ登录浏览器",
        "auto 会依次尝试 Chrome、Edge、Playwright 自带 Chromium；也可以固定一种",
        "auto",
        options=["auto", "chrome", "msedge", "chromium"],
    ),
    "qq_headless": GsBoolConfig(
        "QQ无头模式",
        "开启后浏览器在后台运行。若二维码截取失败可尝试关闭",
        True,
    ),
    "qq_page_timeout": GsIntConfig(
        "QQ页面加载超时",
        "打开 QQ 授权页的最长等待秒数",
        90,
        max_value=180,
        options=[60, 90, 120, 180],
    ),
    # ---------------- 检查 ----------------
    "divider_check": GsDivider(
        "CK 状态检查",
        "检查 WzryUID 账号池里的营地 CK 是否仍然有效：在聊天中发送「王者检查营地CK」（管理员）即可执行",
    ),
    "check_concurrency": GsIntConfig(
        "检查并发数",
        "发送「王者检查营地CK」时同时校验多少个 CK；过大可能被营地限流",
        3,
        max_value=10,
        options=[1, 2, 3, 5, 10],
    ),
}

CONFIG = StringConfig(PLUGIN_NAME, CONFIG_PATH, CONFIG_DEFAULT)


def get_cfg(key: str):
    """读取配置项的当前值。"""
    return CONFIG.get_config(key).data
