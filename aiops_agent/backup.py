"""受限备份与隔离恢复；不导入应用配置、不加载 dotenv、不管理服务。"""
from __future__ import annotations

import contextlib
import configparser
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import mode, release_journal

ROOT = Path(__file__).resolve().parents[1]
CHUNK = 1024 * 1024
MAGIC = b"AIOPS-AES256GCM-1\n"
MAX_AGE = 26 * 3600
ROLES = {"policy", "deployment", "environment", "tls", "notifications", "audit",
         "release_records", "release_recovery", "positions"}
# login簇选项（如--no-login-paths）必须位于常规选项之前，实测9.x严格校验，不混入本列表。
DUMP_OPTIONS = ["--single-transaction", "--skip-lock-tables", "--routines", "--events",
                "--triggers", "--hex-blob", "--complete-insert", "--skip-extended-insert",
                "--order-by-primary", "--skip-comments", "--skip-dump-date",
                "--skip-add-locks", "--skip-disable-keys", "--no-tablespaces",
                "--set-gtid-purged=OFF", "--default-character-set=utf8mb4"]


class BackupError(RuntimeError):
    """仅包含可对外显示的脱敏原因。"""


def plain(path: Path | str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() or ".." in p.parts:
        raise BackupError("必须使用无路径穿越的绝对路径")
    if any(x.is_symlink() for x in (p, *p.parents)):
        raise BackupError("拒绝符号链接路径")
    return p


def regular(path: Path, private: bool = False) -> os.stat_result:
    plain(path)
    s = path.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise BackupError("只允许单链接普通文件")
    if private and (stat.S_IMODE(s.st_mode) != 0o600 or s.st_uid != os.getuid()):
        raise BackupError("受保护文件必须属于当前用户且权限为600")
    return s


def private_dir(path: Path) -> None:
    plain(path)
    if not path.exists():
        if not path.parent.exists():
            private_dir(path.parent)
        path.mkdir(mode=0o700)
    s = path.stat()
    if not stat.S_ISDIR(s.st_mode) or stat.S_IMODE(s.st_mode) != 0o700 or s.st_uid != os.getuid():
        raise BackupError("输出目录必须属于当前用户且权限为700")


@contextlib.contextmanager
def new_file(path: Path):
    plain(path)
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: dict) -> None:
    plain(path)
    if path.exists():
        regular(path, True)
    with tempfile.TemporaryDirectory(prefix=".state-", dir=path.parent) as tmp:
        staged = Path(tmp) / "state"
        with new_file(staged) as out:
            out.write(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        os.replace(staged, path)
        sync_dir(path.parent)


def read_json(path: Path) -> dict:
    regular(path, True)
    if path.stat().st_size > 4 * CHUNK:
        raise BackupError("配置或状态文件超过大小上限")
    with path.open("rb") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise BackupError("需要JSON对象")
    return value


def load_config(path: Path, recovery: bool = False) -> dict:
    cfg = read_json(plain(path))
    cfg["config_path"] = str(path)
    current = mode.mode()
    if cfg.get("mode") != current:
        raise BackupError("备份模式与AIOPS_MODE不一致")
    if recovery:
        # 灾后验证只依赖归档与独立密钥，不能要求原主机数据根/数据库仍然存在。
        directory, key = plain(cfg.get("backup_dir", "")), plain(cfg.get("key_file", ""))
        if key.is_relative_to(ROOT) or key.is_relative_to(directory) or directory.is_relative_to(key):
            raise BackupError("恢复密钥必须独立于仓库和归档目录")
        regular(key, True)
        if key.stat().st_size != 32 or stat.S_IMODE(key.parent.stat().st_mode) != 0o700:
            raise BackupError("需要700目录内的32字节独立密钥")
        if cfg.get("offsite", {"enabled": False}) != {"enabled": False}:
            raise BackupError("异地入口必须保持disabled")
        return {**cfg, "backup_dir": str(directory), "key_file": str(key)}
    # 沿用模式模块的默认数据根，显式覆盖必须与运行配置一致。
    if os.environ.get("AIOPS_DATA_ROOT"):
        plain(os.environ["AIOPS_DATA_ROOT"])
    actual = mode.data_root()
    if plain(cfg.get("data_root", "")) != actual:
        raise BackupError("备份数据根与当前模式的实际数据根不一致")
    for name in ("backup_dir", "key_file"):
        cfg[name] = str(plain(cfg.get(name, "")))
    if cfg.get("offsite", {"enabled": False}) != {"enabled": False}:
        raise BackupError("异地入口未配置且必须保持disabled；本工具不上传")
    sources = cfg.get("sources", {})
    if (not isinstance(sources, dict) or {"data", "mysql", "backup_configuration", "mysql_credentials"} & sources.keys()
            or any(not re.fullmatch(r"[a-z_]+", n) for n in sources)):
        raise BackupError("来源名称只允许小写字母和下划线")
    if current == "production":
        if not ROLES <= sources.keys():
            raise BackupError("production缺少关键文件来源配置")
        mysql = cfg.get("mysql", {})
        names = mysql.get("databases", [])
        if (len(names) != 3 or len(set(names)) != 3
                or any(not re.fullmatch(r"[A-Za-z0-9_]+", n) for n in names)):
            raise BackupError("production必须明确三个不同的MySQL库名")
        if any(n in {"mysql", "sys", "information_schema", "performance_schema"} for n in names):
            raise BackupError("不得备份系统库作为业务三库")
        for field in ("defaults_file", "dump_binary"):
            regular(plain(mysql.get(field, "")), field == "defaults_file")
        for field in ("quiescence_file", "release_lock"):
            regular(plain(cfg.get(field, "")), True)
        # H接线：备份与发布必须用同一个实际共享锁/记录位置，不用无关空锁冒充发布空闲。
        shared = release_journal.coordination_paths()
        if plain(cfg["release_lock"]) != plain(shared["release_lock"]):
            raise BackupError("release_lock必须使用发布控制目录的共享锁")
        for role in ("release_records", "release_recovery"):
            if plain(sources[role]) != plain(shared[role]):
                raise BackupError(role + "必须指向发布控制目录的实际共享位置")
    elif cfg.get("mysql"):
        raise BackupError("demo禁止连接MySQL；请使用文件备份")
    roots = {"data": str(actual), **sources, "backup_configuration": str(path)}
    if current == "production":
        roots["mysql_credentials"] = cfg["mysql"]["defaults_file"]
        # 兼容尚未迁入模式数据根的既有通知、发布与采集位点，不漏掉生产留痕。
        for name in ("notify", "argocd", "log_ship_positions.json"):
            legacy = ROOT / "data" / name
            if legacy.exists():
                roots["legacy_" + name.replace(".", "_")] = str(legacy)
        if os.environ.get("AIOPS_POLICY_PATH") and plain(os.environ["AIOPS_POLICY_PATH"]) != plain(sources["policy"]):
            raise BackupError("备份策略路径与运行策略不一致")
    out, key = Path(cfg["backup_dir"]), Path(cfg["key_file"])
    if key.is_relative_to(ROOT) or key.is_relative_to(out) or out.is_relative_to(key):
        raise BackupError("密钥必须独立于仓库和归档目录保管")
    for name, source in roots.items():
        p = plain(source)
        if not p.exists():
            raise BackupError("必需的备份来源不存在：" + name)
        if key == p or key.is_relative_to(p) or out == p or out.is_relative_to(p) or p.is_relative_to(out):
            raise BackupError("来源不得包含密钥或与归档目录重叠")
    regular(key, True)
    if key.stat().st_size != 32 or stat.S_IMODE(key.parent.stat().st_mode) != 0o700:
        raise BackupError("需要独立700目录内人工提供的32字节AES密钥；工具不会生成")
    cfg["sources"] = roots
    cfg.setdefault("offsite", {"enabled": False})
    return cfg


def read_key(cfg: dict) -> bytes:
    path = plain(cfg["key_file"])
    expected = regular(path, True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino, current.st_size) != (expected.st_dev, expected.st_ino, 32):
            raise BackupError("密钥身份或长度已变化")
        key = stream.read(33)
    if len(key) != 32:
        raise BackupError("AES256密钥长度错误")
    return key


def clean_env() -> dict:
    # 不继承MYSQL_PWD、dotenv、应用凭证或代理；容器工具保留连接上下文。
    keys = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
    return {**{k: os.environ[k] for k in keys if k in os.environ}, "LC_ALL": "C"}


def run_file(argv: list[str], output: Path, stdin=None) -> None:
    # 大容量SQL直接流向600文件，stderr不进入内存或日志。
    with new_file(output) as out:
        try:
            subprocess.run(argv, stdin=stdin, stdout=out, stderr=subprocess.DEVNULL,
                           env=clean_env(), check=True, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            raise BackupError("外部导出或导入失败/超时；原始错误已隐藏") from None


def sql_stats(path: Path) -> dict:
    """逐行统计导出内容；每行一条INSERT，内容指纹不依赖返回顺序。"""
    tables, databases = {}, set()
    database = table = None
    schema_hash = None
    with path.open("rb") as stream:
        for line in stream:
            match = re.match(rb"USE `([A-Za-z0-9_]+)`;", line)
            if match:
                database = match[1].decode()
                databases.add(database)
            match = re.match(rb"CREATE TABLE `([^`]+)`", line)
            if match:
                if database is None:
                    raise BackupError("SQL缺少数据库上下文")
                table = database + "." + match[1].decode()
                if table in tables:
                    raise BackupError("SQL重复定义表")
                tables[table] = {"rows": 0, "content_sum": 0, "schema_sha256": ""}
                schema_hash = hashlib.sha256()
            if schema_hash is not None:
                schema_hash.update(line)
                if line.rstrip().endswith(b";"):
                    tables[table]["schema_sha256"] = schema_hash.hexdigest()
                    schema_hash = None
            match = re.match(rb"INSERT INTO `([^`]+)` .* VALUES \(", line)
            if match:
                name = str(database) + "." + match[1].decode()
                if name not in tables or not line.rstrip().endswith(b");"):
                    raise BackupError("SQL不是受支持的单行INSERT导出")
                item = tables[name]
                item["rows"] += 1
                item["content_sum"] = (item["content_sum"] + int.from_bytes(
                    hashlib.sha256(line.rstrip(b"\r\n")).digest(), "big")) % (1 << 256)
    if schema_hash is not None or not tables:
        raise BackupError("SQL导出为空或表结构不完整")
    for item in tables.values():
        item["content_sha256_sum"] = f"{item.pop('content_sum'):064x}"
    return {"databases": sorted(databases), "tables": tables,
            "rows": sum(t["rows"] for t in tables.values())}


def dump_mysql(cfg: dict, target: Path) -> dict:
    """MySQL8备份锁禁止DDL，三库通过一次single-transaction导出。"""
    import pymysql
    mysql = cfg["mysql"]
    conn = None
    try:
        defaults = configparser.RawConfigParser()
        credential = plain(mysql["defaults_file"])
        regular(credential, True)
        with credential.open() as stream:
            defaults.read_file(stream)
        client = defaults["client"]
        if (not all(client.get(k) for k in ("user", "password", "host", "port"))
                or client["user"].lower() == "root" or client["host"] not in {"127.0.0.1", "::1"}
                or not 1 <= int(client["port"]) <= 65535 or client.get("socket")):
            raise BackupError("生产导出需明确的loopback TCP端点及非root专用凭证，禁止隐式默认连接")
        conn = pymysql.connect(read_default_file=mysql["defaults_file"],
                               read_default_group="client", connect_timeout=10,
                               read_timeout=60, write_timeout=60, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("LOCK INSTANCE FOR BACKUP")
            names = mysql["databases"]
            cur.execute("SELECT TABLE_SCHEMA,TABLE_NAME,ENGINE,TABLE_TYPE FROM information_schema.tables "
                        "WHERE TABLE_SCHEMA IN (%s,%s,%s)", names)
            tables = cur.fetchall()
            if {row[0] for row in tables} != set(names) or any(
                    row[2] != "InnoDB" or row[3] != "BASE TABLE" for row in tables):
                raise BackupError("三库必须存在且仅含InnoDB基础表；其他类型需独立一致性方案")
            cur.execute("SELECT VERSION()")
            server_version = cur.fetchone()[0]
            # --no-login-paths属login簇，必须紧跟--defaults-file；8.0客户端无此选项（恢复路径不传）。
            run_file([mysql["dump_binary"], "--defaults-file=" + mysql["defaults_file"], "--no-login-paths",
                      "--protocol=TCP", "--host=" + client["host"], "--port=" + client["port"],
                      "--user=" + client["user"], *DUMP_OPTIONS, "--databases", *names], target)
            result = sql_stats(target)
            if set(result["tables"]) != {r[0] + "." + r[1] for r in tables}:
                raise BackupError("导出表内容清单与受锁定schema不一致")
            result["server_version"] = server_version
            cur.execute("UNLOCK INSTANCE")
            return result
    except BackupError:
        raise
    except Exception:
        raise BackupError("MySQL一致性导出失败；检查专用凭证、BACKUP_ADMIN和三库只读权限") from None
    finally:
        if conn is not None:
            conn.close()


@contextlib.contextmanager
def lock(path: Path, existing: bool = False):
    plain(path)
    if existing or path.exists():
        regular(path, True)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (0 if existing else os.O_CREAT), 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise BackupError("拒绝非独占锁文件")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("备份或发布正在执行，拒绝重入") from None
        yield
    finally:
        os.close(fd)


def quiescence(cfg: dict) -> None:
    if cfg["mode"] != "production":
        return
    gate = read_json(Path(cfg["quiescence_file"]))
    now = time.time()
    if (gate.get("mode") != cfg["mode"] or gate.get("data_root") != cfg["data_root"]
            or gate.get("writers_paused") is not True or gate.get("releases_idle") is not True
            or not now < gate.get("expires_at", 0) <= now + 3600):
        raise BackupError("缺少有效写入暂停/发布空闲确认；禁止自动停止服务或假设全局一致性")


def signature(path: Path) -> tuple:
    s = regular(path)
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def inventory(cfg: dict) -> tuple[dict, dict]:
    """收集完整数据根；TLS唯一受控current链接物化为普通目录。"""
    files, notes = {}, {}
    key_s = regular(Path(cfg["key_file"]), True)
    for role, raw in cfg["sources"].items():
        root = plain(raw)
        def visit(p: Path, relative: str) -> None:
            arc = role + "/" + relative
            if p.is_symlink():
                tls_root = Path(cfg["sources"].get("tls", "/__not_configured__"))
                if p == tls_root / "current":
                    dest = plain(p.resolve(strict=True))
                    if not dest.is_relative_to(tls_root) or dest == tls_root or not dest.is_dir():
                        raise BackupError("TLS current只能指向同TLS目录的版本目录")
                    notes[arc] = {"materialized_link": str(dest.relative_to(tls_root))}
                    for child in sorted(dest.iterdir()):
                        visit(child, relative + "/" + child.name)
                    return
                raise BackupError("备份来源含不受信符号链接")
            plain(p)
            if p.is_dir():
                # demo默认根的prod子目录属于另一模式，不混入归档。
                if role == "data" and cfg["mode"] == "demo" and p == ROOT / "data" / "prod":
                    notes[arc] = {"excluded": "另一模式的数据根"}
                    return
                children = sorted(p.iterdir())
                if not children:
                    notes[arc] = {"empty_directory": True}
                for child in children:
                    visit(child, (relative + "/" if relative else "") + child.name)
            elif p.is_socket() and role == "deployment" and p.name == "caddy-admin.sock":
                notes[arc] = {"excluded": "运行期管理套接字，不是恢复材料"}
            else:
                s = regular(p)
                if (s.st_dev, s.st_ino) == (key_s.st_dev, key_s.st_ino):
                    raise BackupError("密钥不得通过别名进入归档")
                files[arc] = (p, signature(p))
        visit(root, root.name if root.is_file() else "")
    if cfg["mode"] == "production":
        required_tls = {"tls/ca/ca.crt", "tls/ca/ca.key", "tls/current/leaf.crt", "tls/current/leaf.key"}
        if not required_tls <= files.keys():
            raise BackupError("缺少本地CA或当前TLS证书/私钥材料")
    return files, notes


def digest_file(path: Path) -> dict:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            size += len(block)
            digest.update(block)
    return {"size": size, "sha256": digest.hexdigest()}


def copy_snapshot(source: Path, target: Path, expected: tuple) -> dict:
    if signature(source) != expected:
        raise BackupError("来源在备份期间发生变化")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(fd, "rb") as src, new_file(target) as out:
        s = os.fstat(src.fileno())
        if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) != expected:
            raise BackupError("来源被替换，拒绝不一致快照")
        while block := src.read(CHUNK):
            out.write(block)
            digest.update(block)
            size += len(block)
    if signature(source) != expected or size != expected[2]:
        raise BackupError("来源在备份期间发生变化")
    return {"size": size, "sha256": digest.hexdigest()}


class EncryptWriter:
    """将tar/gzip输出直接送入AES-GCM，不创建明文压缩归档。"""
    def __init__(self, stream, key: bytes):
        self.stream = stream
        nonce = os.urandom(12)
        header = MAGIC + nonce
        self.encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        self.encryptor.authenticate_additional_data(header)
        stream.write(header)

    def write(self, data: bytes) -> int:
        self.stream.write(self.encryptor.update(data))
        return len(data)

    def finish(self) -> None:
        self.stream.write(self.encryptor.finalize())
        self.stream.write(self.encryptor.tag)


def encrypt_archive(staging: Path, manifest: dict, target: Path, key: bytes) -> None:
    with new_file(target) as out:
        writer = EncryptWriter(out, key)
        with tarfile.open(fileobj=writer, mode="w|gz") as tar:
            content = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(content), 0o600
            tar.addfile(info, io.BytesIO(content))
            for name in sorted(manifest["files"]):
                path = staging / name
                info = tarfile.TarInfo(name)
                info.size, info.mode = path.stat().st_size, 0o600
                with path.open("rb") as stream:
                    tar.addfile(info, stream)
        writer.finish()


def decrypt_archive(archive: Path, target: Path, key: bytes) -> None:
    size = regular(archive, True).st_size
    head_size = len(MAGIC) + 12
    if size <= head_size + 16:
        raise BackupError("归档不完整")
    with archive.open("rb") as src, new_file(target) as out:
        header = src.read(head_size)
        if header[:len(MAGIC)] != MAGIC:
            raise BackupError("不支持未加密或旧格式归档")
        src.seek(-16, os.SEEK_END)
        tag = src.read(16)
        dec = Cipher(algorithms.AES(key), modes.GCM(header[-12:], tag)).decryptor()
        dec.authenticate_additional_data(header)
        src.seek(head_size)
        remaining = size - head_size - 16
        while remaining:
            block = src.read(min(CHUNK, remaining))
            if not block:
                raise BackupError("归档被截断")
            out.write(dec.update(block))
            remaining -= len(block)
        try:
            out.write(dec.finalize())
        except Exception:
            raise BackupError("归档认证失败：密钥错误或内容损坏") from None


def safe_member(name: str) -> None:
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or str(p) != name or "\\" in name:
        raise BackupError("归档包含不安全路径")


def verify_archive(archive: Path, key: bytes, work: Path, extract: Path | None = None) -> dict:
    private_dir(work)
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=work) as tmp:
        compressed = Path(tmp) / "archive.tar.gz"
        decrypt_archive(archive, compressed, key)
        seen = set()
        with tarfile.open(compressed, mode="r|gz") as tar:
            first = tar.next()
            if (first is None or first.name != "manifest.json" or not first.isfile()
                    or first.size > 16 * CHUNK or first.mode != 0o600):
                raise BackupError("归档缺少受限manifest")
            manifest = json.load(tar.extractfile(first))
            if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
                raise BackupError("不支持的manifest")
            for member in tar:
                if member.name == "manifest.json" and not seen:
                    # 流式迭代首次返回已经读取的首项。
                    if member is first:
                        continue
                safe_member(member.name)
                if (not member.isfile() or member.mode != 0o600 or member.name in seen
                        or member.name not in manifest["files"]):
                    raise BackupError("归档含重复、链接或未登记成员")
                expected = manifest["files"][member.name]
                if member.size != expected["size"]:
                    raise BackupError("归档文件大小不符")
                seen.add(member.name)
                digest = hashlib.sha256()
                with contextlib.ExitStack() as stack:
                    src = stack.enter_context(tar.extractfile(member))
                    out = stack.enter_context(new_file(extract / member.name)) if extract else None
                    while block := src.read(CHUNK):
                        digest.update(block)
                        if out:
                            out.write(block)
                if digest.hexdigest() != expected["sha256"]:
                    raise BackupError("归档文件校验和不符")
            if seen != set(manifest["files"]):
                raise BackupError("归档遗漏manifest文件")
        return manifest


def status(backup_dir: Path, now: float | None = None, current_mode: str = "production") -> dict:
    now = time.time() if now is None else now
    try:
        data = read_json(plain(backup_dir) / ("status-" + current_mode + ".json"))
        success = data.get("last_success") or {}
        ts = success.get("time", 0)
        stale = not 0 <= now - ts <= MAX_AGE or success.get("verified") is not True
    except (OSError, ValueError, TypeError, AttributeError, BackupError):
        data, stale = {}, True
    return {**data, "stale": stale, "max_age_seconds": MAX_AGE,
            "offsite": "未配置（disabled）", "runtime_acceptance": "blocked"}


def retain(cfg: dict, keep: int, key: bytes) -> None:
    if keep < 1:
        raise BackupError("keep必须大于等于1")
    directory = plain(cfg["backup_dir"])
    pattern = re.compile(r"aiops-" + cfg["mode"] + r"-daily-\d{8}T\d{6}-[a-f0-9]{32}\.aes$")
    verified = []
    for path in sorted(directory.iterdir()):
        if not pattern.fullmatch(path.name):
            continue
        # 只清理由本工具认证的成功日备份；protected及未知文件永不进入删除集合。
        regular(path, True)
        manifest = verify_archive(path, key, directory)
        if manifest.get("kind") != "daily" or manifest.get("mode") != cfg["mode"]:
            raise BackupError("保留策略发现归档身份不符")
        verified.append((manifest["created_at"], path, signature(path)))
    verified.sort(key=lambda item: (item[0], item[1].name))
    parent = directory.stat()
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (parent.st_dev, parent.st_ino):
            raise BackupError("清理目录被替换，已停止")
        for _, path, before in verified[:-keep]:
            current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            actual = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or actual != before:
                raise BackupError("清理目标被替换，已停止")
            os.unlink(path.name, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def create_backup(cfg: dict, keep: int = 7, protected: bool = False) -> Path:
    if keep < 1:
        raise BackupError("keep必须大于等于1")
    directory = Path(cfg["backup_dir"])
    private_dir(directory)
    key = read_key(cfg)
    state_path = directory / ("status-" + cfg["mode"] + ".json")
    with lock(directory / "backup.lock"):
        prior = status(directory, current_mode=cfg["mode"])
        try:
            with contextlib.ExitStack() as stack:
                if cfg["mode"] == "production":
                    stack.enter_context(lock(Path(cfg["release_lock"]), existing=True))
                    # 已持发布共享锁：再核对日志无未结束/待人工核对记录，拒绝拍不一致快照。
                    release_journal.assert_idle_locked()
                quiescence(cfg)
                files, notes = inventory(cfg)
                tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix=".backup-", dir=directory))
                staging = Path(tmp) / "snapshot"
                private_dir(staging)
                manifest = {"format": 1, "tool_version": "H1", "mode": cfg["mode"],
                            "data_root": cfg["data_root"], "created_at": time.time(),
                            "kind": "protected" if protected else "daily",
                            "consistency": "component-level; not a global transaction",
                            "sources": cfg["sources"], "notes": notes, "files": {},
                            "offsite": "disabled", "runtime_acceptance": "blocked"}
                if cfg["mode"] == "production":
                    sql = staging / "mysql" / "all.sql"
                    private_dir(sql.parent)
                    manifest["mysql"] = dump_mysql(cfg, sql)
                    manifest["files"]["mysql/all.sql"] = digest_file(sql)
                for name, (source, sig) in sorted(files.items()):
                    manifest["files"][name] = copy_snapshot(source, staging / name, sig)
                if inventory(cfg) != (files, notes):
                    raise BackupError("文件清单或内容在备份期间变化，未发布归档")
                quiescence(cfg)
                pending = Path(tmp) / "pending.aes"
                encrypt_archive(staging, manifest, pending, key)
                verified = verify_archive(pending, key, directory)
                if verified != manifest:
                    raise BackupError("归档manifest回读不符")
                name = (f"aiops-{cfg['mode']}-{manifest['kind']}-"
                        + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex + ".aes")
                target = directory / name
                os.replace(pending, target)
                sync_dir(directory)
                prior["last_success"] = {"time": time.time(), "archive": name, "verified": True,
                                         **digest_file(target)}
                write_json(state_path, {"last_success": prior["last_success"], "last_failure": None})
            retain(cfg, keep, key)
            return target
        except Exception as exc:
            reason = str(exc) if isinstance(exc, BackupError) else "备份失败；底层错误已隐藏"
            write_json(state_path, {"last_success": prior.get("last_success"),
                                   "last_failure": {"time": time.time(), "reason": reason}})
            raise BackupError(reason) from None


