"""王者营地 QQ / 微信扫码登录协议实现。

微信：纯 HTTP（SDK Ticket -> 二维码 -> 长轮询 -> code 换营地登录态）。
QQ  ：二维码由 QQ 授权页自己生成，必须用浏览器保持同一个会话，
      拦截最后的 ``auth://tauth.qq.com/?code=...`` 回调后再走 YSDK 换票。

营地登录成功后返回的数据里：
- ``userId`` 就是 WzryUID 需要的「营地 UID」（账号池里的 uid）
- ``token``  就是 WzryUID 需要的「CK」（账号池里的 cookie）
- ``encodeRes`` 解出来的 ``userKey`` 是营地接口的附加密钥，这里一并保留
"""

from __future__ import annotations

import re
import hmac
import json
import time
import uuid
import base64
import random
import asyncio
import hashlib
from typing import Any, Callable, Optional, Awaitable
from dataclasses import field, dataclass

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from gsuid_core.logger import logger

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CAMP_BASE_URL = "https://ssl.kohsocialapp.qq.com:10001"
CAMP_LOGIN_URL = f"{CAMP_BASE_URL}/user/login"
CAMP_ROLE_LIST_URL = f"{CAMP_BASE_URL}/game/allrolelistv3"

WX_APPID = "wxf4b1e8a3e9aaf978"
WX_QR_URL = "https://open.weixin.qq.com/connect/sdk/qrconnect"
WX_POLL_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"

QQ_APPID = "1105200115"
QQ_LOGIN_PAGE = (
    "https://openmobile.qq.com/oauth2.0/m_authorize"
    f"?client_id={QQ_APPID}&scope=all&redirect_uri=auth://tauth.qq.com/"
    "&style=qr&response_type=code"
)
QQ_TOKEN_URL = "https://ysdk.qq.com/cmd/QQCodeLogin?"

# 营地登录表单里的 RSA 公钥（不是签名校验，而是给 specialEncodeParam 做分块加密）
CAMP_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC0h62mV/zjJtFsNdfFNlxksfUOpjDI2KCcBrPiA8T7"
    "szABT4InLDTrdXAW84QyGNiazB0i7pgPCNGSAYbiJrCRutZ5jQsVS0Wg/RnXfwVQDJcAHJDjP5IXyroe"
    "LX7NUxDai8nPcpfRsvq6sneobyPexZSH0TlVSnecsJZTj5wu/wIDAQAB"
)

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 扫码页在手机 UA 下才会给二维码版式
QR_UA = (
    "Mozilla/5.0 (Linux; Android 15; V2366GA Build/V417IR; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Safari/537.36 "
    "tencent_game_emulator"
)

COMMON_HEADERS = {
    "Content-Encrypt": "",
    "Accept-Encrypt": "",
    "NOENCRYPT": "1",
    "X-Client-Proto": "https",
    "User-Agent": "okhttp/4.9.1",
}

CODE_RE = re.compile(r"[?&]code=([0-9A-Za-z]+)")
CODE_ENCODED_RE = re.compile(r"(?:code%3D)([0-9A-Za-z]+)", re.IGNORECASE)
# ptqrlogin 的响应体里 & 可能被转义成 \u0026
CODE_ESCAPED_RE = re.compile(r"(?:\\u0026|&amp;|%26)code=([0-9A-Za-z]+)", re.IGNORECASE)

QQ_QR_SELECTORS = (
    "#qrlogin_img, .qrlogin_img, img[src*='ptqrshow'], img[src*='qrcode'], "
    "canvas#qrlogin_canvas, .qrlogin canvas"
)

SCAN_TEXT_RE = re.compile(r"扫码成功|请在手机上确认|已扫码|确认登录|扫描成功")


