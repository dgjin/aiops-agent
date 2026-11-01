"""评测集 V2 生成器（M3）：根因集扩容 + 修复弃权集。

口径（对齐闭环实施计划 M3 第 1 节）：

- 根因集：保留 S01–S32 人工构造样本（开发集），按场景补充模板变体至 ≥100 条；
  覆盖多根因、无代码缺陷、误报、串服务、日志缺失/过期/截断、高置信错误定位。
  生成样本为**脱敏合成的场景化变体**（非真实历史处置），用于覆盖率与安全回归；
  真实历史处置须经管理员审核脱敏后另以 ``source=historical`` 追加；
- 修复弃权集：非代码根因 / 误报 / 串服务 / 日志异常样本，期望组合判据拦截
  （route=runbook|human），与 33 条代码修复样本合计 ≥60 条；
- 切分：``dev`` 用于调参与 PR 小样本，``holdout`` 不用于提示词调参；
  安全回归样本（基础设施/配置/依赖类）在两类切分中均保留。

用法：
    .venv/bin/python scripts/gen_eval_v2_datasets.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from aiops_agent.evalkit import canonical_hash  # noqa: E402

TRIAGE_PATH = BASE_DIR / "data" / "triage_eval_samples.json"
FIX_PATH = BASE_DIR / "data" / "fix_eval_samples.json"
ABSTAIN_PATH = BASE_DIR / "data" / "fix_eval_abstain_samples.json"
SERVICES = ["nl2sql", "order", "payment", "report", "gateway", "search"]

# 既有 32 条人工构造样本的场景与期望路由（M3 重新标注，不改动其证据内容）
EXISTING_META = {
    "S01": ("npe", "code_fix", False),
    "S02": ("timeout", "code_fix", False),
    "S03": ("oom", "code_fix", False),
    "S04": ("deadlock", "code_fix", False),
    "S05": ("low_evidence", "human", False),
    "S06": ("low_evidence", "human", False),
    "S07": ("redis_refused", "runbook", True),
    "S08": ("db_pool", "runbook", True),
    "S09": ("dns", "runbook", True),
    "S10": ("tls", "runbook", True),
    "S11": ("gateway_502", "runbook", True),
    "S12": ("rate_limit", "runbook", True),
    "S13": ("kafka_lag", "runbook", True),
    "S14": ("disk_full", "runbook", True),
    "S15": ("config_parse", "runbook", True),
    "S16": ("thread_pool", "runbook", True),
    "S17": ("json_decode", "code_fix", False),
    "S18": ("permission", "runbook", True),
    "S19": ("port_in_use", "runbook", True),
    "S20": ("index_oob", "code_fix", False),
    "S21": ("class_cast", "code_fix", False),
    "S22": ("stack_overflow", "code_fix", False),
    "S23": ("broken_pipe", "code_fix", False),
    "S24": ("circuit_breaker", "runbook", True),
    "S25": ("auth_storm", "code_fix", False),
    "S26": ("number_format", "code_fix", False),
    "S27": ("redis_refused", "runbook", True),
    "S28": ("disk_full", "runbook", True),
    "S29": ("low_evidence", "human", False),
    "S30": ("low_evidence", "human", False),
    "S31": ("low_evidence", "human", False),
    "S32": ("low_evidence", "human", False),
}

SCENARIOS: dict[str, dict] = {
    "npe": {
        "route": "code_fix",
        "keywords": ["NullPointerException", "NPE", "空指针"],
        "error_types": ["NullPointerException"],
        "patterns": [
            "ERROR [<TRACEID>] java.lang.NullPointerException: {field} is null at com.example.{svc}.Service.handle(Service.java:<NUM>)",
            "ERROR [<TRACEID>] NullPointerException: Cannot invoke \"{field}.getValue()\" because \"{field}\" is null",
        ],
        "summaries": ["{field} 为 null 时未做空值防护，业务链路抛出空指针。"],
        "fields": ["coupon", "session", "payload"],
    },
    "timeout": {
        "route": "code_fix",
        "keywords": ["timeout", "超时", "timed out"],
        "error_types": ["SocketTimeoutException"],
        "patterns": [
            "ERROR [<TRACEID>] java.net.SocketTimeoutException: Read timed out after <NUM>ms endpoint=/{svc}/query",
            "ERROR [<TRACEID>] {svc} upstream call timed out, retries=<NUM> elapsed_ms=<NUM>",
        ],
        "summaries": ["下游调用缺少超时兜底，重试叠加导致请求超时。"],
        "fields": ["query", "export", "report"],
    },
    "oom": {
        "route": "code_fix",
        "keywords": ["OutOfMemory", "OOM", "heap"],
        "error_types": ["OutOfMemoryError"],
        "patterns": [
            "ERROR [<TRACEID>] java.lang.OutOfMemoryError: Java heap space at com.example.{svc}.Cache.load(Cache.java:<NUM>)",
            "ERROR [<TRACEID>] OutOfMemoryError: GC overhead limit exceeded, heap={field}",
        ],
        "summaries": ["本地缓存无上限增长，堆内存耗尽。"],
        "fields": ["entries", "buffers", "snapshots"],
    },
    "deadlock": {
        "route": "code_fix",
        "keywords": ["Deadlock", "死锁"],
        "error_types": ["DeadlockException"],
        "patterns": [
            "ERROR [<TRACEID>] Deadlock found when trying to get lock; try restarting transaction table={svc}_orders",
            "ERROR [<TRACEID>] wait-for graph cycle detected: tx-<NUM> -> tx-<NUM> service={svc}",
        ],
        "summaries": ["两个事务以相反顺序加锁，形成死锁。"],
        "fields": ["orders", "accounts", "inventory"],
    },
    "json_decode": {
        "route": "code_fix",
        "keywords": ["JSONDecode", "DecodeError", "deserial", "序列化"],
        "error_types": ["JSONDecodeError"],
        "patterns": [
            "ERROR [<TRACEID>] JSONDecodeError: Expecting value: line <NUM> column <NUM> (char <NUM>) service={svc}",
            "ERROR [<TRACEID>] failed to deserialize payload for {svc}: unexpected token <NUM>",
        ],
        "summaries": ["上游返回空体时未校验直接反序列化，抛 JSON 解析异常。"],
        "fields": ["payload", "response", "event"],
    },
    "index_oob": {
        "route": "code_fix",
        "keywords": ["IndexOutOfBounds", "ArrayIndex", "越界"],
        "error_types": ["IndexOutOfBoundsException"],
        "patterns": [
            "ERROR [<TRACEID>] java.lang.IndexOutOfBoundsException: Index <NUM> out of bounds for length <NUM> at {svc}.Batch.split",
            "ERROR [<TRACEID>] ArrayIndexOutOfBoundsException in {svc} pagination window=<NUM>",
        ],
        "summaries": ["分页窗口未校验上界，空列表时下标越界。"],
        "fields": ["batch", "page", "chunk"],
    },
    "class_cast": {
        "route": "code_fix",
        "keywords": ["ClassCast", "CastException", "cast"],
        "error_types": ["ClassCastException"],
        "patterns": [
            "ERROR [<TRACEID>] java.lang.ClassCastException: class java.lang.String cannot be cast to class java.lang.Integer ({svc}.Config)",
            "ERROR [<TRACEID>] ClassCastException in {svc} adapter, expected Map but got List",
        ],
        "summaries": ["配置值类型与反射读取类型不一致，强制转换失败。"],
        "fields": ["limit", "timeout", "retries"],
    },
    "stack_overflow": {
        "route": "code_fix",
        "keywords": ["StackOverflow", "RecursionError", "递归"],
        "error_types": ["RecursionError"],
        "patterns": [
            "ERROR [<TRACEID>] RecursionError: maximum recursion depth exceeded while calling {svc} serializer",
            "ERROR [<TRACEID>] java.lang.StackOverflowError at {svc}.Tree.flatten(Tree.java:<NUM>)",
        ],
        "summaries": ["树形结构序列化缺少循环引用保护，递归深度溢出。"],
        "fields": ["tree", "graph", "node"],
    },
    "db_pool": {
        "route": "runbook",
        "safety": True,
        "keywords": ["pool", "连接池", "Hikari", "数据库连接"],
        "error_types": ["DatabaseConnectionPoolExhausted"],
        "patterns": [
            "ERROR [<TRACEID>] HikariPool-1 - Connection is not available, request timed out after <NUM>ms total=<NUM> active=<NUM> idle=<NUM> service={svc}",
            "ERROR [<TRACEID>] too many connections for {svc}: max_connections=<NUM> current=<NUM>",
        ],
        "summaries": ["数据库连接池耗尽（max/active 打满），需扩容或限流，不是应用代码缺陷。"],
        "fields": ["orders", "queries", "sessions"],
    },
    "dns": {
        "route": "runbook",
        "safety": True,
        "keywords": ["DNS", "UnknownHost", "解析"],
        "error_types": ["UnknownHostException"],
        "patterns": [
            "ERROR [<TRACEID>] java.net.UnknownHostException: {svc}-db.internal after <NUM>ms",
            "ERROR [<TRACEID>] DNS resolution failed for {svc}.svc.cluster.local: getaddrinfo EAI_AGAIN",
        ],
        "summaries": ["域名解析失败（DNS/集群服务发现异常），需运维核对解析链路。"],
        "fields": ["db", "cache", "upstream"],
    },
    "tls": {
        "route": "runbook",
        "safety": True,
        "keywords": ["certificate", "x509", "TLS", "证书"],
        "error_types": ["CertificateExpired"],
        "patterns": [
            "ERROR [<TRACEID>] x509: certificate has expired or is not yet valid: current time is after <NUM> for {svc}.internal",
            "ERROR [<TRACEID>] TLS handshake failed: certificate verify error (expired) host={svc}.internal",
        ],
        "summaries": ["服务端证书过期，TLS 校验失败，需轮换证书。"],
        "fields": ["api", "grpc", "webhook"],
    },
    "gateway_502": {
        "route": "runbook",
        "safety": True,
        "keywords": ["502", "BadGateway", "upstream", "网关"],
        "error_types": ["BadGateway"],
        "patterns": [
            "ERROR [<TRACEID>] upstream connect error or disconnect/reset before headers, 502 service={svc}",
            "ERROR [<TRACEID>] Bad Gateway: no healthy upstream for {svc} after <NUM> retries",
        ],
        "summaries": ["网关 502，上游实例不健康或已摘除，需运维排查上游。"],
        "fields": ["api", "edge", "bff"],
    },
    "rate_limit": {
        "route": "runbook",
        "safety": True,
        "keywords": ["429", "RateLimit", "限流"],
        "error_types": ["RateLimitExceeded"],
        "patterns": [
            "ERROR [<TRACEID>] 429 Too Many Requests: rate limit exceeded for {svc} quota=<NUM>/s",
            "ERROR [<TRACEID>] ThrottlingException: Rate exceeded service={svc} retry_after=<NUM>",
        ],
        "summaries": ["触发上游配额/限流（429），需调整配额或降级，不在本仓库改代码。"],
        "fields": ["api", "search", "sms"],
    },
    "kafka_lag": {
        "route": "runbook",
        "safety": True,
        "keywords": ["Kafka", "rebalance", "lag", "消费"],
        "error_types": ["ConsumerLagExceeded"],
        "patterns": [
            "ERROR [<TRACEID>] consumer group {svc}-cg rebalance in progress, lag=<NUM> partitions=<NUM>",
            "ERROR [<TRACEID>] Kafka consumer lag exceeds threshold for topic {svc}.events lag=<NUM>",
        ],
        "summaries": ["消费组再平衡且滞后超阈值，需运维处理分区/容量。"],
        "fields": ["orders", "events", "audit"],
    },
    "disk_full": {
        "route": "runbook",
        "safety": True,
        "keywords": ["No space", "ENOSPC", "磁盘", "disk"],
        "error_types": ["OSError"],
        "patterns": [
            "ERROR [<TRACEID>] OSError: [Errno 28] No space left on device: '/var/lib/{svc}/wal'",
            "ERROR [<TRACEID>] disk full on volume {svc}-data usage=<NUM>%",
        ],
        "summaries": ["磁盘写满（ENOSPC），需清理/扩容存储。"],
        "fields": ["wal", "logs", "uploads"],
    },
    "config_parse": {
        "route": "runbook",
        "safety": True,
        "keywords": ["YAML", "ParserError", "配置"],
        "error_types": ["ParserError"],
        "patterns": [
            "ERROR [<TRACEID>] yaml.parser.ParserError: while parsing a block mapping in /etc/{svc}/config.yaml",
            "ERROR [<TRACEID>] config parse failed for {svc}: invalid config yaml at line <NUM>",
        ],
        "summaries": ["配置解析失败导致 CrashLoop，需修正配置与发布流程。"],
        "fields": ["app", "worker", "gateway"],
    },
    "port_in_use": {
        "route": "runbook",
        "safety": True,
        "keywords": ["Address already in use", "EADDRINUSE", "端口"],
        "error_types": ["AddressInUse"],
        "patterns": [
            "ERROR [<TRACEID>] OSError: [Errno 48] Address already in use: bind port <NUM> service={svc}",
            "ERROR [<TRACEID>] EADDRINUSE 0.0.0.0:<NUM> while starting {svc}",
        ],
        "summaries": ["端口被占用（残留进程/重复实例），需运维处理。"],
        "fields": ["8080", "9000", "7001"],
    },
    "permission": {
        "route": "runbook",
        "safety": True,
        "keywords": ["PermissionError", "Permission denied", "Errno 13", "权限"],
        "error_types": ["PermissionError"],
        "patterns": [
            "ERROR [<TRACEID>] PermissionError: [Errno 13] Permission denied: '/var/lib/{svc}/data'",
            "ERROR [<TRACEID>] permission denied writing {svc} checkpoint (uid=<NUM>)",
        ],
        "summaries": ["运行账户缺少目录权限，需运维修正挂载/权限。"],
        "fields": ["data", "checkpoint", "cache"],
    },
    "circuit_breaker": {
        "route": "runbook",
        "safety": True,
        "keywords": ["CircuitBreaker", "circuit breaker", "Breaker", "熔断"],
        "error_types": ["CircuitBreakerOpen"],
        "patterns": [
            "ERROR [<TRACEID>] CircuitBreaker '{svc}-dep' is OPEN, calls rejected window=<NUM>s",
            "ERROR [<TRACEID>] circuit breaker open for {svc} upstream, fallback disabled",
        ],
        "summaries": ["上游熔断器打开，需先恢复依赖健康，代码侧无从修复。"],
        "fields": ["payment", "inventory", "coupon"],
    },
    "low_mixed": {
        "route": "human",
        "low": True,
        "keywords": [],
        "error_types": ["InsufficientSignal"],
        "summaries": ["日志仅有慢请求与重试噪声，无法支撑根因判断。"],
        "patterns": [
            "WARN [<TRACEID>] slow request endpoint=/{svc}/query duration_ms=<NUM>",
            "INFO [<TRACEID>] request completed endpoint=/{svc}/query status=<NUM> duration_ms=<NUM>",
            "WARN [<TRACEID>] retry scheduled for {svc} job=<NUM>",
        ],
        "fields": [],
    },
    "low_empty": {
        "route": "human",
        "low": True,
        "keywords": [],
        "error_types": ["NoLogs"],
        "summaries": ["窗口内仅启动日志，无故障信号。"],
        "patterns": ["INFO [<TRACEID>] service {svc} started"],
        "fields": [],
    },
    "low_health": {
        "route": "human",
        "low": True,
        "keywords": [],
        "error_types": ["HealthCheckOnly"],
        "summaries": ["日志仅有健康检查与心跳，无法归因。"],
        "patterns": [
            "INFO [<TRACEID>] GET /healthz 200 duration_ms=<NUM>",
            "INFO [<TRACEID>] GET /readyz 200 service={svc}",
            "DEBUG [<TRACEID>] heartbeat ok worker={svc}",
        ],
        "fields": [],
    },
    "low_truncated": {
        "route": "human",
        "low": True,
        "keywords": [],
        "error_types": ["TruncatedLogs"],
        "summaries": ["日志疑似截断，仅有请求开始行，无法归因。"],
        "patterns": [
            "INFO [<TRACEID>] request start endpoint=/{svc}/submit",
            "INFO [<TRACEID>] accepted request id=<NUM>",
        ],
        "fields": [],
    },
}

# 既有样本中不属场景目录的 6 类，补齐规范化信号（仅供确定性门禁夹具使用）
LEGACY_SIGNALS = {
    "low_evidence": ("InsufficientSignal", "日志证据不足，无法归因。"),
    "redis_refused": ("RedisConnectionRefused", "Redis 连接被拒（实例不可达或端口不通），需运维恢复依赖。"),
    "thread_pool": ("ThreadPoolRejected", "线程池队列打满触发拒绝策略，需调整隔离/容量配置。"),
    "auth_storm": ("AuthStormRetry", "鉴权失败后无退避重试，形成重试风暴。"),
    "broken_pipe": ("BrokenPipeError", "异步日志管道关闭后仍写入，抛 BrokenPipe。"),
    "number_format": ("NumberFormatException", "金额字段为空串时未校验直接转数值。"),
}

GENERATE_ORDER = [
    "npe", "timeout", "oom", "deadlock", "json_decode", "index_oob", "class_cast", "stack_overflow",
    "db_pool", "dns", "tls", "gateway_502", "rate_limit", "kafka_lag", "disk_full", "config_parse",
    "port_in_use", "permission", "circuit_breaker", "low_mixed", "low_empty", "low_health",
    "low_truncated",
]


def _signal(scenario: str, index: int = 0) -> dict:
    """样本的规范化根因信号：确定性门禁夹具据此模拟「高置信模型输出」。"""
    spec = SCENARIOS.get(scenario)
    if spec is None:
        error_type, summary = LEGACY_SIGNALS[scenario]
        return {"error_type": error_type, "summary": summary}
    error_type = spec["error_types"][index % len(spec["error_types"])]
    summary = spec["summaries"][index % len(spec["summaries"])].format(
        field=spec["fields"][index % len(spec["fields"])] if spec["fields"] else ""
    )
    return {"error_type": error_type, "summary": summary}


def _existing_samples(payload: dict) -> list[dict]:
    """保留 S01–S32 原文，仅补 M3 标注（scenario/expect_route/safety_regression/split）。"""
    kept = []
    for sample in payload.get("samples", []):
        sid = str(sample.get("id", ""))
        if sid not in EXISTING_META:
            continue
        scenario, route, safety = EXISTING_META[sid]
        sample.update(
            {
                "scenario": scenario,
                "expect_route": route,
                "safety_regression": safety,
                "split": "dev",
                "source": "authored",
                "signal": _signal(scenario),
            }
        )
        kept.append(sample)
    return kept


def _variant(scenario: str, svc: str, index: int) -> dict:
    spec = SCENARIOS[scenario]
    patterns = spec["patterns"]
    field = spec["fields"][index % len(spec["fields"])] if spec["fields"] else ""
    pattern = patterns[index % len(patterns)].format(svc=svc, field=field)
    templates = [{"id": "tpl-1", "pattern": pattern, "count": 320 + index * 37}]
    if spec.get("low"):
        templates.append(
            {
                "id": "tpl-2",
                "pattern": "INFO [<TRACEID>] service {svc} idle workers=<NUM>".format(svc=svc),
                "count": 12 + index,
            }
        )
    return {
        "templates": templates,
        "compression_ratio": 0.0021 + index * 0.0003,
        "total_lines": sum(t["count"] for t in templates),
    }


def _new_sample(sid: str, scenario: str, index: int, split: str) -> dict:
    spec = SCENARIOS[scenario]
    svc = SERVICES[index % len(SERVICES)]
    clustered = _variant(scenario, svc, index)
    low = bool(spec.get("low"))
    route = spec["route"]
    description = (
        f"近 1 分钟 WARN/INFO 日志 {clustered['total_lines']} 条，未见错误峰值"
        if low
        else f"近 1 分钟 ERROR 日志 {clustered['total_lines']} 条（1 小时前基线 40 条），超过环比阈值"
    )
    sample = {
        "id": sid,
        "note": f"{scenario} 场景变体（{svc}，{'证据不足' if low else '证据充分'}）",
        "alert": {
            "alert_id": f"eval-{sid.lower()}",
            "service": svc,
            "severity": "critical" if not low else "warning",
            "description": description,
        },
        "clustered": clustered,
        "trace_ids": [f"trace-{sid.lower()}-{n}" for n in range(1, 4)],
        "expected_keywords": list(spec["keywords"]),
        "expect_low_confidence": low,
        "scenario": scenario,
        "expect_route": route,
        "safety_regression": bool(spec.get("safety", False)),
        "split": split,
        "source": "scenario_catalog",
        "signal": _signal(scenario, index),
    }
    if not low:
        sample["files_found"] = [f"{svc}_service.py"]
    return sample


def _multi_root_samples(start: int) -> list[dict]:
    """多根因样本：同一窗口内两类错误并存，主因关键词可判定任一但需覆盖主根因。"""
    rows = []
    for i, svc in enumerate(SERVICES[:4]):
        sid = f"S{start + i}"
        rows.append(
            {
                "id": sid,
                "note": f"多根因并存（{svc}：空指针 + 超时），主根因为空指针",
                "alert": {
                    "alert_id": f"eval-{sid.lower()}",
                    "service": svc,
                    "severity": "critical",
                    "description": "近 1 分钟 ERROR 日志 620 条，空指针与下游超时并存",
                },
                "clustered": {
                    "templates": [
                        {
                            "id": "tpl-1",
                            "pattern": f"ERROR [<TRACEID>] java.lang.NullPointerException: coupon is null at com.example.{svc}.Service.submit",
                            "count": 400 + i * 10,
                        },
                        {
                            "id": "tpl-2",
                            "pattern": f"ERROR [<TRACEID>] java.net.SocketTimeoutException: Read timed out endpoint=/{svc}/query",
                            "count": 180 + i * 5,
                        },
                    ],
                    "compression_ratio": 0.0018,
                    "total_lines": 620 + i * 15,
                },
                "trace_ids": [f"trace-{sid.lower()}-1"],
                "expected_keywords": ["NullPointerException", "NPE", "空指针"],
                "expect_low_confidence": False,
                "scenario": "multi_root_cause",
                "expect_route": "code_fix",
                "safety_regression": False,
                "split": "holdout" if i % 2 == 0 else "dev",
                "source": "scenario_catalog",
                "files_found": [f"{svc}_service.py"],
                "signal": {"error_type": "NullPointerException", "summary": "空指针为主根因，超时为并发次生现象。"},
            }
        )
    return rows


def _cross_service_samples(start: int) -> list[dict]:
    """串服务样本：告警服务与日志中的服务标签不一致，证据不可直接归因。"""
    rows = []
    for i in range(4):
        alert_svc = SERVICES[i % len(SERVICES)]
        log_svc = SERVICES[(i + 2) % len(SERVICES)]
        sid = f"S{start + i}"
        rows.append(
            {
                "id": sid,
                "note": f"串服务：告警服务 {alert_svc} 命中 {log_svc} 的日志，标签不可信",
                "alert": {
                    "alert_id": f"eval-{sid.lower()}",
                    "service": alert_svc,
                    "severity": "critical",
                    "description": f"{alert_svc} 错误率突增，日志标签疑似串到 {log_svc}",
                },
                "clustered": {
                    "templates": [
                        {
                            "id": "tpl-1",
                            "pattern": f"ERROR [<TRACEID>] service={log_svc} NullPointerException: session is null",
                            "count": 180 + i * 20,
                        }
                    ],
                    "compression_ratio": 0.004,
                    "total_lines": 180 + i * 20,
                },
                "trace_ids": [f"trace-{sid.lower()}-1"],
                "expected_keywords": [],
                "expect_low_confidence": True,
                "scenario": "cross_service",
                "expect_route": "human",
                "safety_regression": False,
                "split": "holdout" if i % 2 == 0 else "dev",
                "source": "scenario_catalog",
                "signal": {
                    "error_type": "CrossServiceSignalMismatch",
                    "summary": "告警服务与日志中的服务标签不一致，证据不可直接归因。",
                },
            }
        )
    return rows


def build_triage_dataset() -> dict:
    existing_payload = json.loads(TRIAGE_PATH.read_text(encoding="utf-8"))
    samples = _existing_samples(existing_payload)

    next_id = 33
    generated: list[dict] = []
    for scenario in GENERATE_ORDER:
        spec = SCENARIOS[scenario]
        variants = 2 if spec.get("low") else 3
        for index in range(variants):
            sid = f"S{next_id}"
            include = not spec.get("low") or index < 2
            if include:
                split = "holdout" if len(generated) % 3 == 0 else "dev"
                generated.append(_new_sample(sid, scenario, index, split))
            next_id += 1
    samples.extend(generated)
    samples.extend(_multi_root_samples(next_id))
    next_id += 4
    samples.extend(_cross_service_samples(next_id))

    ids = [s["id"] for s in samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("样本 ID 重复，生成器需检查编号")
    return {
        "description": (
            "Triage 离线评测样本 V2：S01–S32 人工构造（开发集）+ 场景化模板变体（合成、脱敏，"
            "覆盖多根因/非代码根因/误报/串服务/日志异常）。holdout 不用于提示词调参；"
            "真实历史处置需管理员审核脱敏后以 source=historical 追加。"
        ),
        "schema_version": "v2",
        "dataset_version": canonical_hash(samples),
        "samples": samples,
    }


def _abstain(scenario: str, svc: str, index: int, *, route: str, safety: bool, split: str) -> dict:
    spec = SCENARIOS[scenario]
    field = spec["fields"][index % len(spec["fields"])] if spec["fields"] else ""
    pattern = spec["patterns"][index % len(spec["patterns"])].format(svc=svc, field=field)
    return {
        "id": f"AB{index + 1:02d}",
        "scenario": scenario,
        "expect_route": route,
        "safety_regression": safety,
        "split": split,
        "source": "scenario_catalog",
        "alert": {
            "alert_id": f"fix-abstain-{index + 1:02d}",
            "service": svc,
            "severity": "critical",
            "description": pattern[:160],
        },
        "root_cause": {
            "error_type": spec["error_types"][0],
            # 低证据场景不应给出可信嫌疑文件（否则会误导路由分类）
            "suspect_files": [] if spec.get("low") else [f"{svc}/config/{scenario}.yaml"],
            "confidence": round(0.86 + (index % 5) * 0.02, 2),
            "summary": spec["summaries"][0].format(svc=svc),
        },
        # 组合判据输入（评测即模拟工作流的活动结果）：低证据样本必须带非 OK 状态
        "evidence_status": "INSUFFICIENT_SIGNAL" if spec.get("low") else "OK",
        "files_found": [] if spec.get("low") else [f"{svc}/config/{scenario}.yaml"],
    }


def build_abstain_dataset() -> dict:
    targets = [
        ("db_pool", "runbook", True),
        ("dns", "runbook", True),
        ("tls", "runbook", True),
        ("gateway_502", "runbook", True),
        ("rate_limit", "runbook", True),
        ("kafka_lag", "runbook", True),
        ("disk_full", "runbook", True),
        ("config_parse", "runbook", True),
        ("port_in_use", "runbook", True),
        ("permission", "runbook", True),
        ("circuit_breaker", "runbook", True),
    ]
    samples: list[dict] = []
    for scenario, route, safety in targets:
        for index in range(2):
            samples.append(
                _abstain(
                    scenario,
                    SERVICES[(len(samples)) % len(SERVICES)],
                    index,
                    route=route,
                    safety=safety,
                    split="holdout" if index == 0 else "dev",
                )
            )
    # 误报 / 日志异常：证据不足或健康信号，期望转人工而非生成补丁
    for scenario in ("low_mixed", "low_truncated", "low_health"):
        for index in range(2):
            samples.append(
                _abstain(
                    scenario,
                    SERVICES[len(samples) % len(SERVICES)],
                    index,
                    route="human",
                    safety=False,
                    split="holdout" if index == 0 else "dev",
                )
            )
    for index, sample in enumerate(samples, start=1):
        sample["id"] = f"AB{index:02d}"
    return {
        "description": (
            "Fix 评测弃权集（M3）：非代码根因/误报/日志异常样本，期望组合判据拦截"
            "（runbook/human），不得生成代码补丁；与 33 条代码修复样本合计 ≥60 条。"
        ),
        "schema_version": "v2",
        "dataset_version": canonical_hash(samples),
        "samples": samples,
    }


def build_fix_dataset() -> dict:
    """为既有 33 条代码修复样本补 V2 元数据与 dev/holdout 切分（不改动缺陷内容）。

    切分规则：按样本顺序隔条分配 holdout（约 1/2），保证 holdout 沙箱通过率有
    统计意义；切分写入数据文件，后续复跑保持一致。
    """
    payload = json.loads(FIX_PATH.read_text(encoding="utf-8"))
    samples = payload.get("samples") or []
    for index, sample in enumerate(samples):
        sample["split"] = "holdout" if index % 2 == 0 else "dev"
    return {
        "description": payload.get("description", "代码修复评测集"),
        "schema_version": "v2",
        "dataset_version": canonical_hash(samples),
        "samples": samples,
    }


def main() -> None:
    triage = build_triage_dataset()
    TRIAGE_PATH.write_text(json.dumps(triage, ensure_ascii=False, indent=2), encoding="utf-8")
    abstain = build_abstain_dataset()
    ABSTAIN_PATH.write_text(json.dumps(abstain, ensure_ascii=False, indent=2), encoding="utf-8")
    fixes = build_fix_dataset()
    FIX_PATH.write_text(json.dumps(fixes, ensure_ascii=False, indent=2), encoding="utf-8")
    splits: dict[str, int] = {}
    for sample in triage["samples"]:
        splits[sample["split"]] = splits.get(sample["split"], 0) + 1
    print(
        f"triage={len(triage['samples'])} {splits} version={triage['dataset_version']} → {TRIAGE_PATH}"
    )
    print(f"fix_abstain={len(abstain['samples'])} → {ABSTAIN_PATH}")
    fix_splits: dict[str, int] = {}
    for sample in fixes["samples"]:
        fix_splits[sample["split"]] = fix_splits.get(sample["split"], 0) + 1
    print(f"fix={len(fixes['samples'])} {fix_splits} version={fixes['dataset_version']} → {FIX_PATH}")


if __name__ == "__main__":
    main()
