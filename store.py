"""把扫码得到的账号写入 WzryUID 使用的数据表。

WzryUID 需要的数据只有两项：

- ``wzryuser.uid``    -> 营地登录返回的 ``userId``（营地 UID）
- ``wzryuser.cookie`` -> 营地登录返回的 ``token``（也就是大家说的营地 CK）

``wzryuser.stoken`` 是 gsuid_core ``User`` 基类自带的字段，WzryUID 并没有使用，
所以这里留空。绑定关系写入 ``wzrybind.uid``，和「王者绑定UID」命令保存的位置一致。

为了不和 WzryUID 的代码产生硬依赖（避免它更新后 import 路径变化把我们带崩），
优先从 ``sys.modules`` 里找已经加载的 WzryUID 模型；找不到时退化为直接 SQL。
"""

from __future__ import annotations

import sys
import asyncio
from typing import Any, Callable, Iterable, Optional, Awaitable
from dataclasses import dataclass

from sqlalchemy import text

from gsuid_core.logger import logger
from gsuid_core.utils.database import base_models
from gsuid_core.utils.database.write_gate import WriteGateTimeout, sqlite_write_gate

from .login import validate_cookie

MODEL_CACHE: dict[str, Any] = {}


@dataclass
class SaveOutcome:
    ck_saved: bool = False
    backend: str = ""
    bound: int = -99  # 0 新绑定 / -2 已绑定 / 其它失败
    switched: int = -99
    note: str = ""


@dataclass
class AccountRow:
    id: int
    user_id: str
    bot_id: str
    uid: str
    cookie: str


def mask_value(value: str, head: int = 4, tail: int = 4) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= head + tail:
        return value[:2] + "***"
    return f"{value[:head]}***{value[-tail:]}"


def _find_model(class_name: str, table_name: str):
    """在已加载模块里找 WzryUID 的 SQLModel 类，不主动 import 它的内部路径。"""
    if class_name in MODEL_CACHE:
        cached = MODEL_CACHE[class_name]
        return cached or None

    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            candidate = getattr(module, class_name, None)
        except Exception:
            continue
        if not isinstance(candidate, type):
            continue
        if getattr(candidate, "__tablename__", None) != table_name:
            continue
        if not hasattr(candidate, "insert_data"):
            continue
        MODEL_CACHE[class_name] = candidate
        logger.debug(f"[王者扫码] 已找到 WzryUID 模型：{class_name} ({module.__name__})")
        return candidate

    # 不缓存“没找到”：插件加载顺序或热重载后 WzryUID 可能稍后才就绪
    return None


def _engine():
    return getattr(base_models, "engine", None)


async def _raw_save_ck(owner_id: str, bot_id: str, uid: str, token: str) -> bool:
    engine = _engine()
    if engine is None:
        logger.warning("[王者扫码] 数据库尚未初始化，无法写入 CK")
        return False

    try:
        async with sqlite_write_gate.hold(core=False, label="WzryScanLogin"):
            async with engine.begin() as conn:
                existing = (
                    await conn.execute(
                        text(
                            "SELECT id FROM wzryuser "
                            "WHERE user_id = :user_id AND bot_id = :bot_id AND uid = :uid LIMIT 1"
                        ),
                        {"user_id": owner_id, "bot_id": bot_id, "uid": uid},
                    )
                ).first()
                if existing is not None:
                    await conn.execute(
                        text("UPDATE wzryuser SET cookie = :cookie WHERE id = :id"),
                        {"cookie": token, "id": existing[0]},
                    )
                else:
                    await conn.execute(
                        text(
                            "INSERT INTO wzryuser "
                            "(bot_id, user_id, cookie, stoken, status, push_switch, sign_switch, uid) "
                            "VALUES (:bot_id, :user_id, :cookie, NULL, NULL, 'off', 'off', :uid)"
                        ),
                        {"bot_id": bot_id, "user_id": owner_id, "cookie": token, "uid": uid},
                    )
        return True
    except WriteGateTimeout:
        logger.warning("[王者扫码] 等待数据库写锁超时，CK 未保存")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[王者扫码] 直接写入 CK 失败：{exc}")
        return False