class LoginError(Exception):
    """扫码登录失败，code 供上层转成用户可读提示。"""

    def __init__(self, message: str, code: str = "LOGIN_FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Account:
    """营地登录成功后的账号数据。"""

    platform: str
    user_id: str
    token: str
    user_key: str = ""
    nickname: str = ""
    avatar: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class LoginSession:
    """一次扫码会话。QQ 会话持有浏览器对象，必须调用 close_session 释放。"""

    platform: str
    owner_user_id: str
    owner_bot_id: str
    qr_bytes: bytes
    created_at: float = field(default_factory=time.time)
    # 微信
    wx_uuid: str = ""
    wx_xlog_uid: str = ""
    # QQ
    qq_playwright: Any = None
    qq_browser: Any = None
    qq_context: Any = None
    qq_page: Any = None
    qq_code_future: Optional[asyncio.Future] = None
    closed: bool = False
    # 取消信号：微信长轮询可能挂起十几秒，靠它把等待立刻打断
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.created_at)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _build_uuid() -> str:
    return str(uuid.uuid4()).upper()


def _load_public_key():
    return serialization.load_der_public_key(base64.b64decode(CAMP_PUBLIC_KEY))


_PUBLIC_KEY = None


def _public_key():
    global _PUBLIC_KEY
    if _PUBLIC_KEY is None:
        _PUBLIC_KEY = _load_public_key()
    return _PUBLIC_KEY


def _rsa_encrypt_chunked(data: bytes) -> bytes:
    """等价于 Node 的 crypto.publicEncrypt(RSA_PKCS1_PADDING)，按 117 字节分块。"""
    key = _public_key()
    chunks = [key.encrypt(data[i : i + 117], padding.PKCS1v15()) for i in range(0, len(data), 117)]
    return b"".join(chunks)


def build_special_encode_param() -> str:
    """生成营地登录接口要求的设备信息密文。"""
    timestamp = int(time.time() * 1000)
    device_id = uuid.uuid4().hex
    payload = {
        "timestamp": timestamp,
        "nonce": f":{uuid.uuid4().hex}:{timestamp}",
        "cDeviceId": device_id,
        "deviceid": device_id,
        "cDeviceImei": device_id[:15],
        "cDeviceMac": "02:00:00:00:00:00",
        "cDevicePPI": 480,
        "cDeviceScreenWidth": 1080,
        "cDeviceScreenHeight": 2400,
        "cDeviceBrand": "OnePlus",
        "cDeviceModel": "PHK110",
        "cDeviceMem": 12 * 1024 * 1024 * 1024,
        "cDeviceCPU": "SM8650",
        "cSystemVersionCode": "34",
        "cDeviceNet": "WIFI",
        "cDeviceSP": "China Mobile",
        "cDeviceOaid": device_id,
        "deviceLevel": 3,
        "px": 0,
        "py": 0,
        "wifi_ssid": "unknown",
        "wifi_mac": "02:00:00:00:00:00",
    }
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(_rsa_encrypt_chunked(plain)).decode("ascii")


def _manual_public_decrypt(signature: bytes) -> bytes:
    """RSA 公钥运算 + PKCS#1 v1.5 去填充，等价于 Node 的 crypto.publicDecrypt。"""
    key = _public_key()
    numbers = key.public_numbers()
    key_size = (numbers.n.bit_length() + 7) // 8
    block = pow(int.from_bytes(signature, "big"), numbers.e, numbers.n).to_bytes(key_size, "big")
    if len(block) < 11 or block[0] != 0x00 or block[1] != 0x01:
        raise ValueError("encodeRes 不是合法的 PKCS#1 v1.5 密文")
    separator = block.find(b"\x00", 2)
    if separator < 0:
        raise ValueError("encodeRes 填充格式错误")
    return block[separator + 1 :]


