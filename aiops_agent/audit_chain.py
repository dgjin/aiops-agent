"""审计哈希链与本地归档（M4）：tamper-evident 追加、完整性校验、归档后清理。

设计边界（明确不夸大）：
- 这是**本机可控链头**的哈希链：每条审计记录携带 ``event_id/prev_hash/entry_hash``，
  篡改、插入或删除链中任一条都能被 :func:`verify_demo` / :func:`verify_db` 发现；
- **不等同外部 WORM**：拥有本机文件/数据库写权限的人可以整体重算哈希链。真实不可变
  存储（对象锁 / WORM 桶 / 第三方存证）留待环境接入，本模块只保证「本机能发现篡改」。

写入串行化：
- demo：``.audit-chain.lock`` 文件锁内「读链尾 → 追加 → 原子写链头」；
- production：同一事务内 ``SELECT ... FOR UPDATE`` 锁 ``audit_chain_heads`` 单行链头。

归档与清理：
- 只有整条链校验通过才允许归档；
- 删除（purge）只针对「已有归档清单记录、归档文件 SHA-256 复核通过」的过期审计，
  未归档记录不会被 TTL 静默删除；归档文件本身保留，供离线复核。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from . import db as db_layer

GENESIS = ""
SCOPE = "audit"
HEAD_NAME = ".chain-head.json"
LOCK_NAME = ".audit-chain.lock"
ARCHIVE_DIR = "archive"
MANIFEST_NAME = "manifest.json"

_AUDIT_FILE_RE = re.compile(r"^audit-(\d{8})\.jsonl$")
_thread_lock = threading.Lock()


class AuditChainError(RuntimeError):
    """审计链不可追加/不可读取（fail-closed，不允许分叉或静默丢条目）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(record: dict) -> str:
    """条目规范化：参与哈希的字段固定，避免新增展示字段导致历史校验漂移。"""
    payload = {
        "ts": record.get("ts") or "",
        "actor": record.get("actor") or "",
        "action": record.get("action") or "",
        "wf_id": record.get("wf_id") or "",
        "target": record.get("target"),
        "params": record.get("params") or {},
        "result": record.get("result") or "",
        "command_id": record.get("command_id") or "",
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def entry_hash(prev_hash: str, record: dict) -> str:
    """entry_hash = sha256(prev_hash + "\\n" + canonical(record))。"""
    body = canonical(record)
    return hashlib.sha256(f"{prev_hash or ''}\n{body}".encode("utf-8")).hexdigest()


def make_entry(record: dict, prev_hash: str) -> dict:
    """在待写入记录上补齐链字段（不修改入参）。"""
    entry = dict(record)
    entry["event_id"] = str(entry.get("event_id") or uuid.uuid4().hex)
    entry["prev_hash"] = prev_hash or ""
    entry["entry_hash"] = entry_hash(entry["prev_hash"], entry)
    return entry


def verify_records(records: list[dict], *, start_hash: str | None = None) -> dict:
    """按写入顺序校验记录链。

    ``start_hash`` 用于**分段校验**（归档一段行时，用前一条已校验记录的 entry_hash
    作为起点）；不传则从创世空串开始。历史未上链记录只允许出现在首条链记录之前，
    链开始后再出现无 ``entry_hash`` 的记录视为断链（防止删字段绕过校验）。
    """
    running = GENESIS if start_hash is None else start_hash
    chain_started = start_hash is not None
    checked = 0
    legacy = 0
    for record in records:
        stored_hash = record.get("entry_hash")
        if not stored_hash:
            if chain_started:
                return {
                    "ok": False,
                    "checked": checked,
                    "legacy": legacy,
                    "chain_started": True,
                    "head": running,
                    "broken_at": _point(record),
                    "error": "链中出现缺少 entry_hash 的记录（疑似被删改）",
                }
            legacy += 1
            continue
        chain_started = True
        prev_hash = record.get("prev_hash") or ""
        if prev_hash != running:
            return {
                "ok": False,
                "checked": checked,
                "legacy": legacy,
                "chain_started": True,
                "head": running,
                "broken_at": _point(record),
                "error": "prev_hash 与上一条记录不连续（疑似删除、插入或重排）",
            }
        expected = entry_hash(prev_hash, record)
        if expected != stored_hash:
            return {
                "ok": False,
                "checked": checked,
                "legacy": legacy,
                "chain_started": True,
                "head": running,
                "broken_at": _point(record),
                "error": "entry_hash 校验失败（记录内容已被修改）",
            }
        running = stored_hash
        checked += 1
    return {
        "ok": True,
        "checked": checked,
        "legacy": legacy,
        "chain_started": chain_started,
        "head": running,
        "broken_at": None,
        "error": "",
    }


def _point(record: dict) -> dict:
    return {
        "event_id": record.get("event_id") or "",
        "ts": record.get("ts") or "",
        "source": record.get("source") or "",
    }


# ----------------------------------------------------------------------
# demo：JSONL 文件链
# ----------------------------------------------------------------------


def audit_files(audit_dir: Path) -> list[Path]:
    """按日期升序返回 ``audit-YYYYMMDD.jsonl``（归档子目录不参与）。"""
    directory = Path(audit_dir)
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and _AUDIT_FILE_RE.match(path.name)),
        key=lambda path: path.name,
    )


