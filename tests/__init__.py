"""测试包隔离钩子：unittest 体系下强制 demo 模式（pytest 另由 conftest.py 同款覆盖）。

``tests/conftest.py`` 只对 pytest 生效；README 约定的 unittest 运行方式
（``python -m unittest tests.xxx``、``discover -s tests -t .``）会先导入本包
（``tests/__init__.py`` 先于一切测试模块执行），从而在任何 ``aiops_agent``
导入（含 config.py 的 .env 加载，override=False）之前把 ``AIOPS_MODE`` 固定为
demo，杜绝测试进程经 production ``.env`` 直连生产 MySQL。

实测教训（2026-10-04）：``discover -s tests``（不带 ``-t .``）会把测试按顶层
模块导入并跳过本文件——当时 ``.env=production`` 生效，测试直写生产库
（monitored_apps / console_tokens 被测试数据污染，并连带触发 prober 对
http://a.test/ 等测试条目告警）。务必使用带 ``-t .`` 的形式（见 README）。
"""

import os
from pathlib import Path
import re
import sys
import tempfile
import weakref
from urllib.parse import unquote, urlsplit

_SYSTEM_TEMP = Path("/tmp").resolve()
sys.dont_write_bytecode = True
_BOUND_TEST_SOCKETS = weakref.WeakSet()
_INHERITED_ROOT = os.environ.get("AIOPS_DATA_ROOT")
_PROTECTED_ROOTS = [Path(_INHERITED_ROOT).expanduser().resolve()] if _INHERITED_ROOT else []

# 入口必须先于应用导入；不尝试修复已经缓存了生产配置/引擎的进程。
if any(name == "aiops_agent.config" or name == "aiops_agent.db" or name.startswith("bff.")
       for name in sys.modules):
    raise RuntimeError("测试隔离必须先于应用导入；使用 unittest discover -s tests -t .")

REPO_ROOT = Path(__file__).resolve().parents[1]
_PROTECTED_ROOTS.append(REPO_ROOT / "data")
# 不采信继承的 TMPDIR/HOME/PATH（可能指向生产根或携带工具凭证）。
TEST_ROOT = Path(tempfile.mkdtemp(prefix="aiops-unit-", dir="/tmp")).resolve()
TEST_ROOT.chmod(0o700)
os.environ.clear()
os.environ.update({
    "PATH": f"{Path(sys.executable).parent}:/opt/homebrew/opt/openssl@3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": str(TEST_ROOT), "TMPDIR": str(TEST_ROOT),
    "LANG": "C.UTF-8", "PYTHON_DOTENV_DISABLED": "1",
    "AIOPS_MODE": "demo", "AIOPS_DATABASE_URL": "",
    "AIOPS_DATA_ROOT": str(TEST_ROOT / "data"),
    "AIOPS_CONSOLE_TOKEN_FILE": str(TEST_ROOT / "console_tokens.json"),
    "AIOPS_USERS_FILE": str(TEST_ROOT / "console_users.json"),
    "AIOPS_SESSIONS_FILE": str(TEST_ROOT / "console_sessions.json"),
    "AIOPS_MONITORED_APPS_PATH": str(TEST_ROOT / "monitored_apps.json"),
    "AIOPS_SANDBOX_ROOT": str(TEST_ROOT / "sandbox"),
    "AIOPS_QODER_ROOT": str(TEST_ROOT / "qoder"),
    "DOCKER_HOST": "unix://" + str(TEST_ROOT / "disabled-docker.sock"),
    "DOCKER_CONFIG": str(TEST_ROOT / "docker"),
})
tempfile.tempdir = str(TEST_ROOT)

# 老版本 python-dotenv 不识别 PYTHON_DOTENV_DISABLED；同时封住显式调用。
try:
    import dotenv
    import dotenv.main

    def _no_dotenv(*args, **kwargs):
        return False

    dotenv.load_dotenv = dotenv.main.load_dotenv = _no_dotenv
except ImportError:
    pass

# macOS 系统代理（_scproxy）不经环境变量：env 清空后 urllib 仍会走真实代理
# （实测 127.0.0.1:7890），令本地假服务请求被 connect 门禁拒绝。测试进程一律无代理。
import urllib.request as _urllib_request  # noqa: E402 - 必须在环境清空后

_urllib_request.getproxies = lambda: {}
_urllib_request._opener = None  # 默认 opener 若已提前构建（携代理），强制重建


