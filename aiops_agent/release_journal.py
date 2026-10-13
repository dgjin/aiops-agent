"""单主机发布日志；跨模式共享锁和资源占用，不使用时间租约。"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

# 不随 mode/data_root 改变；同一主机的两个 worker 必须使用此同一控制目录。
CONTROL_ROOT = Path(__file__).resolve().parents[1] / "data" / "release-control"


class ReleaseSafetyError(RuntimeError):
    """不可自动重试；占用/证据保留，交由人工核对。"""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def plain(path: Path | str) -> Path:
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts or any(x.is_symlink() for x in (p, *p.parents)):
        raise ReleaseSafetyError("拒绝非绝对路径、目录穿越或符号链接")
    return p


def _private(path: Path, directory=False) -> None:
    plain(path)
    s = path.stat()
    if (s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink == 1)):
        raise ReleaseSafetyError("发布控制文件权限/身份异常")


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: dict) -> None:
    plain(path)
    if path.exists():
        _private(path)
    with tempfile.TemporaryDirectory(prefix=".journal-", dir=path.parent) as tmp:
        staged = Path(tmp) / "state"
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, ensure_ascii=False, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(staged, path)
        sync_dir(path.parent)


def coordination_paths() -> dict[str, str]:
    """H批次release_lock及release_records/release_recovery使用此实际共享位置。"""
    return {"release_lock": str(CONTROL_ROOT / "release.lock"),
            "release_records": str(CONTROL_ROOT), "release_recovery": str(CONTROL_ROOT)}


@contextlib.contextmanager
def locked():
    # ponytail: 单主机全局执行锁串行化发布；资源占用按目标持久化，扩多主机前须重设计。
    root = plain(CONTROL_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(root, True)
    path = root / "release.lock"
    plain(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _private(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseSafetyError("备份或旧发布线程仍在执行，拒绝重入") from None
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def resource_locks(resources):
    """锁文件名只取真实资源hash，不含模式、service或Workflow身份。"""
    descriptors = []
    try:
        for resource in sorted(set(resources)):
            path = plain(CONTROL_ROOT / ("resource-" + digest(resource) + ".lock"))
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            _private(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseSafetyError("实际资源锁被旧执行线程占用") from None
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _read() -> dict:
    path = plain(CONTROL_ROOT / "journal.json")
    if not path.exists():
        if (CONTROL_ROOT / "release.lock").stat().st_size:
            raise ReleaseSafetyError("发布日志丢失，拒绝重新初始化或接管")
        return {"format": 1, "records": {}}
    try:
        _private(path)
        data = json.loads(path.read_text())
        if data["format"] != 1 or not isinstance(data["records"], dict):
            raise ValueError()
        for rec in data["records"].values():
            if (rec["status"] not in {"occupied", "running", "manual", "terminal"}
                    or not isinstance(rec["resources"], list) or not rec["resources"]
                    or not isinstance(rec["stages"], dict) or not isinstance(rec["target"], dict)
                    or not isinstance(rec["patch_hash"], str)):
                raise ValueError()
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseSafetyError("发布记录不可读/损坏，禁止接管目标") from exc


def assert_idle_locked() -> None:
    """调用者须已持有 coordination_paths()['release_lock'] EX锁（与backup.lock兼容）。"""
    if any(r["status"] != "terminal" for r in _read()["records"].values()):
        raise ReleaseSafetyError("存在跨阶段发布占用或待人工核对记录，备份不得宣称发布空闲")


class Journal:
    """仅在locked()内创建和使用；整阶段锁由执行线程持有。"""
    def __init__(self, workflow_id: str, namespace: str, patch_id: str):
        if not workflow_id or not namespace or not patch_id:
            raise ReleaseSafetyError("缺少发布幂等身份")
        self.data = _read()
        self.key = digest([namespace, workflow_id, patch_id])
        self.identity = {"workflow_id": workflow_id, "namespace": namespace, "patch_id": patch_id}
        self.record = self.data["records"].get(self.key)

    def save(self) -> None:
        write_json(CONTROL_ROOT / "journal.json", self.data)
        fd = os.open(CONTROL_ROOT / "release.lock", os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.write(fd, b"release-journal-v1\n")
            os.fsync(fd)
        finally:
            os.close(fd)

    def claim(self, patch_hash: str, target: dict, resources: list[str]) -> None:
        for key, other in self.data["records"].items():
            if other["patch_id"] == self.identity["patch_id"] and other["patch_hash"] != patch_hash:
                raise ReleaseSafetyError("相同patch内容hash冲突")
            if key != self.key and other["status"] != "terminal" and set(resources) & set(other["resources"]):
                raise ReleaseSafetyError("实际目标已有未结束发布占用，拒绝抢占")
        if self.record is not None:
            if self.record["target"] != target or self.record["patch_hash"] != patch_hash:
                raise ReleaseSafetyError("发布目标或补丁hash变化")
            return
        self.record = {**self.identity, "patch_hash": patch_hash, "target": target,
                       "resources": resources, "status": "occupied", "stages": {}, "before": {}}
        self.data["records"][self.key] = self.record
        self.save()

    def begin(self, stage: str, inputs: dict):
        if self.record is None:
            raise ReleaseSafetyError("缺少canary持久化记录；旧活动不能猜测目标")
        entry = self.record["stages"].get(stage)
        if entry and entry["input_hash"] != digest(inputs):
            raise ReleaseSafetyError("阶段输入hash冲突")
        if self.record["status"] == "manual":
            raise ReleaseSafetyError("发布待人工核对，禁止自动重试")
        if self.record["status"] == "running":
            raise ReleaseSafetyError("上次进程中断；必须核对恢复，不得重做副作用")
        if entry and entry.get("result") is not None:
            return entry["result"]
        if self.record["status"] == "terminal":
            raise ReleaseSafetyError("发布已结束，拒绝新增阶段")
        self.record["status"] = "running"
        self.record["phase"] = stage
        self.record["stages"][stage] = {"input_hash": digest(inputs), "result": None}
        self.save()
        return None

    def update(self, **values) -> None:
        self.record.update(values)
        self.save()

    def finish(self, stage: str, result: dict) -> None:
        self.record["stages"][stage]["result"] = result
        self.record["status"] = ("terminal" if stage == "finalize" and
                                 self.record["target"]["kind"] == "direct" else "occupied")
        self.save()

    def manual(self, recovered: bool, reason: str = "待人工核对") -> None:
        self.update(status="manual", recovery_verified=recovered, failure_reason=reason)
