"""紧急停止开关（kill switch）：激活后拒绝启动新流程与人工写操作。

存储（双后端，与其余 store 一致）：
    - **demo**：``data/kill_switch.json``（原子写，既有演示形态）；
    - **production**：MySQL ``system_kv`` 表（key=``kill_switch``），跨副本一致。

检查点（**不在 workflow 编排层**检查，避免 Temporal replay 不确定性）：
    - ``alert_webhook``：启动 workflow 前检查——激活时返回 503，拒绝新流程；
    - BFF：集中式鉴权中间件拦截一切 /api 写操作（kill switch 管理端点自身除外）。

安全默认（fail-closed）：状态不可读（DB 故障 / 文件损坏）时抛
:class:`KillSwitchError`，调用方应拒绝动作（宁可拦住，不可放行）。

状态结构::

    {"active": bool, "reason": str, "actor": str, "since": iso|None, "updated_at": iso}
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import mode

DATA_PATH = mode.data_root() / "kill_switch.json"
_KV_KEY = "kill_switch"


class KillSwitchError(RuntimeError):
    """kill switch 状态不可读/写（由调用方转 503，fail-closed）。"""


def _default_state() -> dict:
    return {"active": False, "reason": "", "actor": "", "since": None, "updated_at": None}


def get_state() -> dict:
    """读取当前状态；不可读时抛 :class:`KillSwitchError`（fail-closed）。"""
    if mode.is_production():
        try:
            engine = db_layer.get_engine()
            with engine.connect() as conn:
                raw = conn.execute(
                    select(db_layer.system_kv.c.kv_value).where(
                        db_layer.system_kv.c.kv_key == _KV_KEY
                    )
                ).scalar()
        except Exception as exc:  # noqa: BLE001 - DB 故障一律 fail-closed
            raise KillSwitchError(f"kill switch 状态不可读（数据库）：{exc}") from exc
        if not raw:
            return _default_state()
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KillSwitchError(f"kill switch 状态非法（数据库）：{exc}") from exc
        if not isinstance(loaded, dict):
            raise KillSwitchError("kill switch 状态非法（数据库）：非对象")
        return {**_default_state(), **loaded}

    if not DATA_PATH.is_file():
        return _default_state()
    try:
        loaded = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KillSwitchError(f"kill switch 状态不可读或非法（{DATA_PATH}）：{exc}") from exc
    if not isinstance(loaded, dict):
        raise KillSwitchError(f"kill switch 状态非法（{DATA_PATH}）：非对象")
    return {**_default_state(), **loaded}


def is_active() -> bool:
    return bool(get_state().get("active"))


def set_state(*, active: bool, actor: str, reason: str = "") -> dict:
    """写入状态（激活 / 关闭）；写失败抛 :class:`KillSwitchError`。"""
    moment = db_layer.now_iso()
    state = {
        "active": bool(active),
        "reason": (reason or "").strip(),
        "actor": actor,
        "since": moment if active else None,
        "updated_at": moment,
    }
    if mode.is_production():
        try:
            engine = db_layer.get_engine()
            with engine.begin() as conn:
                conn.execute(
                    db_layer.system_kv.delete().where(db_layer.system_kv.c.kv_key == _KV_KEY)
                )
                conn.execute(
                    db_layer.system_kv.insert().values(
                        kv_key=_KV_KEY,
                        kv_value=json.dumps(state, ensure_ascii=False),
                        updated_at=db_layer.now_dt(),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            raise KillSwitchError(f"kill switch 状态写入失败（数据库）：{exc}") from exc
        return state

    try:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(DATA_PATH) + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DATA_PATH)
    except OSError as exc:
        raise KillSwitchError(f"kill switch 状态写入失败（{DATA_PATH}）：{exc}") from exc
    return state