def _static_compose_only(executable, argv, cwd, env):
    """仅给E/F的离线模板合并留窄门；精确命令、固定源文件、临时HOME/env。"""
    if not isinstance(argv, (list, tuple)) or not isinstance(env, dict) or str(cwd) != str(REPO_ROOT):
        return False
    if executable == "docker" and len(argv) > 1 and argv[1] == "compose":
        flags = list(argv[2:])
    elif executable == "docker-compose":
        flags = list(argv[1:])
    else:
        return False
    if (len(flags) != 13 or flags[0] != "--project-name" or flags[2] != "--env-file"
            or flags[4:9:2] != ["-f", "-f", "--profile"]
            or flags[9:] != ["*", "config", "--format", "json"]
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", flags[1])):
        return False
    if [str(flags[5]), str(flags[7])] != [str(REPO_ROOT / "docker-compose.yml"),
                                        str(REPO_ROOT / "docker-compose.production.yml")]:
        return False
    path = Path(flags[3])
    return (path.is_absolute() and path.resolve().is_relative_to(TEST_ROOT) and not path.is_symlink()
            and path.is_file() and path.stat().st_nlink == 1
            and set(env) <= {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "LC_ALL", "COMPOSE_DISABLE_ENV_FILE"}
            and env.get("HOME") == str(TEST_ROOT) and env.get("COMPOSE_DISABLE_ENV_FILE") == "1"
            and env.get("DOCKER_HOST") == "unix://" + str(TEST_ROOT / "disabled-docker.sock")
            and env.get("DOCKER_CONFIG") == str(TEST_ROOT / "docker"))


def _production_guard(event, args):
    """第二道门：固定 data 路径不能因遗漏 patch 而接触真实状态。

    这不是恶意代码沙箱。隔离集成测试另起进程；本入口仅允许严格的离线Compose合并。
    """
    if event in {"open", "os.mkdir", "os.remove", "os.rmdir", "os.rename"}:
        paths = args[:2] if event == "os.rename" else args[:1]
        for value in paths:
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            path = Path(os.fsdecode(value)).absolute().resolve()
            if path == REPO_ROOT / ".env" or any(path.is_relative_to(root) for root in _PROTECTED_ROOTS):
                # 已提交的只读评测夹具不是运行数据；写入仍拒绝。
                fixture = path.is_relative_to(REPO_ROOT / "data" / "fix_eval_repo") or path in {
                    REPO_ROOT / "data" / name for name in ("fix_eval_samples.json", "fix_eval_report.json",
                                                         "triage_eval_samples.json", "triage_eval_report.json",
                                                         "historical_tickets.json")
                }
                writing = event != "open" or (isinstance(args[1], str) and any(c in args[1] for c in "wax+"))
                if event == "open" and isinstance(args[2], int):
                    writing = writing or bool(args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                if not fixture or writing:
                    raise PermissionError("测试禁止访问真实数据根或 .env；请注入临时夹具")
    elif event == "sqlite3.connect":
        database = os.fsdecode(args[0])
        if database not in {"", ":memory:"}:
            if database.startswith("file:"):
                parsed = urlsplit(database)
                if parsed.netloc:
                    raise PermissionError("测试SQLite拒绝远程URI")
                database = unquote(parsed.path)
            path = Path(database).resolve()
            in_smoke = (path != _SYSTEM_TEMP and path.is_relative_to(_SYSTEM_TEMP) and
                        path.relative_to(_SYSTEM_TEMP).parts[0].startswith("aiops-smoke-"))
            if (not (path.is_relative_to(TEST_ROOT) or in_smoke)
                    or any(path.is_relative_to(root) for root in _PROTECTED_ROOTS)):
                raise PermissionError("SQLite必须使用临时测试库，禁止真实数据库文件")
    elif event == "socket.bind":
        sock, address = args
        if isinstance(address, tuple):
            if address[0] not in {"127.0.0.1", "::1"} or (address[1] != 0 and address[1] < 20000):
                raise PermissionError("测试只允许独立 loopback 测试端口")
            _BOUND_TEST_SOCKETS.add(sock)
    elif event == "socket.connect":
        # 只信"本进程 bind 过的 (ip, port) 精确匹配"。端口范围不能再卡下限：
        # 本机 port-0 分配的临时端口可能落在 20000 以下（实测 7890），那仍是自建服务。
        address = args[1]
        allowed = False
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            for sock in list(_BOUND_TEST_SOCKETS):
                try:
                    allowed = allowed or sock.getsockname()[:2] == address[:2]
                except OSError:
                    pass
        if not allowed:
            raise PermissionError("测试只能连接本进程创建的loopback服务；禁止真实服务/Unix套接字")
    elif event == "subprocess.Popen":
        executable = Path(os.fsdecode(args[0])).name
        if executable in {"docker", "docker-compose", "colima", "launchctl", "kubectl", "qoder", "ssh"}:
            if not _static_compose_only(executable, args[1], args[2], args[3]):
                raise PermissionError("单元测试禁止调用真实部署/凭证/容器工具")


sys.addaudithook(_production_guard)