def _parse_lines(path: Path) -> tuple[list[dict], int]:
    """读取一个 JSONL 文件的记录；坏行计数返回（坏行不参与链，但会导致校验缺口）。"""
    rows: list[dict] = []
    bad = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditChainError(f"审计文件不可读：{path.name}（{exc}）") from exc
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(value, dict):
            value["source"] = path.name
            rows.append(value)
    return rows, bad


def read_demo_records(audit_dir: Path, *, ascending: bool = True) -> list[dict]:
    records: list[dict] = []
    for path in audit_files(audit_dir):
        rows, _ = _parse_lines(path)
        records.extend(rows)
    return records if ascending else list(reversed(records))


def _verify_failure(message: str, *, source: str = "") -> dict:
    return {
        "ok": False,
        "checked": 0,
        "legacy": 0,
        "chain_started": False,
        "head": GENESIS,
        "broken_at": {"source": source} if source else None,
        "error": message,
    }


def _archived_entries(audit_dir: Path) -> list[dict]:
    """归档清单条目（按 day 升序）；清单损坏时抛 ``AuditChainError``。"""
    return sorted(
        (
            entry
            for entry in load_manifest(audit_dir).get("entries") or []
            if isinstance(entry, dict) and entry.get("day")
        ),
        key=lambda entry: str(entry.get("day") or ""),
    )


def verify_demo(audit_dir: Path) -> dict:
    """校验 demo 全链（归档副本 + 活动文件，按 day 升序）。坏行视为断链证据。

    purge 只删除活动文件、归档副本长期保留；因此校验必须把归档段当作链前缀
    一起校验，否则清理后剩余记录会因 ``prev_hash`` 指向已归档前缀而误报断链。
    未 purge 的归档条目直接校验活动文件，避免同一批记录重复参与链。
    """
    directory = Path(audit_dir)
    try:
        entries = _archived_entries(directory)
    except AuditChainError as exc:
        return _verify_failure(f"归档清单不可读：{exc}")
    rows: list[dict] = []
    for entry in entries:
        name = Path(str(entry.get("path") or "")).name
        live = directory / name
        archive = Path(str(entry.get("path") or ""))
        status = str(entry.get("status") or "")
        if status == "archived":
            if not live.is_file():
                return _verify_failure(
                    f"{name} 已归档但尚未 purge，活动文件缺失（疑似删除）", source=name
                )
            continue  # 活动文件本身参与校验，避免重复
        if status != "purged" and live.is_file():
            continue
        if not archive.is_file():
            return _verify_failure(f"{name} 归档副本缺失，无法校验历史链", source=name)
        data = archive.read_bytes()
        if _sha256(data) != str(entry.get("sha256") or ""):
            return _verify_failure(f"{name} 归档副本 SHA-256 不匹配（疑似篡改）", source=name)
        parsed, bad = _parse_lines(archive)
        if bad:
            return _verify_failure(f"{name} 含 {bad} 行无法解析（疑似被截断或改写）", source=name)
        rows.extend(parsed)
    for path in audit_files(directory):
        parsed, bad = _parse_lines(path)
        if bad:
            return _verify_failure(
                f"{path.name} 含 {bad} 行无法解析（疑似被截断或改写）", source=path.name
            )
        rows.extend(parsed)
    return verify_records(rows)


