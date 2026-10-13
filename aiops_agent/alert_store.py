"""告警事件聚合存储（C 批次）：跨进程持久化、去重、租约认领与补偿重试。

设计要点（对齐《单机生产化整改实施计划》批次 C）：

- 故障域键 ``{mode}:{namespace}:{service}``：运行档与 Temporal namespace 都进键，
  demo 与 production（或不同 namespace）的告警绝不互相归并；
- 窗口自事件首条告警起算（固定窗口，非滑动）：窗口内同域后续告警只记成员行；
  **当前事件仍未启动（pending）时窗口视为继续**——否则启动失败的事件会被新事件
  顶掉，同域开出第二条代表流程；
- 成员唯一约束 ``(domain_key, alert_id)``：重复投递（Alertmanager 重试 / 多实例
  并发）不重复计数；一批全为历史周期重投时不再新开事件；
- 认领 = 条件 UPDATE + 租约（``status='pending' AND 租约空闲/过期``）：原子完成，
  进程崩溃后租约到期可由任意实例接管，但代表 Workflow id 不变，
  重复启动由 REJECT_DUPLICATE + FAIL 幂等兜底；
- **Temporal 调用一律在事务外**（调用方职责）：本模块全部函数只操作 DB，
  事务短小、不长持行锁；启动结果用 mark_started / mark_failed 回报；
- 时间统一取应用层 UTC naive（``db.now_dt``）：单机部署无跨机时钟问题；不取
  ``CURRENT_TIMESTAMP`` 是因为 MySQL 会话时区未强制 UTC，混用两个时钟反而引入偏差。
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from aiops_agent import db, mode
from aiops_agent.models import Alert

LEASE_SECONDS = 60.0  # 认领租约：接管等待上限（webhook 重投窗口内）
RETRY_BASE_SECONDS = 15.0  # 补偿退避基数（按 attempts 指数增长）
RETRY_MAX_SECONDS = 300.0
ERROR_SUMMARY_LIMIT = 2000


def domain_key_for(service: str) -> str:
    """故障域键：档位 + namespace + service（跨档/跨 namespace 不归并）。"""
    return f"{mode.mode()}:{mode.temporal_namespace()}:{service}"


def workflow_id_for_alert(alert: Alert) -> str:
    """与 alert_webhook.workflow_id_for / demo_cli._wf_id 同构的代表流程幂等键。"""
    return f"aiops-fix-{alert.service}-{alert.alert_id}"


@dataclass
class EventView:
    """事件视图：ingest / 补偿扫描的返回值，供响应组装与启动编排。"""

    event_id: str
    domain_key: str
    service: str
    wf_id: str
    representative_alert: dict
    policy_snapshot: dict
    status: str = "pending"
    claim_token: str = ""
    count: int = 0
    alert_ids: list[str] = field(default_factory=list)
    is_new: bool = False

    def as_aggregated(self) -> dict:
        """响应 aggregated 条目（event_key 等字段名保持历史契约）。"""
        return {
            "event_key": self.event_id,
            "service": self.service,
            "count": self.count,
            "wf_id": self.wf_id,
            "alert_ids": self.alert_ids[:8],
            "status": self.status,
        }


@dataclass
class IngestOutcome:
    """单批 ingest 结果。"""

    events: list[EventView] = field(default_factory=list)  # 本批触达（新开或归并）
    to_start: list[EventView] = field(default_factory=list)  # 本进程认领，负责启动
    busy: list[EventView] = field(default_factory=list)  # 他人租约中，需重投确认


def _alert_payload(alert: Alert) -> dict:
    return {
        "alert_id": alert.alert_id,
        "service": alert.service,
        "severity": alert.severity,
        "description": alert.description,
    }


def _claim_statement(event_id: str, token: str, now: datetime):
    """条件认领（原子）：仅 pending 且租约空闲/过期可认领；attempts 认领即 +1。"""
    return (
        update(db.alert_events)
        .where(
            db.alert_events.c.event_id == event_id,
            db.alert_events.c.status == "pending",
            or_(
                db.alert_events.c.lease_expires_at.is_(None),
                db.alert_events.c.lease_expires_at < now,
            ),
        )
        .values(
            claim_token=token,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            attempts=db.alert_events.c.attempts + 1,
            updated_at=now,
        )
    )


def _insert_member(conn, event_id: str, domain_key: str, alert: Alert, now: datetime) -> bool:
    """成员去重写入：唯一键 ``(domain_key, alert_id)`` 冲突返回 False。"""
    try:
        with conn.begin_nested():
            conn.execute(
                db.alert_members.insert().values(
                    event_id=event_id,
                    domain_key=domain_key,
                    alert_id=alert.alert_id,
                    service=alert.service,
                    received_at=now,
                )
            )
        return True
    except IntegrityError:
        return False


def _member_summary(conn, event_id: str) -> tuple[int, list[str]]:
    count = conn.execute(
        select(func.count())
        .select_from(db.alert_members)
        .where(db.alert_members.c.event_id == event_id)
    ).scalar_one()
    alert_ids = [
        row[0]
        for row in conn.execute(
            select(db.alert_members.c.alert_id)
            .where(db.alert_members.c.event_id == event_id)
            .order_by(db.alert_members.c.id)
            .limit(8)
        )
    ]
    return int(count), alert_ids


def _view_from_event(conn, row, claim_token: str = "", is_new: bool = False) -> EventView:
    count, alert_ids = _member_summary(conn, row["event_id"])
    return EventView(
        event_id=row["event_id"],
        domain_key=row["domain_key"],
        service=row["service"],
        wf_id=row["representative_wf_id"],
        representative_alert=dict(row["representative_alert"] or {}),
        policy_snapshot=dict(row["policy_snapshot"] or {}),
        status=row["status"],
        claim_token=claim_token,
        count=count,
        alert_ids=alert_ids,
        is_new=is_new,
    )


def _ensure_domain(conn, domain_key: str, service: str, now: datetime):
    """取（必要时创建）故障域行并锁定。

    先无锁探测——对不存在的键直接 ``FOR UPDATE`` 会取 gap 锁，两个实例同时首建
    时互相等待对方的插入意向锁而死锁；探测缺失后走 INSERT（savepoint 兜并发），
    随后的锁定读在 MySQL 下穿透 REPEATABLE READ 快照，必然能看到对端已提交的行。
    """
    probe = conn.execute(
        select(db.alert_domains.c.domain_key).where(db.alert_domains.c.domain_key == domain_key)
    ).first()
    if probe is None:
        try:
            with conn.begin_nested():
                conn.execute(
                    db.alert_domains.insert().values(
                        domain_key=domain_key,
                        service=service,
                        runtime_mode=mode.mode(),
                        temporal_namespace=mode.temporal_namespace(),
                        current_event_id=None,
                        window_started_at=None,
                        updated_at=now,
                    )
                )
        except IntegrityError:
            pass  # 并发对端已创建
    return (
        conn.execute(
            select(db.alert_domains)
            .where(db.alert_domains.c.domain_key == domain_key)
            .with_for_update()
        )
        .mappings()
        .first()
    )


def _claim_or_busy(conn, event, outcome: IngestOutcome, now: datetime) -> None:
    """当前窗口事件：已启动仅归并展示；pending 原子认领；租约被占则标记 busy。"""
    token = secrets.token_hex(16)
    claimed = conn.execute(_claim_statement(event["event_id"], token, now)).rowcount == 1
    view = _view_from_event(conn, event, claim_token=token if claimed else "")
    outcome.events.append(view)
    if event["status"] != "pending":
        return  # 已启动：重复投递仅归并
    if claimed:
        outcome.to_start.append(view)
    else:
        outcome.busy.append(view)  # 他人有效租约中：调用方应让 Alertmanager 重投


def ingest(
    engine: Engine,
    alerts: list[Alert],
    snapshot: dict,
    window_seconds: float,
    now: datetime | None = None,
) -> IngestOutcome:
    """单批告警入库（单事务完成域/窗口裁决 + 成员去重 + 认领）。

    调用方必须在**事务提交后**再启动 ``to_start`` 的代表流程，
    然后用 :func:`mark_started` / :func:`mark_failed` 回报结果。
    """
    now = now or db.now_dt()
    outcome = IngestOutcome()
    ordered: dict[str, list[Alert]] = {}
    for alert in alerts:
        ordered.setdefault(alert.service, []).append(alert)
    window = timedelta(seconds=max(0.0, window_seconds))
    with engine.begin() as conn:
        for service, batch in ordered.items():
            domain_key = domain_key_for(service)
            domain = _ensure_domain(conn, domain_key, service, now)
            event = None
            if domain["current_event_id"]:
                event = (
                    conn.execute(
                        select(db.alert_events).where(
                            db.alert_events.c.event_id == domain["current_event_id"]
                        )
                    )
                    .mappings()
                    .first()
                )
            window_live = event is not None and (
                (
                    domain["window_started_at"] is not None
                    and now - domain["window_started_at"] < window
                )
                or event["status"] == "pending"  # 未启动事件继续归并/接管，防开第二代表流程
            )
            if window_live:
                for alert in batch:
                    _insert_member(conn, event["event_id"], domain_key, alert, now)
                _claim_or_busy(conn, event, outcome, now)
                continue
            # 开新事件：成员先按新事件去重写入；全部为历史重投则不开（不给旧周期开第二条流程）
            event_id = f"evt-{service}-{uuid.uuid4().hex[:8]}"
            inserted = [a for a in batch if _insert_member(conn, event_id, domain_key, a, now)]
            if not inserted:
                continue
            representative = inserted[0]
            token = secrets.token_hex(16)
            conn.execute(
                db.alert_events.insert().values(
                    event_id=event_id,
                    domain_key=domain_key,
                    service=service,
                    representative_alert=_alert_payload(representative),
                    representative_wf_id=workflow_id_for_alert(representative),
                    policy_snapshot=snapshot,
                    first_received_at=now,
                    status="pending",
                    claim_token=token,
                    lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                    retry_at=None,
                    attempts=1,
                    error_summary=None,
                    started_at=None,
                    updated_at=now,
                )
            )
            conn.execute(
                update(db.alert_domains)
                .where(db.alert_domains.c.domain_key == domain_key)
                .values(current_event_id=event_id, window_started_at=now, updated_at=now)
            )
            row = (
                conn.execute(select(db.alert_events).where(db.alert_events.c.event_id == event_id))
                .mappings()
                .one()
            )
            view = _view_from_event(conn, row, claim_token=token, is_new=True)
            outcome.events.append(view)
            outcome.to_start.append(view)
    return outcome


def mark_started(
    engine: Engine, event_id: str, claim_token: str, now: datetime | None = None
) -> bool:
    """认领者启动成功（或幂等冲突=已启动）后落库；token 不匹配返回 False（已被接管）。"""
    now = now or db.now_dt()
    with engine.begin() as conn:
        result = conn.execute(
            update(db.alert_events)
            .where(
                db.alert_events.c.event_id == event_id,
                db.alert_events.c.claim_token == claim_token,
            )
            .values(
                status="started",
                started_at=now,
                claim_token="",
                lease_expires_at=None,
                retry_at=None,
                error_summary=None,
                updated_at=now,
            )
        )
    return result.rowcount == 1


def mark_failed(
    engine: Engine,
    event_id: str,
    claim_token: str,
    error_summary: str,
    now: datetime | None = None,
) -> bool:
    """启动失败：释放租约 + 退避重试时间 + 错误摘要；保持 pending 等待重投/补偿。"""
    now = now or db.now_dt()
    with engine.begin() as conn:
        attempts = conn.execute(
            select(db.alert_events.c.attempts).where(db.alert_events.c.event_id == event_id)
        ).scalar_one_or_none()
        if attempts is None:
            return False
        retry_at = now + timedelta(
            seconds=min(RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)), RETRY_MAX_SECONDS)
        )
        result = conn.execute(
            update(db.alert_events)
            .where(
                db.alert_events.c.event_id == event_id,
                db.alert_events.c.claim_token == claim_token,
            )
            .values(
                claim_token="",
                lease_expires_at=None,
                retry_at=retry_at,
                error_summary=str(error_summary)[:ERROR_SUMMARY_LIMIT],
                updated_at=now,
            )
        )
    return result.rowcount == 1


def due_events(engine: Engine, now: datetime | None = None, limit: int = 20) -> list[EventView]:
    """补偿扫描：pending 且租约空闲/过期、且已到重试时间的到期事件（按收到时间升序）。"""
    now = now or db.now_dt()
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(db.alert_events)
                .where(
                    db.alert_events.c.status == "pending",
                    or_(
                        db.alert_events.c.lease_expires_at.is_(None),
                        db.alert_events.c.lease_expires_at < now,
                    ),
                    or_(db.alert_events.c.retry_at.is_(None), db.alert_events.c.retry_at <= now),
                )
                .order_by(db.alert_events.c.first_received_at)
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return [_view_from_event(conn, row) for row in rows]


def claim_event(engine: Engine, event_id: str, now: datetime | None = None) -> str | None:
    """补偿认领（原子条件 UPDATE）；成功返回认领 token，失败（他人持有）返回 None。"""
    now = now or db.now_dt()
    token = secrets.token_hex(16)
    with engine.begin() as conn:
        result = conn.execute(_claim_statement(event_id, token, now))
    return token if result.rowcount == 1 else None
