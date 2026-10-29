"""王者营地扫码登录指令。

注意：GsCore 处理单条入站消息的预算是 30 秒（``handler._INBOUND_BUDGET_S``），
扫码却可能等几分钟。所以指令本身只负责创建会话并立刻返回，
真正的“等扫码 -> 保存 -> 回复”放在后台 asyncio 任务里执行。
"""

from __future__ import annotations

import time
import asyncio
from typing import Optional
from dataclasses import field, dataclass

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .login import (
    LoginError,
    LoginSession,
    close_session,
    create_session,
    wait_for_login,
    validate_cookie,
)
from .store import mask_value, save_account, list_accounts, check_all_accounts
from .config import get_cfg

PLUGIN_TAG = "[王者扫码]"

SCAN_WARNING = (
    "此处扫码使用的账号为王者营地账号，在这里登录会使王者营地APP被顶号。"
    "扫码后请勿在营地APP登录此账号，否则会顶号失效。"
    "建议使用不登营地APP的账号扫码。"
)

sv_login = SV("王者扫码登录", priority=5, area="ALL")
sv_status = SV("王者扫码状态", priority=5, area="ALL")
sv_admin = SV("王者营地CK检查", priority=5, area="ALL", pm=6)


@dataclass
class LoginJob:
    platform: str
    key: str
    user_id: str
    bot_id: str
    group_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    session: Optional[LoginSession] = None
    task: Optional[asyncio.Task] = None
    cancel_requested: bool = False
    cancel_replied: bool = False
    # 已发送消息的 ID，用于 90 秒限时撤回
    recall_ids: list[str] = field(default_factory=list)
    recall_task: Optional[asyncio.Task] = None

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.created_at)


# key = bot_id:user_id
JOBS: dict[str, LoginJob] = {}
CHECK_JOBS: set[str] = set()
CHECK_TASKS: dict[str, asyncio.Task] = {}


def _key(ev: Event) -> str:
    return f"{ev.bot_id}:{ev.user_id}"


def _platform_of(ev: Event) -> str:
    command = (ev.command or "").lower()
    text = (ev.text or "").strip().lower()
    mixed = f"{command} {text}"
    if "微信" in mixed or "wx" in mixed:
        return "wx"
    if "qq" in mixed:
        return "qq"
    return str(get_cfg("default_platform") or "wx").lower()


def _platform_name(platform: str) -> str:
    return "微信" if platform == "wx" else "QQ"


def _in_group(ev: Event) -> bool:
    return ev.user_type != "direct"


def _pick_text(platform: str) -> str:
    if platform == "wx":
        return "请使用微信扫描二维码，并在手机上确认登录。"
    return "请使用 QQ 扫描二维码，并在手机上确认登录。若二维码显示异常，可改用「王者微信扫码登录」。"


async def _safe_send(bot: Bot, message) -> bool:
    try:
        await bot.send(message)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{PLUGIN_TAG} 发送消息失败：{exc}")
        return False