def _tail_record(path: Path) -> dict | None:
    """读取单个 JSONL 的最后一条非空记录；截断/损坏时 fail-closed。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditChainError(f"审计文件不可读：{path.name}（{exc}）") from exc
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditChainError(f"审计链尾损坏（{path.name}），拒绝追加") from exc
        if not isinstance(value, dict):
            raise AuditChainError(f"审计链尾格式非法（{path.name}），拒绝追加")
        return {
            "last_event_id": str(value.get("event_id") or ""),
            "last_hash": str(value.get("entry_hash") or ""),
            "source": path.name,
        }
    return None


def chain_tail(audit_dir: Path) -> dict:
    """读取链尾（最后一个非空记录）；活动文件为空时回退到归档副本。

    purge 删光活动文件后不能从创世重启新链，否则新记录与归档前缀断链；
    归档副本损坏或清单不可读时 fail-closed。
    """
    directory = Path(audit_dir)
    for path in reversed(audit_files(directory)):
        tail = _tail_record(path)
        if tail is not None:
            return tail
    try:
        entries = _archived_entries(directory)
    except AuditChainError as exc:
        raise AuditChainError(f"归档清单不可读，拒绝追加：{exc}") from exc
    for entry in reversed(entries):
        path = Path(str(entry.get("path") or ""))
        if not path.is_file():
            raise AuditChainError(f"归档副本缺失（{path.name or 'unknown'}），拒绝追加")
        tail = _tail_record(path)
        if tail is not None:
            return tail
    return {"last_event_id": "", "last_hash": GENESIS, "source": ""}


@contextmanager
def file_lock(audit_dir: Path):
    """跨线程/跨进程串行追加（demo 单主机）。"""
    directory = Path(audit_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOCK_NAME
    with _thread_lock, open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _write_head(audit_dir: Path, entry: dict) -> None:
    path = Path(audit_dir) / HEAD_NAME
    temp = path.with_suffix(".tmp")
    data = {
        "last_event_id": entry.get("event_id") or "",
        "last_hash": entry.get("entry_hash") or "",
        "last_ts": entry.get("ts") or "",
        "updated_at": now_iso(),
    }
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def append_demo_locked(audit_dir: Path, record: dict, *, day: str | None = None) -> dict:
    """在已持有文件锁的前提下追加一条链记录（调用方负责 :func:`file_lock`）。"""
    directory = Path(audit_dir)
    directory.mkdir(parents=True, exist_ok=True)
    head = chain_tail(directory)
    entry = make_entry(record, head["last_hash"])
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    path = directory / f"audit-{day}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    _write_head(directory, entry)
    return entry


def head_event_id(audit_dir: Path) -> str:
    head = chain_tail(audit_dir)
    return head["last_event_id"]


# ----------------------------------------------------------------------
# production：audit_events 表链
# ----------------------------------------------------------------------


def _row_record(row) -> dict:
    detail = row["detail"]
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    return {
        "id": row["id"],
        "ts": db_layer.to_iso(row["ts"]),
        "actor": row["actor"],
        "action": row["action"],
        "wf_id": row["workflow_id"],
        "target": row["target"],
        "params": detail or {},
        "result": row["result"],
        "command_id": row["command_id"] if "command_id" in row.keys() else "",
        "event_id": row["event_id"] if "event_id" in row.keys() else "",
        "prev_hash": row["prev_hash"] if "prev_hash" in row.keys() else "",
        "entry_hash": row["entry_hash"] if "entry_hash" in row.keys() else "",
    }


def _lock_head(conn) -> str:
    table = db_layer.audit_chain_heads
    row = conn.execute(
        select(table).where(table.c.scope == SCOPE).with_for_update()
    ).mappings().first()
    if row is not None:
        return row["last_hash"] or ""
    try:
        conn.execute(
            table.insert().values(
                scope=SCOPE,
                last_event_id="",
                last_hash=GENESIS,
                updated_at=db_layer.now_dt(),
            )
        )
    except IntegrityError:
        # 并发首写：另一事务已插入链头，重新加锁读取
        row = conn.execute(
            select(table).where(table.c.scope == SCOPE).with_for_update()
        ).mappings().first()
        return (row["last_hash"] if row else "") or ""
    return GENESIS


def append_db(conn, record: dict, *, command_id: str | None = None) -> dict:
    """在调用方事务内追加一条审计链记录（SELECT FOR UPDATE 串行化链头）。"""
    prev_hash = _lock_head(conn)
    entry = make_entry({**record, "command_id": command_id or record.get("command_id") or ""}, prev_hash)
    conn.execute(
        db_layer.audit_events.insert().values(
            ts=db_layer.to_dt(entry["ts"]),
            actor=entry["actor"],
            action=entry["action"],
            workflow_id=entry.get("wf_id") or "",
            target=entry.get("target"),
            detail=entry.get("params") or {},
            result=entry.get("result") or "ok",
            mode="production",
            event_id=entry["event_id"],
            command_id=entry.get("command_id") or None,
            prev_hash=entry["prev_hash"],
            entry_hash=entry["entry_hash"],
        )
    )
    conn.execute(
        db_layer.audit_chain_heads.update()
        .where(db_layer.audit_chain_heads.c.scope == SCOPE)
        .values(
            last_event_id=entry["event_id"],
            last_hash=entry["entry_hash"],
            updated_at=db_layer.now_dt(),
        )
    )
    return entry


def _archive_segments(engine) -> list[dict]:
    table = db_layer.audit_archives
    with engine.connect() as conn:
        rows = conn.execute(
            select(table).order_by(table.c.day.asc(), table.c.archive_id.asc())
        ).mappings().all()
    return [dict(row) for row in rows]


def verify_db(engine, *, since=None, until=None, start_hash: str | None = None) -> dict:
    """校验 production 审计表；``since/until`` 可分段（start_hash 由前段链尾提供）。

    全量校验（不传 since/until/start_hash）时先校验归档副本（purge 后表内已无
    这些行），再以归档链尾为起点校验活动行，避免清理后误报断链。未 purge 的
    归档段（行仍在表中）跳过副本，统一按活动链校验，避免重复计数。
    """
    table = db_layer.audit_events
    archived_checked = 0
    archived_legacy = 0
    full = since is None and until is None and start_hash is None
    if full:
        running = GENESIS
        chain_started = False
        for segment in _archive_segments(engine):
            last_id = segment.get("last_id")
            still_live = False
            if last_id is not None:
                with engine.connect() as conn:
                    still_live = conn.execute(
                        select(table.c.id).where(table.c.id == int(last_id)).limit(1)
                    ).first() is not None
            if still_live:
                continue
            name = Path(str(segment.get("path") or "")).name
            path = Path(str(segment.get("path") or ""))
            if not path.is_file():
                return _verify_failure(f"归档副本缺失：{name or segment.get('archive_id')}")
            data = path.read_bytes()
            if _sha256(data) != str(segment.get("manifest_sha256") or ""):
                return _verify_failure(f"{name} 归档 SHA-256 不匹配（疑似篡改）", source=name)
            rows, bad = _parse_lines(path)
            if bad:
                return _verify_failure(f"{name} 含 {bad} 行无法解析（疑似被截断或改写）", source=name)
            result = verify_records(rows, start_hash=running if chain_started else None)
            if not result["ok"]:
                return result
            archived_checked += result["checked"]
            archived_legacy += result["legacy"]
            running = result["head"]
            chain_started = result["chain_started"]
        start_hash = running if chain_started else None
    query = select(table).order_by(table.c.id.asc())
    if since is not None:
        query = query.where(table.c.id >= since)
    if until is not None:
        query = query.where(table.c.id <= until)
    with engine.connect() as conn:
        rows = [_row_record(row) for row in conn.execute(query).mappings()]
    result = verify_records(rows, start_hash=start_hash)
    result["checked"] += archived_checked
    result["legacy"] += archived_legacy
    return result


# ----------------------------------------------------------------------
# 归档（demo JSONL / production 表行）
# ----------------------------------------------------------------------


def _manifest_path(audit_dir: Path) -> Path:
    return Path(audit_dir) / ARCHIVE_DIR / MANIFEST_NAME


def load_manifest(audit_dir: Path) -> dict:
    path = _manifest_path(audit_dir)
    if not path.is_file():
        return {"version": 1, "updated_at": "", "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditChainError(f"归档清单不可读：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise AuditChainError("归档清单格式非法")
    return data


def _save_manifest(audit_dir: Path, manifest: dict) -> None:
    path = _manifest_path(audit_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = now_iso()
    temp = path.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def _archive_rows(path: Path) -> tuple[list[dict], str]:
    rows, bad = _parse_lines(path)
    if bad:
        raise AuditChainError(f"{path.name} 含 {bad} 行无法解析，拒绝归档")
    return rows, _sha256(path.read_bytes())


def archive_demo(
    audit_dir: Path, cutoff_day: str, *, purge: bool = False, now: datetime | None = None
) -> dict:
    """归档 ``day < cutoff_day``（YYYYMMDD）的 JSONL；仅链完整时执行。"""
    directory = Path(audit_dir)
    verification = verify_demo(directory)
    if not verification["ok"]:
        return {
            "archived": 0,
            "purged": 0,
            "skipped": 0,
            "error": f"审计链校验失败，拒绝归档：{verification['error']}",
            "verification": verification,
        }
    manifest = load_manifest(directory)
    by_id = {entry.get("archive_id"): entry for entry in manifest["entries"]}
    archived = 0
    purged = 0
    skipped = 0
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for path in audit_files(directory):
        match = _AUDIT_FILE_RE.match(path.name)
        if not match or match.group(1) >= cutoff_day:
            continue
        archive_id = f"audit-{match.group(1)}"
        existing = by_id.get(archive_id)
        if existing and existing.get("status") in ("archived", "purged"):
            archive_path = Path(existing.get("path") or "")
            if archive_path.is_file() and _sha256(archive_path.read_bytes()) == existing.get("sha256"):
                skipped += 1
                if purge and existing.get("status") == "archived":
                    path.unlink(missing_ok=True)
                    existing["status"] = "purged"
                    existing["purged_at"] = stamp
                    purged += 1
                continue
        rows, digest = _archive_rows(path)
        archive_path = directory / ARCHIVE_DIR / path.name
        _atomic_write(archive_path, path.read_bytes())
        entry = {
            "archive_id": archive_id,
            "day": match.group(1),
            "path": str(archive_path),
            "rows": len(rows),
            "first_ts": rows[0].get("ts") if rows else None,
            "last_ts": rows[-1].get("ts") if rows else None,
            "chain_ok": True,
            "sha256": digest,
            "status": "archived",
            "created_at": stamp,
        }
        by_id[archive_id] = entry
        archived += 1
        if purge:
            path.unlink(missing_ok=True)
            entry["status"] = "purged"
            entry["purged_at"] = stamp
            purged += 1
    manifest["entries"] = sorted(
        by_id.values(), key=lambda item: str(item.get("day") or ""), reverse=True
    )
    _save_manifest(directory, manifest)
    return {
        "archived": archived,
        "purged": purged,
        "skipped": skipped,
        "error": "",
        "verification": verification,
        "manifest": str(_manifest_path(directory)),
    }


def _archive_id(day: str) -> str:
    return f"audit-db-{day}"


def _archive_head_before(engine, first_id: int) -> str | None:
    """取 ``last_id < first_id`` 的最近归档段的链尾哈希（校验副本 SHA-256）。"""
    table = db_layer.audit_archives
    with engine.connect() as conn:
        row = conn.execute(
            select(table)
            .where(table.c.last_id.is_not(None), table.c.last_id < first_id)
            .order_by(table.c.last_id.desc())
            .limit(1)
        ).mappings().first()
    if row is None:
        return None
    path = Path(str(row["path"] or ""))
    if not path.is_file():
        raise AuditChainError(f"归档副本缺失（{path.name or row['archive_id']}），拒绝归档")
    data = path.read_bytes()
    if _sha256(data) != str(row["manifest_sha256"] or ""):
        raise AuditChainError(f"{path.name} 归档 SHA-256 不匹配，拒绝归档")
    rows, bad = _parse_lines(path)
    if bad or not rows:
        raise AuditChainError(f"{path.name} 归档内容不可用，拒绝归档")
    return rows[-1].get("entry_hash") or None


def archive_db(
    engine,
    archive_dir: Path,
    cutoff: datetime,
    *,
    purge: bool = False,
    now: datetime | None = None,
) -> dict:
    """归档 ``ts < cutoff`` 的审计表行；仅当该段链校验通过才写清单并可选删除。

    ``archive_dir`` 由调用方按运行模式注入（与 demo 审计根一致），本模块不猜测目录。
    """
    table = db_layer.audit_events
    cutoff_naive = db_layer.to_dt(cutoff)
    with engine.connect() as conn:
        rows = [
            _row_record(row)
            for row in conn.execute(
                select(table).where(table.c.ts < cutoff_naive).order_by(table.c.id.asc())
            ).mappings()
        ]
    if not rows:
        return {"archived": 0, "purged": 0, "error": "", "days": []}
    first_id = int(rows[0]["id"])
    with engine.connect() as conn:
        predecessor = conn.execute(
            select(table).where(table.c.id < first_id).order_by(table.c.id.desc()).limit(1)
        ).mappings().first()
    if predecessor is not None:
        # 前驱未上链（历史行）时不给出 start_hash，让本段从创世起点容忍历史行
        start_hash = predecessor["entry_hash"] or None
    else:
        # 前驱已被归档 purge：从最近的归档副本恢复链尾，避免二次归档失败
        start_hash = _archive_head_before(engine, first_id)
    groups: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row["ts"] or "")[:10].replace("-", "") or "unknown"
        groups.setdefault(day, []).append(row)
    return _archive_db_groups(engine, groups, start_hash, Path(archive_dir), purge, now)


def _archive_db_groups(
    engine,
    groups: dict[str, list[dict]],
    start_hash: str | None,
    archive_dir: Path,
    purge: bool,
    now: datetime | None,
) -> dict:
    result = {"archived": 0, "purged": 0, "error": "", "days": []}
    running = start_hash
    for day, rows in groups.items():
        verification = verify_records(rows, start_hash=running)
        if not verification["ok"]:
            result["error"] = f"{day} 审计链校验失败，拒绝归档：{verification['error']}"
            return result
        running = verification["head"]
        payload = "\n".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for row in rows
        ).encode("utf-8") + b"\n"
        digest = _sha256(payload)
        path = Path(archive_dir) / ARCHIVE_DIR / f"audit-db-{day}.jsonl"
        _atomic_write(path, payload)
        archive_id = _archive_id(day)
        table = db_layer.audit_archives
        stamp = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        values = {
            "day": day,
            "path": str(path),
            "rows": len(rows),
            "first_id": int(rows[0]["id"]),
            "last_id": int(rows[-1]["id"]),
            "first_ts": db_layer.to_dt(rows[0]["ts"]),
            "last_ts": db_layer.to_dt(rows[-1]["ts"]),
            "chain_ok": True,
            "manifest_sha256": digest,
            "status": "purged" if purge else "archived",
            "created_at": stamp,
        }
        with engine.begin() as conn:
            exists = conn.execute(
                select(table.c.archive_id).where(table.c.archive_id == archive_id)
            ).scalar()
            if exists is None:
                conn.execute(table.insert().values(archive_id=archive_id, **values))
            else:
                conn.execute(table.update().where(table.c.archive_id == archive_id).values(**values))
            if purge:
                conn.execute(delete(db_layer.audit_events).where(db_layer.audit_events.c.id.in_(
                    [int(row["id"]) for row in rows]
                )))
        result["archived"] += 1
        if purge:
            result["purged"] += len(rows)
        result["days"].append(day)
    return result