def decode_encode_res(encode_res: str) -> dict:
    """解开营地登录返回的 encodeRes，拿到 userKey 等字段。"""
    if not encode_res:
        return {}
    signature = base64.b64decode(encode_res)
    try:
        plain = _public_key().recover_data_from_signature(signature, padding.PKCS1v15(), None)
    except Exception:
        try:
            plain = _manual_public_decrypt(signature)
        except Exception:
            return {}
    try:
        data = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # 少数情况下公钥运算结果会带 DigestInfo 前缀，手动去填充再试一次
        try:
            data = json.loads(_manual_public_decrypt(signature).decode("utf-8"))
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


async def _read_json(resp: aiohttp.ClientResponse) -> dict:
    text = await resp.text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise LoginError(f"营地返回了非 JSON 数据（HTTP {resp.status}）", "BAD_RESPONSE")
    if not isinstance(data, dict):
        raise LoginError("营地返回数据格式异常", "BAD_RESPONSE")
    return data


def _session_timeout(total: float = 30.0) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=total)


def _ssl_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=False)


# ---------------------------------------------------------------------------
# 微信扫码
# ---------------------------------------------------------------------------


async def _fetch_wx_ticket(x_log_uid: str) -> str:
    async with aiohttp.ClientSession(timeout=_session_timeout(), connector=_ssl_connector()) as client:
        async with client.post(
            f"{CAMP_BASE_URL}/a/getwxsdkticket",
            headers={**COMMON_HEADERS, "x-log-uid": x_log_uid},
        ) as resp:
            data = await _read_json(resp)
    ticket = (data.get("data") or {}).get("sdkTicket")
    if data.get("returnCode") != 0 or not ticket:
        raise LoginError("获取微信登录 SDK Ticket 失败，请稍后重试", "WX_TICKET_FAILED")
    return str(ticket)


async def _fetch_wx_qr(ticket: str) -> tuple[str, bytes]:
    nonce = "".join(str(random.SystemRandom().randrange(10)) for _ in range(8))
    timestamp = str(int(time.time()))
    signature = hashlib.sha1(
        f"appid={WX_APPID}&noncestr={nonce}&sdk_ticket={ticket}&timestamp={timestamp}".encode("utf-8")
    ).hexdigest()
    params = {
        "appid": WX_APPID,
        "noncestr": nonce,
        "timestamp": timestamp,
        "scope": "snsapi_userinfo",
        "signature": signature,
    }
    async with aiohttp.ClientSession(timeout=_session_timeout(), connector=_ssl_connector()) as client:
        async with client.get(WX_QR_URL, params=params) as resp:
            data = await _read_json(resp)
    qr_base64 = (data.get("qrcode") or {}).get("qrcodebase64")
    wx_uuid = data.get("uuid")
    if data.get("errcode") != 0 or not qr_base64 or not wx_uuid:
        raise LoginError(f"获取微信登录二维码失败：{data.get('errmsg') or '未知错误'}", "WX_QR_FAILED")
    try:
        qr_bytes = base64.b64decode(qr_base64)
    except (ValueError, TypeError) as exc:
        raise LoginError("微信登录二维码解码失败", "WX_QR_FAILED") from exc
    return str(wx_uuid), qr_bytes


async def create_wx_session(owner_user_id: str, owner_bot_id: str) -> LoginSession:
    x_log_uid = _build_uuid()
    ticket = await _fetch_wx_ticket(x_log_uid)
    wx_uuid, qr_bytes = await _fetch_wx_qr(ticket)
    logger.info(f"[王者扫码] 微信二维码已生成 owner={owner_user_id}")
    return LoginSession(
        platform="wx",
        owner_user_id=owner_user_id,
        owner_bot_id=owner_bot_id,
        qr_bytes=qr_bytes,
        wx_uuid=wx_uuid,
        wx_xlog_uid=x_log_uid,
    )


async def _poll_wx(client: aiohttp.ClientSession, wx_uuid: str) -> dict:
    async with client.get(WX_POLL_URL, params={"f": "json", "uuid": wx_uuid}) as resp:
        return await _read_json(resp)