async def _send_recallable(bot: Bot, job: LoginJob, message) -> bool:
    """发送消息并尽量拿到 message_id，供 90 秒后撤回。

    ``wait_recall`` 依赖适配器回传 message_id；这里只等 3 秒，
    适配器不支持时消息仍然正常发出，只是无法撤回，不会卡住扫码流程。
    """
    core_bot = getattr(bot, "bot", None)
    if getattr(core_bot, "_supports_recall", None) is False:
        try:
            await bot.send(message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{PLUGIN_TAG} 发送消息失败：{exc}")
            return False

    try:
        ids = await asyncio.wait_for(bot.send(message, wait_recall=True), timeout=3.0)
    except asyncio.TimeoutError:
        logger.debug(f"{PLUGIN_TAG} 等待消息回执超时，本条消息不参与限时撤回")
        return True
    except TypeError:
        # 极旧版本的 Bot.send 没有 wait_recall 参数
        try:
            await bot.send(message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{PLUGIN_TAG} 发送消息失败：{exc}")
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{PLUGIN_TAG} 发送消息失败：{exc}")
        return False

    if ids:
        job.recall_ids.extend(str(item) for item in ids if item)
    return True


async def _unsend(bot: Bot, ids: list[str]) -> None:
    if not ids:
        return
    try:
        await bot.unsend(list(ids))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{PLUGIN_TAG} 撤回消息失败：{exc}")


def _schedule_recall(bot: Bot, job: LoginJob) -> None:
    delay = int(get_cfg("recall_delay") or 0)
    if delay <= 0 or job.recall_task is not None or not job.recall_ids:
        return
    ids = list(job.recall_ids)
    job.recall_task = asyncio.create_task(_recall_after(bot, job, ids, delay))


async def _recall_after(bot: Bot, job: LoginJob, ids: list[str], delay: int) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await _unsend(bot, ids)
    recalled = set(ids)
    job.recall_ids = [item for item in job.recall_ids if item not in recalled]
    job.recall_task = None


async def _recall_now(bot: Bot, job: LoginJob) -> None:
    """取消/失败时立刻清掉二维码和提示语；已到期的消息不会重复撤回。"""
    if int(get_cfg("recall_delay") or 0) <= 0:
        job.recall_ids.clear()
        return
    task = job.recall_task
    job.recall_task = None
    if task is not None and not task.done():
        task.cancel()
    ids = list(job.recall_ids)
    job.recall_ids.clear()
    await _unsend(bot, ids)


async def _notify_scanned(bot: Bot, job: LoginJob) -> None:
    if job.cancel_requested:
        return
    await _safe_send(bot, "已扫码，请在手机上确认登录。")


def _format_outcome(account, outcome) -> str:
    lines = [
        f"✅ {_platform_name(account.platform)}扫码登录成功",
        f"营地UID：{account.user_id}",
        f"CK：{mask_value(account.token)}",
    ]
    if account.nickname:
        lines.append(f"昵称：{account.nickname}")

    if get_cfg("auto_save_ck"):
        if outcome.ck_saved:
            lines.append("✅ CK 已写入 WzryUID 账号池")
        else:
            lines.append("⚠️ CK 写入账号池失败，请查看 bot 日志")
    else:
        lines.append("ℹ️ 控制台已关闭「自动保存CK」，本次未写入账号池")

    if get_cfg("auto_bind_uid"):
        if outcome.bound == 0:
            suffix = "，并已切换为当前UID" if outcome.switched == 0 else ""
            lines.append(f"✅ 已自动绑定营地 UID{suffix}")
        elif outcome.bound == -2:
            lines.append("ℹ️ 该营地 UID 之前已经绑定过")
        else:
            lines.append(f"⚠️ 自动绑定失败，可手动发送：王者绑定UID {account.user_id}")
    else:
        lines.append(f"ℹ️ 未自动绑定，可发送：王者绑定UID {account.user_id}")

    if get_cfg("auto_save_ck") and not outcome.ck_saved:
        lines.append("提示：CK 未保存时，WzryUID 查询会缺少 CK。")
    return "\n".join(lines)


async def _handle_success(bot: Bot, job: LoginJob, account) -> None:
    if not account.user_id or not account.token:
        await _safe_send(bot, "营地登录返回的数据不完整，本次登录未保存，请重新扫码。")
        return

    if get_cfg("verify_before_save"):
        ok, message = await validate_cookie(account.user_id, account.token)
        if ok is False:
            logger.warning(f"{PLUGIN_TAG} 登录后 CK 校验失败 uid={account.user_id} msg={message}")
            await _safe_send(
                bot,
                "营地登录成功，但 CK 校验未通过"
                f"（{message}），为避免写入坏数据，本次未保存。\n请重新扫码，或稍后重试。",
            )
            return

    outcome = await save_account(
        job.user_id,
        job.bot_id,
        account,
        auto_save=bool(get_cfg("auto_save_ck")),
        auto_bind=bool(get_cfg("auto_bind_uid")),
        auto_switch=bool(get_cfg("auto_switch_uid")),
        group_id=job.group_id,
    )
    await _safe_send(bot, _format_outcome(account, outcome))


async def _login_worker(bot: Bot, job: LoginJob) -> None:
    session: Optional[LoginSession] = None
    success = False
    timeout = max(30, int(get_cfg("login_timeout") or 180))
    poll_interval = float(get_cfg("poll_interval") or 1.5)
    try:
        try:
            session = await create_session(
                job.platform,
                job.user_id,
                job.bot_id,
                qq_browser=str(get_cfg("qq_browser") or "auto"),
                qq_headless=bool(get_cfg("qq_headless")),
                qq_page_timeout=float(get_cfg("qq_page_timeout") or 90),
            )
        except LoginError as exc:
            logger.warning(f"{PLUGIN_TAG} 创建扫码会话失败：{exc.code} {exc.message}")
            if not job.cancel_replied:
                await _safe_send(bot, f"发起扫码登录失败：{exc.message}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"{PLUGIN_TAG} 创建扫码会话异常：{exc}")
            if not job.cancel_replied:
                await _safe_send(bot, "发起扫码登录失败，请稍后重试。")
            return

        job.session = session
        if job.cancel_requested:
            return

        if not await _send_recallable(bot, job, session.qr_bytes):
            return
        await _send_recallable(bot, job, _pick_text(job.platform))
        await _send_recallable(bot, job, f"二维码有效期约 {timeout} 秒，请尽快扫描。")
        _schedule_recall(bot, job)

        try:
            account = await wait_for_login(
                session,
                timeout=timeout,
                poll_interval=poll_interval,
                on_status=lambda _status: _notify_scanned(bot, job),
            )
        except LoginError as exc:
            if job.cancel_requested:
                return
            logger.info(f"{PLUGIN_TAG} 扫码登录结束：{exc.code} {exc.message}")
            await _safe_send(bot, f"扫码登录失败：{exc.message}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"{PLUGIN_TAG} 等待扫码异常：{exc}")
            if not job.cancel_requested:
                await _safe_send(bot, "扫码登录过程中出现异常，请稍后重试。")
            return

        if job.cancel_requested:
            return
        await _handle_success(bot, job, account)
        success = True
    finally:
        if session is not None:
            await close_session(session)
        if not success:
            await _recall_now(bot, job)
        if JOBS.get(job.key) is job:
            JOBS.pop(job.key, None)


async def _start_login(bot: Bot, ev: Event, platform: str) -> None:
    if platform not in ("wx", "qq"):
        await _safe_send(bot, "未知的扫码方式，请使用「王者微信扫码登录」或「王者QQ扫码登录」。")
        return

    if platform == "wx" and not get_cfg("enable_wx"):
        await _safe_send(bot, "微信扫码登录已在 GsCore 控制台关闭，请联系管理员开启。")
        return
    if platform == "qq" and not get_cfg("enable_qq"):
        await _safe_send(bot, "QQ 扫码登录已在 GsCore 控制台关闭，请联系管理员开启。")
        return

    if _in_group(ev) and not get_cfg("allow_group"):
        await _safe_send(bot, "为了账号安全，请在私聊中发起扫码登录（管理员可在控制台关闭此限制）。")
        return

    key = _key(ev)
    if key in JOBS:
        await _safe_send(bot, "你已有一个扫码登录正在进行，请先在手机上确认，或发送「王者取消扫码登录」。")
        return

    job = LoginJob(
        platform=platform,
        key=key,
        user_id=ev.user_id,
        bot_id=ev.bot_id,
        group_id=ev.group_id,
    )
    JOBS[key] = job

    if not await _send_recallable(bot, job, f"正在生成{_platform_name(platform)}登录二维码，请稍候…"):
        JOBS.pop(key, None)
        return

    if not await _send_recallable(bot, job, SCAN_WARNING):
        await _recall_now(bot, job)
        JOBS.pop(key, None)
        return

    job.task = asyncio.create_task(_login_worker(bot, job))


@sv_login.on_command(
    ("王者扫码登录", "王者扫码登陆", "王者营地扫码登录", "王者营地扫码登陆"),
    block=True,
)
async def cmd_scan_default(bot: Bot, ev: Event):
    await _start_login(bot, ev, _platform_of(ev))


@sv_login.on_command(
    ("王者微信扫码登录", "王者wx扫码登录", "王者微信扫码登陆", "王者wx扫码登陆"),
    block=True,
)
async def cmd_scan_wx(bot: Bot, ev: Event):
    await _start_login(bot, ev, "wx")


@sv_login.on_command(
    ("王者QQ扫码登录", "王者qq扫码登录", "王者QQ扫码登陆", "王者qq扫码登陆"),
    block=True,
)
async def cmd_scan_qq(bot: Bot, ev: Event):
    await _start_login(bot, ev, "qq")


@sv_login.on_command(("王者取消扫码登录", "王者取消扫码登陆"), block=True)
async def cmd_cancel(bot: Bot, ev: Event):
    job = JOBS.get(_key(ev))
    if job is None:
        await _safe_send(bot, "当前没有正在进行的扫码登录。")
        return

    job.cancel_requested = True
    job.cancel_replied = True
    # 立刻让这个用户能重新发起；旧 worker 收尾时按对象身份判断，不会误删新任务
    if JOBS.get(job.key) is job:
        JOBS.pop(job.key, None)
    # 不直接 cancel task：任务可能正在启动浏览器，硬取消会跳过清理。
    # 已生成二维码时关闭会话即可让等待循环立刻退出。
    if job.session is not None:
        await close_session(job.session)
    await _recall_now(bot, job)
    await _safe_send(bot, "已取消本次扫码登录。")


@sv_status.on_command(("王者扫码状态", "王者营地扫码状态"), block=True)
async def cmd_status(bot: Bot, ev: Event):
    key = _key(ev)
    job = JOBS.get(key)
    lines = ["【王者营地扫码登录状态】"]
    if job is not None:
        phase = "正在生成二维码" if job.session is None else "等待扫码/确认"
        lines.append(f"进行中：{_platform_name(job.platform)}扫码，{phase}，已等待 {int(job.elapsed)} 秒")
    else:
        lines.append("进行中：无")

    rows = await list_accounts(ev.user_id)
    if not rows:
        lines.append("本账号尚未保存营地 CK")
    else:
        lines.append(f"已保存 CK：{len(rows)} 个")
        for row in rows[:8]:
            lines.append(f"- UID {row.uid or '未知'} / CK {mask_value(row.cookie)}")
        if len(rows) > 8:
            lines.append(f"- 其余 {len(rows) - 8} 个已省略")

    lines.append("发送「王者扫码登录」开始，发送「王者取消扫码登录」结束当前会话。")
    await _safe_send(bot, "\n".join(lines))


@sv_status.on_command(("王者扫码帮助", "王者扫码登录帮助"), block=True)
async def cmd_help(bot: Bot, ev: Event):
    await _safe_send(
        bot,
        "【王者营地扫码登录帮助】\n"
        "王者扫码登录 / 王者营地扫码登录：使用默认方式登录\n"
        "王者微信扫码登录 / 王者wx扫码登录：微信扫码\n"
        "王者QQ扫码登录 / 王者qq扫码登录：QQ扫码\n"
        "王者扫码状态：查看当前会话和已保存的 CK\n"
        "王者取消扫码登录：关闭当前二维码\n"
        "王者检查营地CK：检查账号池中全部 CK 是否有效\n\n"
        "登录成功后会自动把 UID 和 CK 写入 WzryUID 的账号池，"
        "并按控制台设置自动绑定 UID。\n"
        "所有参数都可以在 GsCore 管理控制台 /app 的插件配置里调整。",
    )


async def _check_worker(bot: Bot, key: str) -> None:
    try:
        rows = await list_accounts()
        if not rows:
            await _safe_send(bot, "WzryUID 账号池里还没有任何营地 CK。")
            return

        await _safe_send(bot, f"开始检查 {len(rows)} 个营地 CK，请稍候…")
        results = await check_all_accounts(rows, concurrency=int(get_cfg("check_concurrency") or 3))

        valid = sum(1 for _, ok, _ in results if ok is True)
        invalid = [(row, msg) for row, ok, msg in results if ok is False]
        unknown = sum(1 for _, ok, _ in results if ok is None)

        lines = [
            "【营地 CK 检查结果】",
            f"总数：{len(results)}",
            f"有效：{valid}",
            f"失效：{len(invalid)}",
            f"无法判断：{unknown}",
        ]
        if invalid:
            lines.append("失效明细（最多显示 15 个）：")
            for row, message in invalid[:15]:
                lines.append(f"- 用户 {row.user_id} / UID {row.uid or '未知'}：{message}")
            if len(invalid) > 15:
                lines.append(f"- 其余 {len(invalid) - 15} 个已省略")
            lines.append("失效 CK 可由对应用户重新发送「王者扫码登录」获取。")

        await _safe_send(bot, "\n".join(lines))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"{PLUGIN_TAG} 检查 CK 异常：{exc}")
        await _safe_send(bot, "检查营地 CK 时出现异常，请查看 bot 日志。")
    finally:
        CHECK_JOBS.discard(key)
        CHECK_TASKS.pop(key, None)


@sv_admin.on_command(("王者检查营地CK", "王者检查营地ck", "王者检查全部CK", "王者检查全部ck"), block=True)
async def cmd_check_all(bot: Bot, ev: Event):
    key = _key(ev)
    if key in CHECK_JOBS:
        await _safe_send(bot, "你已经有一个 CK 检查在进行了，请稍候。")
        return
    CHECK_JOBS.add(key)
    CHECK_TASKS[key] = asyncio.create_task(_check_worker(bot, key))