def readiness(cfg: dict) -> dict:
    # 不读密钥、环境文件正文，不连接数据库或容器；结构通过不等于运行验收通过。
    return {"mode": cfg["mode"], "data_root": cfg["data_root"],
            "configuration": "valid", "backup": status(Path(cfg["backup_dir"]), current_mode=cfg["mode"]),
            "offsite": "未配置（disabled）", "runtime_acceptance": "blocked",
            "unverified": ["MySQL真实一致性导出及恢复", "业务API和关键行", "正式Temporal历史及等待流程恢复",
                           "SQLite/Loki维护窗口冷备", "launchd实际调度及告警接线", "RPO/RTO实测"]}


def docker_json(args: list[str]) -> object:
    # inspect仅请求身份/卷/端口字段，避免将容器环境凭证带入输出。
    try:
        proc = subprocess.run(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=clean_env(), check=True, timeout=30)
        if len(proc.stdout) > 4 * CHUNK:
            raise BackupError("容器身份响应异常")
        return json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise BackupError("无法确认独立测试容器身份，恢复被阻断") from None


def validate_target(target: dict) -> None:
    token, container, volume = (target.get(k, "") for k in ("test_marker", "container", "volume"))
    if (not re.fullmatch(r"[a-f0-9]{32}", token)
            or not re.fullmatch(r"[a-f0-9]{64}", target.get("container_id", ""))):
        raise BackupError("需要独立测试标记和完整容器ID")
    if any(not re.fullmatch(r"aiops-backup-test-[a-z0-9-]+", n) for n in (container, volume)):
        raise BackupError("只允许明确命名的独立备份测试容器和卷")
    port = target.get("port", 0)
    if not isinstance(port, int) or not 20000 <= port <= 65535:
        raise BackupError("测试端口必须显式指定为20000以上独立端口")
    template = ('{"id":{{json .Id}},"name":{{json .Name}},"labels":{{json .Config.Labels}},'
                '"mounts":{{json .Mounts}},"ports":{{json .NetworkSettings.Ports}},'
                '"running":{{json .State.Running}},"network":{{json .HostConfig.NetworkMode}},'
                '"networks":{{json .NetworkSettings.Networks}},"privileged":{{json .HostConfig.Privileged}}}')
    info = docker_json(["inspect", "--type=container", "--format", template, container])
    mounts = info.get("mounts", [])
    ports = info.get("ports") or {}
    active_ports = {k: v for k, v in ports.items() if v}
    network = info.get("network", "")
    if not re.fullmatch(r"aiops-backup-test-[a-z0-9-]+", network):
        raise BackupError("只允许专用内部测试网络")
    if (info.get("name") != "/" + container or info.get("id") != target.get("container_id")
            or (info.get("labels") or {}).get("io.aiops.backup-test") != token
            or info.get("running") is not True or info.get("privileged") is not False
            or set(info.get("networks") or {}) != {network}
            or len(mounts) != 1 or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != volume or mounts[0].get("Destination") != "/var/lib/mysql"
            or active_ports != {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}):
        raise BackupError("容器身份、独立卷或loopback端口不匹配；禁止恢复")
    net = docker_json(["network", "inspect", "--format",
                       '{"internal":{{json .Internal}},"labels":{{json .Labels}},"containers":{{json .Containers}}}', network])
    if (net.get("internal") is not True or (net.get("labels") or {}).get("io.aiops.backup-test") != token
            or set(net.get("containers") or {}) != {target["container_id"]}):
        raise BackupError("测试网络必须内部隔离且无其他容器")
    labels = docker_json(["volume", "inspect", "--format", "{{json .Labels}}", volume])
    if not isinstance(labels, dict) or labels.get("io.aiops.backup-test") != token:
        raise BackupError("测试卷缺少匹配标记")
    # 即使停止的其他容器也不能共享演练卷；不创建、停止或删除任何容器。
    try:
        proc = subprocess.run(["docker", "ps", "-aq", "--filter", "volume=" + volume],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              check=True, timeout=30, env=clean_env())
    except (OSError, subprocess.SubprocessError):
        raise BackupError("无法核实卷独占性") from None
    ids = proc.stdout.decode().split()
    if len(ids) != 1 or not target["container_id"].startswith(ids[0]):
        raise BackupError("测试卷被其他容器占用")