async def _login_camp_by_wx(code: str, x_log_uid: str) -> Account:
    special = build_special_encode_param()
    form = {
        "loginType": "wx",
        "code": code,
        "delOldUser": "0",
        "key1": uuid.uuid4().hex,
        "lastLoginTime": "0",
        "lastGetRemarkTime": "0",
        "cChannelId": "10003391",
        "cClientVersionCode": "2057957801",
        "cClientVersionName": "10.111.0323",
        "cCurrentGameId": "20001",
        "cGameId": "20001",
        "cGzip": "1",
        "cIsArm64": "true",
        "cRand": str(int(time.time() * 1000)),
        "cSupportArm64": "true",
        "cSystem": "android",
        "cSystemVersionCode": "34",
        "cSystemVersionName": "14",
        "cpuHardware": "qcom",
        "gameId": "20001",
        "tinkerId": "2057957801_64_0",
        "specialEncodeParam": special,
    }
    headers = {
        **COMMON_HEADERS,
        "x-log-uid": x_log_uid,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "cChannelId": "10003391",
        "cClientVersionCode": "2057957801",
        "cClientVersionName": "10.111.0323",
        "cCurrentGameId": "20001",
        "cGameId": "20001",
        "cGzip": "1",
        "cIsArm64": "true",
        "cRand": str(int(time.time() * 1000)),
        "cSupportArm64": "true",
        "cSystem": "android",
        "cSystemVersionCode": "34",
        "cSystemVersionName": "14",
        "cpuHardware": "qcom",
        "gameId": "20001",
        "tinkerId": "2057957801_64_0",
        "specialEncodeParam": special,
    }
    async with aiohttp.ClientSession(timeout=_session_timeout(), connector=_ssl_connector()) as client:
        async with client.post(CAMP_LOGIN_URL, headers=headers, data=form) as resp:
            data = await _read_json(resp)

    payload = data.get("data") or {}
    if data.get("returnCode") != 0 or not payload.get("userId") or not payload.get("token"):
        message = data.get("returnMsg") or data.get("errmsg") or "营地登录失败"
        raise LoginError(f"营地登录失败：{message}", "CAMP_LOGIN_FAILED")
    return _account_from_payload("wx", payload)


