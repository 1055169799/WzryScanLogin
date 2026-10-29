"""WzryScanLogin —— 王者营地 / WzryUID 扫码登录辅助插件。

独立于 WzryUID 存放，不修改 WzryUID 的任何文件；
扫码成功后写入 WzryUID 使用的 wzryuser / wzrybind 表。

指令：
- 王者扫码登录（默认方式）
- 王者微信扫码登录 / 王者wx扫码登录
- 王者QQ扫码登录 / 王者qq扫码登录
- 王者扫码状态 / 王者取消扫码登录 / 王者扫码帮助

配置：GsCore 管理控制台 /app -> 插件 -> WzryScanLogin
"""

from gsuid_core.logger import logger

from . import commands  # noqa: F401  # 注册 SV 指令
from .config import CONFIG, CONFIG_PATH  # noqa: F401

logger.info(f"[王者扫码] 插件已加载，配置文件：{CONFIG_PATH}")
