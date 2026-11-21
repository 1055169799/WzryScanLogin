"""把扫码得到的登录态同步到 WzryUID 自己的账号池 AuthPool.json。

为什么需要它：
    WzryUID 新版（见 WzryUID/utils/camp_auth.py）所有业务接口都从本插件自己的
    data/WzryUID/AuthPool.json 按营地 uid 取新版签名登录态；旧的数据库
    wzryuser.cookie 路径已无业务调用者。WzryScanLogin 只写数据库的话，
    扫码"成功"后 WzryUID 依然查不到登录态（-61）。

字段契约参考 GloryOfKings-Plugin 的 authStore.js（仅参考逻辑，不共享文件）：
    - 字段集合 / 顺序对齐 ACCOUNT_FIELDS
    - 新登录态清掉 authInvalid 等失效标记（等价于 upsertGlobalAccount 的 resetAuthState）
    - 原子写（临时文件 + os.replace），避免并发写坏文件
"""
from __future__ import annotations

import os
import json
import time
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from gsuid_core.data_store import get_res_path
from gsuid_core.logger import logger

# 与 WzryUID/utils/camp_auth.py 的 AUTH_POOL_PATH 保持一致（同一 gsuid_core 数据目录）
# 用 get_res_path 动态解析，不含任何其它项目的绝对路径
AUTH_POOL_PATH = get_res_path('WzryUID') / 'AuthPool.json'

# 参考插件 authStore.js 的 ACCOUNT_FIELDS 顺序；新账号按这个顺序落盘，
# 免得主人手工救数据时面对一份键顺序全乱的文件。
ACCOUNT_FIELDS = (
    'userId', 'token', 'userKey', 'encodeRes',
    'openId', 'gameOpenId', 'gameRoleId', 'gameServerId', 'gameAreaId',
    'gameUserSex', 'kohDimGender', 'xLogUid', 'traceparent',
    'accessToken', 'refreshToken', 'appOpenid',
    'avatar', 'bigAvatar', 'icon', 'nickname', 'snsnickname', 'userName',
    'sex', 'expires', 'uin', 'userSig', 'realRegisterTime',
    'ownerBotUserId', 'loginPlatform', 'remark',
    'isGlobalDefault', 'priority',
    'authInvalid', 'authErrorCount', 'lastAuthErrorAt', 'lastAuthErrorMessage',
    'lastSuccessAt', 'createdAt', 'updatedAt', 'lastLoginAt',
)

_WRITE_LOCK = asyncio.Lock()


def _now_iso() -> str:
    """参考插件用 ISO 8601（毫秒 + Z），这里保持同一形状。"""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _login_platform(platform: str) -> str:
    """参考插件用 'qq' / 'wechat' 两种取值。"""
    return 'qq' if platform == 'qq' else 'wechat'


def is_usable_auth(auth: Dict[str, Any]) -> bool:
    """与参考插件 isUsableAuth 同口径：三样齐才算可用登录态。"""
    return bool(
        auth.get('token') and auth.get('userId')
        and (auth.get('userKey') or auth.get('encodeRes'))
    )