async def _wait_wx(
    session: LoginSession,
    timeout: float,
    poll_interval: float,
    on_status: Optional[Callable[[str], Awaitable[None]]],
) -> Account:
    deadline = time.monotonic() + timeout
    scanned_notified = False
    failures = 0
    async with aiohttp.ClientSession(timeout=_session_timeout(20), connector=_ssl_connector()) as client:

        async def _poll_once() -> dict:
            """长轮询可能挂十几秒，和取消信号赛跑，保证取消能立刻生效。"""
            poll_task = asyncio.create_task(_poll_wx(client, session.wx_uuid))
            cancel_task = asyncio.create_task(session.cancel_event.wait())
            done, pending = await asyncio.wait({poll_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if cancel_task in done or session.cancel_event.is_set():
                poll_task.cancel()
                await asyncio.gather(poll_task, cancel_task, return_exceptions=True)
                raise LoginError("扫码登录已取消", "QR_CANCELED")
            await asyncio.gather(cancel_task, return_exceptions=True)
            return await poll_task

        async def _sleep(seconds: float) -> None:
            if session.cancel_event.is_set():
                raise LoginError("扫码登录已取消", "QR_CANCELED")
            try:
                await asyncio.wait_for(session.cancel_event.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                return
            raise LoginError("扫码登录已取消", "QR_CANCELED")

        while time.monotonic() < deadline:
            if session.closed or session.cancel_event.is_set():
                raise LoginError("扫码登录已取消", "QR_CANCELED")
            try:
                data = await _poll_once()
                failures = 0
            except LoginError as exc:
                if exc.code == "QR_CANCELED":
                    raise
                failures += 1
                logger.warning(f"[王者扫码] 微信轮询失败（{failures}/5）：{exc}")
                if failures >= 5:
                    raise LoginError("微信扫码状态查询连续失败，请检查网络后重试", "WX_POLL_FAILED") from exc
                await _sleep(poll_interval)
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                failures += 1
                logger.warning(f"[王者扫码] 微信轮询失败（{failures}/5）：{exc}")
                if failures >= 5:
                    raise LoginError("微信扫码状态查询连续失败，请检查网络后重试", "WX_POLL_FAILED") from exc
                await _sleep(poll_interval)
                continue

            try:
                status = int(data.get("wx_errcode") or data.get("errcode") or 0)
            except (TypeError, ValueError):
                status = 0
            code = str(data.get("wx_code") or data.get("code") or "")

            if status in (404, 405) and not scanned_notified:
                scanned_notified = True
                if on_status is not None:
                    try:
                        await on_status("scanned")
                    except Exception:
                        pass

            if code and status == 405:
                return await _login_camp_by_wx(code, session.wx_xlog_uid)

            if status == 402:
                raise LoginError("微信登录二维码已过期，请重新发起", "QR_EXPIRED")
            if status == 403:
                raise LoginError("微信登录已取消，请重新发起", "QR_CANCELED")
            if status == 500:
                raise LoginError("微信登录服务异常，请稍后再试", "WX_SERVER_ERROR")

            await _sleep(poll_interval)

    raise LoginError("等待微信扫码超时，请重新发起", "QR_TIMEOUT")


# ---------------------------------------------------------------------------
# QQ 扫码
# ---------------------------------------------------------------------------


async def _launch_qq_browser(playwright, channel: str, headless: bool = True):
    """按配置尝试可用的浏览器通道。返回 (browser, channel_desc)。"""
    if channel == "auto" or not channel:
        candidates = ["chrome", "msedge", "chromium"]
    else:
        candidates = [channel]

    errors: list[str] = []
    for candidate in candidates:
        kwargs: dict[str, Any] = {
            "headless": headless,
            "args": [
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--no-sandbox",
                "--no-zygote",
                "--disable-dev-shm-usage",
            ],
            "timeout": 60_000,
        }
        if candidate != "chromium":
            kwargs["channel"] = candidate
        try:
            browser = await playwright.chromium.launch(**kwargs)
            logger.info(f"[王者扫码] QQ 登录浏览器已启动：{candidate}")
            return browser, candidate
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {exc}")
            logger.warning(f"[王者扫码] QQ 浏览器 {candidate} 启动失败：{exc}")
    raise LoginError(
        "浏览器启动失败，无法发起 QQ 扫码。请安装 Chrome/Edge，"
        "或在控制台把「QQ登录浏览器」改成可用的一种。",
        "BROWSER_UNAVAILABLE",
    )


async def _inspect_qq_response(resp, future: asyncio.Future) -> None:
    try:
        if "ptqrlogin" not in resp.url:
            return
        text = await resp.text()
        match = CODE_RE.search(text) or CODE_ENCODED_RE.search(text) or CODE_ESCAPED_RE.search(text)
        if match and not future.done():
            future.set_result(match.group(1))
    except Exception:
        return


async def _wait_qq_qr(page, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            element = await page.query_selector(QQ_QR_SELECTORS)
            if element is not None:
                box = await element.bounding_box()
                if box and box.get("width", 0) > 40 and box.get("height", 0) > 40:
                    return
        except Exception:
            pass
        await asyncio.sleep(0.8)
    raise LoginError("未能加载 QQ 登录二维码，请稍后重试", "QQ_QR_FAILED")


async def _grab_qq_qr(page) -> bytes:
    try:
        element = await page.query_selector(QQ_QR_SELECTORS)
        if element is not None:
            data = await element.screenshot(type="png")
            if data:
                return data
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[王者扫码] QQ 二维码元素截图失败，改用整页截图：{exc}")

    try:
        data = await page.screenshot(type="png", full_page=False)
        if data:
            return data
    except Exception as exc:  # noqa: BLE001
        raise LoginError("QQ 二维码截图失败，请稍后重试", "QQ_QR_FAILED") from exc
    raise LoginError("QQ 二维码截图失败，请稍后重试", "QQ_QR_FAILED")


async def create_qq_session(
    owner_user_id: str,
    owner_bot_id: str,
    browser_channel: str = "auto",
    headless: bool = True,
    page_timeout: float = 90.0,
) -> LoginSession:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise LoginError(
            "当前环境没有安装 playwright，无法使用 QQ 扫码；可改用微信扫码",
            "PLAYWRIGHT_MISSING",
        ) from exc

    playwright = await async_playwright().start()
    browser = None
    context = None
    page = None
    try:
        browser, _ = await _launch_qq_browser(playwright, browser_channel, headless=headless)
        context = await browser.new_context(
            user_agent=QR_UA,
            viewport={"width": 420, "height": 760},
            is_mobile=True,
            locale="zh-CN",
        )
        page = await context.new_page()
        loop = asyncio.get_running_loop()
        code_future: asyncio.Future = loop.create_future()

        def _on_request(request) -> None:
            url = request.url
            if "tauth.qq.com" not in url and not url.startswith("auth://"):
                return
            match = CODE_RE.search(url)
            if match and not code_future.done():
                code_future.set_result(match.group(1))

        page.on("request", _on_request)
        page.on("response", lambda resp: asyncio.create_task(_inspect_qq_response(resp, code_future)))

        try:
            await page.goto(QQ_LOGIN_PAGE, wait_until="domcontentloaded", timeout=page_timeout * 1000)
        except Exception as exc:  # noqa: BLE001
            raise LoginError("打开 QQ 授权页失败，请稍后重试", "QQ_PAGE_FAILED") from exc

        await _wait_qq_qr(page, timeout=min(60.0, page_timeout))
        qr_bytes = await _grab_qq_qr(page)

        logger.info(f"[王者扫码] QQ 二维码已生成 owner={owner_user_id} headless={headless}")
        return LoginSession(
            platform="qq",
            owner_user_id=owner_user_id,
            owner_bot_id=owner_bot_id,
            qr_bytes=qr_bytes,
            qq_playwright=playwright,
            qq_browser=browser,
            qq_context=context,
            qq_page=page,
            qq_code_future=code_future,
        )
    except Exception:
        for closer in (
            lambda: page.close() if page is not None else None,
            lambda: context.close() if context is not None else None,
            lambda: browser.close() if browser is not None else None,
            lambda: playwright.stop(),
        ):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        raise


async def _exchange_qq_code(code: str) -> dict:
    timestamp = str(int(time.time()))
    body = json.dumps({"appID": QQ_APPID, "loginCode": code}, separators=(",", ":"))
    sign_str = f"POST\n/cmd/QQCodeLogin\njson\nysdk\n{timestamp}\n{body}"
    digest = base64.b64encode(
        hmac.new(b"yyb@cloud_game:CQ8FA#", sign_str.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        "Content-Type": "json",
        "Auth-Secret-ID": "ysdk",
        "Auth-Secret-Digest": digest,
        "Auth-Request-Time": timestamp,
    }
    async with aiohttp.ClientSession(timeout=_session_timeout(), connector=_ssl_connector()) as client:
        async with client.post(QQ_TOKEN_URL, headers=headers, data=body.encode("utf-8")) as resp:
            data = await _read_json(resp)

    payload = data.get("data") or {}
    if data.get("code") != 0 or payload.get("ret") != 0:
        message = data.get("errmsg") or payload.get("errmsg") or "换取 QQ 登录凭证失败"
        raise LoginError(f"QQ 授权失败：{message}", "QQ_TOKEN_FAILED")
    return {
        "accessToken": str(payload.get("accessToken") or ""),
        "openId": str(payload.get("openID") or ""),
        "payToken": str(payload.get("payToken") or ""),
        "refreshToken": str(payload.get("refreshToken") or ""),
    }


async def _login_camp_by_qq(tokens: dict) -> Account:
    special = build_special_encode_param()
    form = {
        "delOldUser": "0",
        "key1": uuid.uuid4().hex,
        "lastLoginTime": "0",
        "lastGetRemarkTime": "0",
        "cChannelId": "10003391",
        "cClientVersionCode": "2057971306",
        "cClientVersionName": "10.114.0826",
        "cCurrentGameId": "20001",
        "cGameId": "20001",
        "cGzip": "1",
        "cIsArm64": "true",
        "cRand": str(int(time.time() * 1000)),
        "cSupportArm64": "true",
        "cSystem": "android",
        "cSystemVersionCode": "35",
        "cSystemVersionName": "15",
        "cpuHardware": "qcom",
        "gameId": "20001",
        "tinkerId": "2057971306_64_0",
        "specialEncodeParam": special,
        "loginType": "openSdk",
        "accessToken": tokens.get("accessToken", ""),
        "openId": tokens.get("openId", ""),
        "payToken": tokens.get("payToken", ""),
    }
    headers = {
        **COMMON_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-log-uid": _build_uuid(),
    }
    async with aiohttp.ClientSession(timeout=_session_timeout(), connector=_ssl_connector()) as client:
        async with client.post(CAMP_LOGIN_URL, headers=headers, data=form) as resp:
            data = await _read_json(resp)

    payload = data.get("data") or {}
    if data.get("returnCode") != 0 or not payload.get("userId") or not payload.get("token"):
        message = data.get("returnMsg") or data.get("errmsg") or "营地登录失败"
        raise LoginError(f"营地登录失败：{message}", "CAMP_LOGIN_FAILED")
    account = _account_from_payload("qq", payload)
    account.raw["accessToken"] = tokens.get("accessToken", "")
    account.raw["refreshToken"] = tokens.get("refreshToken", "")
    return account


async def _wait_qq(
    session: LoginSession,
    timeout: float,
    poll_interval: float,
    on_status: Optional[Callable[[str], Awaitable[None]]],
) -> Account:
    page = session.qq_page
    future = session.qq_code_future
    if page is None or future is None:
        raise LoginError("QQ 登录会话已失效，请重新发起", "QQ_SESSION_LOST")

    deadline = time.monotonic() + timeout
    scanned_notified = False
    while time.monotonic() < deadline:
        if future.done():
            code = future.result()
            if code:
                tokens = await _exchange_qq_code(code)
                return await _login_camp_by_qq(tokens)

        try:
            if page.is_closed():
                raise LoginError("QQ 登录页面已关闭，请重新发起", "QR_CANCELED")
            current_url = page.url
        except LoginError:
            raise
        except Exception:
            current_url = ""
        match = CODE_RE.search(current_url or "")
        if match:
            tokens = await _exchange_qq_code(match.group(1))
            return await _login_camp_by_qq(tokens)

        if not scanned_notified:
            try:
                text = await page.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 500) : ''"
                )
                if text and SCAN_TEXT_RE.search(text):
                    scanned_notified = True
                    if on_status is not None:
                        try:
                            await on_status("scanned")
                        except Exception:
                            pass
            except Exception:
                pass

        await asyncio.sleep(max(0.4, poll_interval / 2))

    raise LoginError("等待 QQ 扫码超时，请重新发起", "QR_TIMEOUT")


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def _account_from_payload(platform: str, payload: dict) -> Account:
    encode_res = str(payload.get("encodeRes") or "")
    decoded = decode_encode_res(encode_res) if encode_res else {}
    return Account(
        platform=platform,
        user_id=str(payload.get("userId") or ""),
        token=str(payload.get("token") or ""),
        user_key=str(decoded.get("userKey") or ""),
        nickname=str(payload.get("nickname") or payload.get("snsnickname") or ""),
        avatar=str(payload.get("avatar") or payload.get("bigAvatar") or ""),
        raw={k: payload.get(k) for k in ("userId", "unionId", "appOpenid", "expires", "realRegisterTime")},
    )


async def create_session(
    platform: str,
    owner_user_id: str,
    owner_bot_id: str,
    *,
    qq_browser: str = "auto",
    qq_headless: bool = True,
    qq_page_timeout: float = 90.0,
) -> LoginSession:
    if platform == "wx":
        return await create_wx_session(owner_user_id, owner_bot_id)
    if platform == "qq":
        return await create_qq_session(
            owner_user_id,
            owner_bot_id,
            browser_channel=qq_browser,
            headless=qq_headless,
            page_timeout=qq_page_timeout,
        )
    raise LoginError(f"未知的扫码方式：{platform}", "BAD_PLATFORM")


async def wait_for_login(
    session: LoginSession,
    timeout: float,
    poll_interval: float = 1.5,
    on_status: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Account:
    if session.platform == "wx":
        return await _wait_wx(session, timeout, poll_interval, on_status)
    if session.platform == "qq":
        return await _wait_qq(session, timeout, poll_interval, on_status)
    raise LoginError(f"未知的扫码方式：{session.platform}", "BAD_PLATFORM")


async def close_session(session: LoginSession) -> None:
    """关闭扫码会话，释放 QQ 浏览器。可重复调用。"""
    if session is None or session.closed:
        return
    session.closed = True
    session.cancel_event.set()
    for name in ("qq_page", "qq_context", "qq_browser"):
        target = getattr(session, name, None)
        if target is None:
            continue
        try:
            await target.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[王者扫码] 关闭 {name} 失败：{exc}")
    if session.qq_playwright is not None:
        try:
            await session.qq_playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[王者扫码] 停止 playwright 失败：{exc}")


async def validate_cookie(uid: str, token: str, timeout: float = 15.0) -> tuple[Optional[bool], str]:
    """校验营地 CK 是否有效。True 有效 / False 明确失效 / None 无法判断（网络异常）。"""
    if not uid or not token:
        return False, "缺少 UID 或 CK"
    headers = {
        "Host": "ssl.kohsocialapp.qq.com:10001",
        "origin": "https://kohcamp.qq.com",
        "istrpcrequest": "true",
        "cchannelid": "2002",
        "cclientversioncode": "2047933506",
        "cclientversionname": "9.103.0723",
        "ccurrentgameid": "20001",
        "cgameid": "20001",
        "cgzip": "1",
        "cisarm64": "true",
        "crand": str(int(time.time() * 1000)),
        "csupportarm64": "true",
        "csystem": "android",
        "csystemversioncode": "35",
        "csystemversionname": "15",
        "cpuhardware": "qcom",
        "gameareaid": "0",
        "gameid": "20001",
        "gameserverid": "0",
        "gameroleid": "0",
        "gameusersex": "1",
        "openid": "E66DC1CFC7F2EFA94561DD7F09EF7975",
        "tinkerid": "2047933506_64_0",
        "content-encrypt": "",
        "accept-encrypt": "",
        "noencrypt": "1",
        "x-client-proto": "https",
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": "okhttp/4.9.1",
        "kohdimgender": "1",
        "token": token,
        "userid": uid,
    }
    data = {"friendUserId": uid, "token": token, "userId": uid}
    try:
        async with aiohttp.ClientSession(timeout=_session_timeout(timeout), connector=_ssl_connector()) as client:
            async with client.post(CAMP_ROLE_LIST_URL, headers=headers, data=data) as resp:
                payload = await _read_json(resp)
    except (aiohttp.ClientError, asyncio.TimeoutError, LoginError, ValueError) as exc:
        return None, f"网络异常：{exc}"

    return_code = payload.get("returnCode")
    if return_code == 0:
        return True, "有效"
    message = str(payload.get("returnMsg") or return_code)
    return False, message