async def _raw_bind_uid(owner_id: str, bot_id: str, uid: str, group_id: Optional[str], switch: bool) -> int:
    engine = _engine()
    if engine is None:
        return -1

    try:
        async with sqlite_write_gate.hold(core=False, label="WzryScanLoginBind"):
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT id, uid, group_id FROM wzrybind "
                            "WHERE user_id = :user_id AND bot_id = :bot_id LIMIT 1"
                        ),
                        {"user_id": owner_id, "bot_id": bot_id},
                    )
                ).first()
                if row is None:
                    await conn.execute(
                        text(
                            "INSERT INTO wzrybind (bot_id, user_id, group_id, uid) "
                            "VALUES (:bot_id, :user_id, :group_id, :uid)"
                        ),
                        {"bot_id": bot_id, "user_id": owner_id, "group_id": group_id, "uid": uid},
                    )
                    return 0

                row_id, current, current_group = row[0], str(row[1] or ""), row[2]
                uid_list = [item for item in current.split("_") if item]
                already = uid in uid_list
                if not already:
                    uid_list.append(uid)
                if switch and uid in uid_list:
                    uid_list.remove(uid)
                    uid_list.insert(0, uid)
                new_uid = "_".join(uid_list)
                new_group = current_group if current_group is not None else group_id
                await conn.execute(
                    text("UPDATE wzrybind SET uid = :uid, group_id = :group_id WHERE id = :id"),
                    {"uid": new_uid, "group_id": new_group, "id": row_id},
                )
                return -2 if already else 0
    except WriteGateTimeout:
        logger.warning("[王者扫码] 等待数据库写锁超时，UID 未绑定")
        return -1
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[王者扫码] 直接绑定 UID 失败：{exc}")
        return -1


async def save_account(
    owner_id: str,
    bot_id: str,
    account,
    *,
    auto_save: bool = True,
    auto_bind: bool = True,
    auto_switch: bool = True,
    group_id: Optional[str] = None,
) -> SaveOutcome:
    """保存营地 CK（以及可选的 UID 绑定）。"""
    outcome = SaveOutcome()

    if auto_save:
        model = _find_model("WzryUser", "wzryuser")
        if model is not None:
            try:
                await model.insert_data(owner_id, bot_id, cookie=account.token, uid=account.user_id)
                outcome.ck_saved = True
                outcome.backend = "model"
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[王者扫码] 模型写入失败，改用 SQL：{exc}")
                outcome.ck_saved = await _raw_save_ck(owner_id, bot_id, account.user_id, account.token)
                outcome.backend = "sql" if outcome.ck_saved else ""
        else:
            outcome.ck_saved = await _raw_save_ck(owner_id, bot_id, account.user_id, account.token)
            outcome.backend = "sql" if outcome.ck_saved else ""

    if auto_bind:
        model = _find_model("WzryBind", "wzrybind")
        if model is not None:
            try:
                outcome.bound = await model.insert_uid(owner_id, bot_id, account.user_id, group_id)
                if auto_switch and outcome.bound in (0, -2):
                    outcome.switched = await model.switch_uid_by_game(owner_id, bot_id, account.user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[王者扫码] 模型绑定失败，改用 SQL：{exc}")
                outcome.bound = await _raw_bind_uid(owner_id, bot_id, account.user_id, group_id, auto_switch)
                outcome.switched = 0 if auto_switch and outcome.bound in (0, -2) else -99
        else:
            outcome.bound = await _raw_bind_uid(owner_id, bot_id, account.user_id, group_id, auto_switch)
            outcome.switched = 0 if auto_switch and outcome.bound in (0, -2) else -99

    if not outcome.ck_saved and auto_save:
        outcome.note = "CK 写入失败"
    return outcome


async def list_accounts(owner_id: Optional[str] = None) -> list[AccountRow]:
    """列出 WzryUID 账号池。owner_id 为空时返回全部。"""
    engine = _engine()
    if engine is None:
        return []
    sql = "SELECT id, user_id, bot_id, uid, cookie FROM wzryuser"
    params: dict[str, Any] = {}
    if owner_id:
        sql += " WHERE user_id = :user_id"
        params["user_id"] = owner_id
    sql += " ORDER BY id ASC"
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[王者扫码] 读取账号池失败：{exc}")
        return []

    return [
        AccountRow(
            id=int(row[0]),
            user_id=str(row[1] or ""),
            bot_id=str(row[2] or ""),
            uid=str(row[3] or ""),
            cookie=str(row[4] or ""),
        )
        for row in rows
    ]


async def check_all_accounts(
    accounts: Iterable[AccountRow],
    concurrency: int = 3,
    on_progress: Optional[Callable[[int, int, AccountRow, Optional[bool]], Awaitable[None]]] = None,
) -> list[tuple[AccountRow, Optional[bool], str]]:
    rows = list(accounts)
    total = len(rows)
    if total == 0:
        return []

    semaphore = asyncio.Semaphore(max(1, min(10, int(concurrency or 3))))
    finished = 0
    lock = asyncio.Lock()

    async def _one(row: AccountRow) -> tuple[AccountRow, Optional[bool], str]:
        nonlocal finished
        async with semaphore:
            ok, message = await validate_cookie(row.uid, row.cookie)
        async with lock:
            finished += 1
            if on_progress is not None:
                try:
                    await on_progress(finished, total, row, ok)
                except Exception:
                    pass
        return row, ok, message

    return list(await asyncio.gather(*[_one(row) for row in rows]))
