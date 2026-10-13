"""只读业务 readiness；不建表、不缓存成功、不执行外部通知或修复。

poller 新鲜度是队列可消费的必要条件，不保证某个 Workflow 已完成。
本机部署的 /readyz 使用这些检查；K8s 仅同步契约，未在集群验收。
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from temporalio.api.enums.v1 import NamespaceState, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, DescribeTaskQueueRequest

from . import config, mode

RPC_TIMEOUT = timedelta(seconds=3)
CHECK_TIMEOUT = 5.0
POLLER_MAX_AGE = 120.0
AGGREGATION_TABLES = frozenset({"alert_domains", "alert_events", "alert_members"})


def status(ok: bool, detail: str, **extra) -> dict:
    return {"ok": ok, "detail": detail, **extra}


def inspect_schema(connection) -> dict:
    """读取 metadata 的表/列契约；绝不调用 get_engine/ensure_schema/create_all。

    C 批次未提供聚合表定义也要失败，避免把旧 schema 标成就绪。
    """
    from .db import metadata

    missing_contract = AGGREGATION_TABLES - set(metadata.tables)
    if missing_contract:
        return status(False, "schema_contract_missing", tables=sorted(missing_contract))
    inspector = inspect(connection)
    actual = set(inspector.get_table_names())
    missing = {}
    for name, table in metadata.tables.items():
        if name not in actual:
            missing[name] = ["<table>"]
            continue
        absent = set(table.columns.keys()) - {col["name"] for col in inspector.get_columns(name)}
        if absent:
            missing[name] = sorted(absent)
    return status(not missing, "schema_current" if not missing else "schema_upgrade_required", missing=missing)


def probe_mysql() -> dict:
    """独立短连接 SELECT 1 + inspect；连接/读写超时，关闭并回滚只读事务。"""
    engine = None
    try:
        url = make_url(mode.db_url())
        if url.drivername != "mysql+pymysql" or not url.host or not url.database:
            raise ValueError("mysql+pymysql required")
        engine = create_engine(url, poolclass=NullPool, connect_args={
            "connect_timeout": 2, "read_timeout": 2, "write_timeout": 2,
        })
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            if connection.execute(text("SELECT 1")).scalar_one() != 1:
                raise ValueError("unexpected SELECT result")
            schema = inspect_schema(connection)
        return {"mysql": status(True, "select_1_ok"), "schema": schema}
    except Exception:
        # 不返回异常字符串：驱动异常可能含 URL、用户、口令或 SQL 数据。
        return {"mysql": status(False, "mysql_probe_failed"), "schema": status(False, "not_verified")}
    finally:
        if engine is not None:
            engine.dispose()


def probe_redis() -> dict:
    from redis import Redis

    url = os.environ.get("AIOPS_REDIS_URL", "")
    if not url:
        return status(mode.is_demo(), "not_required_demo" if mode.is_demo() else "redis_not_configured")
    client = None
    try:
        client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2, retry_on_timeout=False)
        return status(client.ping() is True, "redis_ping")
    except Exception:
        return status(False, "redis_ping_failed")
    finally:
        if client is not None:
            client.close()


async def probe_temporal(gateway, namespace: str, task_queue: str) -> dict:
    started = time.monotonic()
    temporal = status(False, "namespace_rpc_failed", latency_ms=0.0)
    worker = status(False, "namespace_not_verified")
    try:
        client = await asyncio.wait_for(gateway.client(), CHECK_TIMEOUT)
        if client.namespace != namespace:
            return {"temporal": status(False, "client_namespace_mismatch"), "worker": worker}
        # 即使 client 被缓存，每次仍发真实 RPC；ping 对象存在不算健康。
        reply = await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=namespace), timeout=RPC_TIMEOUT,
        )
        if (reply.namespace_info.name != namespace or
                reply.namespace_info.state != NamespaceState.NAMESPACE_STATE_REGISTERED):
            return {"temporal": status(False, "namespace_not_registered"), "worker": worker}
        temporal = status(True, "namespace_rpc_ok", latency_ms=round((time.monotonic() - started) * 1000, 1))
        counts = {}
        for kind, queue_type in (("workflow", TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
                                 ("activity", TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)):
            reply = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(namespace=namespace, task_queue=TaskQueue(name=task_queue),
                                         task_queue_type=queue_type), timeout=RPC_TIMEOUT,
            )
            now = time.time()
            counts[kind] = sum(1 for poller in reply.pollers
                               if poller.identity and poller.HasField("last_access_time")
                               and -5 <= now - poller.last_access_time.ToDatetime(tzinfo=timezone.utc).timestamp() <= POLLER_MAX_AGE)
        worker = status(all(counts.values()), "active_pollers" if all(counts.values()) else "missing_or_stale_pollers",
                        pollers=counts, max_age_seconds=POLLER_MAX_AGE)
    except Exception:
        worker = status(False, "poller_rpc_failed" if temporal["ok"] else "namespace_not_verified")
    return {"temporal": temporal, "worker": worker}


def degradations() -> dict:
    """不发送付费 LLM 请求/通知探针；仅配置不能证明可用，始终如实标记未验收。"""
    configured = {
        "llm": bool(os.environ.get("AIOPS_OLLAMA_URL")),
        "loki": bool(os.environ.get("AIOPS_LOKI_URL")),
        "notifications": any(os.environ.get(key) for key in
                             ("AIOPS_NOTIFY_WEBHOOK", "AIOPS_FEISHU_WEBHOOK", "AIOPS_DINGTALK_WEBHOOK")),
    }
    return {name: status(False, "configured_not_verified" if value else "not_configured_or_local_fallback")
            for name, value in configured.items()}


async def check(gateway) -> dict:
    """每次重新验证配置和依赖；失败返回稳定原因码，详情仅交由认证端点输出。"""
    checks = {}
    summary = {}
    try:
        snapshot = config.startup_snapshot()
        summary = {key: snapshot[key] for key in ("mode", "policy_sha256", "shadow_mode", "namespace", "task_queue")}
        checks["configuration"] = status(True, "valid_shadow" if snapshot["shadow_mode"] else "valid_demo")
    except Exception:
        checks["configuration"] = status(False, "invalid_runtime_configuration")
        for name in ("mysql", "schema", "redis", "temporal", "worker"):
            checks[name] = status(False, "configuration_not_verified")
        return {"ok": False, "checks": checks, "configuration": summary, "degradations": degradations()}

    async def database():
        if snapshot["mode"] == "demo":
            return {name: status(True, "not_required_demo") for name in ("mysql", "schema")}
        try:
            return await asyncio.wait_for(asyncio.to_thread(probe_mysql), CHECK_TIMEOUT)
        except Exception:
            return {name: status(False, "probe_timeout_or_failed") for name in ("mysql", "schema")}

    async def redis():
        try:
            return {"redis": await asyncio.wait_for(asyncio.to_thread(probe_redis), CHECK_TIMEOUT)}
        except Exception:
            return {"redis": status(False, "probe_timeout_or_failed")}

    async def temporal():
        try:
            return await asyncio.wait_for(probe_temporal(gateway, snapshot["namespace"], snapshot["task_queue"]), 12)
        except Exception:
            return {name: status(False, "probe_timeout_or_failed") for name in ("temporal", "worker")}

    for result in await asyncio.gather(database(), redis(), temporal()):
        checks.update(result)
    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks,
            "configuration": summary, "degradations": degradations()}