def restore_test(cfg: dict, archive: Path, target: dict) -> dict:
    """只向已存在且身份核实的空测试实例灌入；不提供生产恢复路径。"""
    validate_target(target)
    directory = Path(cfg["backup_dir"])
    private_dir(directory)
    with lock(directory / "backup.lock"), tempfile.TemporaryDirectory(prefix=".restore-", dir=directory) as tmp:
        work = Path(tmp)
        extracted = work / "extracted"
        manifest = verify_archive(archive, read_key(cfg), work, extracted)
        if not manifest.get("mysql"):
            raise BackupError("归档没有三库SQL；文件完整性请使用verify")
        # 测试凭证须人工预置于容器本层文件系统；拒绝宿主绑定挂载和root回退。
        container = target["container_id"]
        prefix = ["docker", "exec", "-i", container]
        local = ["--protocol=TCP", "--host=127.0.0.1", "--port=3306", "--user=aiops_restore_test"]
        # 演练容器客户端可能为8.0（无--no-login-paths），恢复命令不传login簇选项。
        client = ["mysql", "--defaults-file=/run/secrets/aiops-backup-test.cnf",
                  *local, "--batch", "--skip-column-names"]
        grants = work / "grants"
        run_file(prefix + client + ["-e", "SHOW GRANTS FOR CURRENT_USER"], grants)
        with grants.open() as stream:
            visible = any(re.search(r"GRANT (?:.*SHOW DATABASES.*|ALL PRIVILEGES) ON \*\.\* TO", line)
                          for line in stream)
        if not visible:
            raise BackupError("演练账户必须有全局SHOW DATABASES权限以证实空实例，禁止隐藏已有库")
        probe = work / "databases"
        run_file(prefix + client + ["-e", "SHOW DATABASES"], probe)
        with probe.open() as stream:
            names = {line.strip() for line in stream}
        if names - {"mysql", "sys", "information_schema", "performance_schema"}:
            raise BackupError("恢复目标不是空实例，拒绝覆盖任何既有业务库")
        validate_target(target)
        with (extracted / "mysql/all.sql").open("rb") as src:
            run_file(prefix + client + ["--binary-mode=1"], work / "import-result", src)
        dump = work / "restored.sql"
        run_file(prefix + ["mysqldump", "--defaults-file=/run/secrets/aiops-backup-test.cnf",
                          *local, *DUMP_OPTIONS,
                          "--databases", *manifest["mysql"]["databases"]], dump)
        actual = sql_stats(dump)
        expected = {k: manifest["mysql"][k] for k in ("databases", "tables", "rows")}
        if actual != expected:
            raise BackupError("恢复内容与归档manifest的逐表行数、内容及schema指纹不符")
        report = {"archive": archive.name, "files_verified": True, "mysql_content_verified": True,
                  "runtime_acceptance": "blocked", "temporal_restore": "未连接正式Server，禁止报通过",
                  "business_api": "未验证", "target_preserved": True}
        write_json(directory / "restore-status.json", report)
        return report