def _build_entry(account, owner_bot_user_id: str, is_global: bool, existing: Dict[str, Any]) -> Dict[str, Any]:
    """把扫码拿到的 Account 摊平成 AuthPool 账号，字段值按新值优先、缺值沿用旧值。"""
    raw: Dict[str, Any] = getattr(account, 'raw', {}) or {}
    now = _now_iso()

    incoming: Dict[str, Any] = {
        'userId': _text(getattr(account, 'user_id', '')),
        'token': _text(getattr(account, 'token', '')),
        'userKey': _text(getattr(account, 'user_key', '')),
        'encodeRes': _text(raw.get('encodeRes')),
        'openId': _text(raw.get('openId')),
        'appOpenid': _text(raw.get('appOpenid')),
        'accessToken': _text(raw.get('accessToken')),
        'refreshToken': _text(raw.get('refreshToken')),
        'avatar': _text(getattr(account, 'avatar', '')) or _text(raw.get('avatar')),
        'bigAvatar': _text(raw.get('bigAvatar')),
        'icon': _text(raw.get('icon')),
        'nickname': _text(getattr(account, 'nickname', '')) or _text(raw.get('nickname')),
        'snsnickname': _text(raw.get('snsnickname')),
        'userName': _text(raw.get('userName')),
        'sex': _text(raw.get('sex')),
        'expires': _text(raw.get('expires')),
        'uin': _text(raw.get('uin')),
        'userSig': _text(raw.get('userSig')),
        'realRegisterTime': _text(raw.get('realRegisterTime')),
        'ownerBotUserId': _text(owner_bot_user_id),
        'loginPlatform': _login_platform(_text(getattr(account, 'platform', ''))),
        # 新登录 = 重置失效标记，等价于 authStore.upsertGlobalAccount 的 resetAuthState
        'isGlobalDefault': bool(is_global),
        'priority': 100,
        'authInvalid': False,
        'authErrorCount': 0,
        'lastAuthErrorAt': '',
        'lastAuthErrorMessage': '',
        'createdAt': _text(existing.get('createdAt')) or now,
        'updatedAt': now,
        'lastLoginAt': now,
    }

    # 先铺旧账号保住手工加过的字段，再按字段表逐项覆盖（覆盖不改键位置）
    entry: Dict[str, Any] = {**existing, **incoming}
    for key in ACCOUNT_FIELDS:
        if key not in entry:
            entry[key] = '' if key not in ('priority', 'authErrorCount') else 0
    # 按字段表重排，未登记字段放最后
    ordered = {key: entry[key] for key in ACCOUNT_FIELDS if key in entry}
    ordered.update({key: value for key, value in entry.items() if key not in ordered})
    return ordered


def _read_pool(path: Path) -> Dict[str, Any]:
    """读账号池。坏文件按参考插件做法隔离留证，再按空池继续。"""
    if not path.exists():
        return {'accounts': {}}
    try:
        pool = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        quarantine = path.with_name(f'{path.name}.corrupt.{int(time.time())}')
        try:
            path.replace(quarantine)
            logger.warning(f'[王者扫码] 账号池解析失败，已隔离到 {quarantine.name}: {exc}')
        except Exception as move_exc:  # noqa: BLE001
            logger.error(f'[王者扫码] 账号池解析失败且无法隔离: {exc}; {move_exc}')
            raise
        return {'accounts': {}}
    if not isinstance(pool, dict):
        logger.warning('[王者扫码] 账号池顶层不是对象，按空池处理')
        return {'accounts': {}}
    accounts = pool.get('accounts')
    if not isinstance(accounts, dict):
        accounts = {}
    pool['accounts'] = accounts
    return pool


def _atomic_write(path: Path, pool: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    try:
        tmp.write_text(json.dumps(pool, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


async def sync_scan_account(
    account,
    owner_bot_user_id: str,
    *,
    path: Optional[Path] = None,
    is_global: bool = True,
    backend: str = 'auth_pool',
) -> Dict[str, Any]:
    """把扫码账号写入账号池。返回 {'ok': bool, 'note': str, 'count': int}。"""
    target = Path(path) if path else AUTH_POOL_PATH
    if not _text(getattr(account, 'user_id', '')) or not _text(getattr(account, 'token', '')):
        return {'ok': False, 'note': '登录数据不完整', 'count': -1}

    async with _WRITE_LOCK:
        try:
            pool = await asyncio.to_thread(_read_pool, target)
            accounts = pool['accounts']
            uid = _text(account.user_id)
            existing = accounts.get(uid)
            if not isinstance(existing, dict):
                existing = {}
            entry = _build_entry(account, owner_bot_user_id, is_global, existing)
            if not is_usable_auth(entry):
                return {
                    'ok': False,
                    'note': 'userKey/encodeRes 缺失，无法生成可用登录态',
                    'count': len(accounts),
                }
            accounts[uid] = entry
            await asyncio.to_thread(_atomic_write, target, pool)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f'[王者扫码] 写入账号池失败: {exc}')
            return {'ok': False, 'note': f'写入失败：{exc}', 'count': -1}

    global_count = sum(
        1 for item in accounts.values()
        if isinstance(item, dict) and item.get('isGlobalDefault')
    )
    logger.info(
        f'[王者扫码] 登录态已写入账号池 ({backend}) uid={uid} '
        f'global={is_global} 全局账号数={global_count}'
    )
    return {'ok': True, 'note': '已写入账号池', 'count': global_count}
