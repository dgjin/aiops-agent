#!/usr/bin/env python3
"""批次H：显式配置的加密备份、只读预检和独立测试实例恢复；不读取dotenv。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiops_agent import backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="显式600权限JSON；不自动读取.env")
    parser.add_argument("--keep", type=int, default=7, help="成功日备份保留份数，至少1")
    parser.add_argument("--protected", action="store_true", help="永久保护本次归档，不纳入日常清理")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--dry-run", action="store_true", help="只检查配置元数据；不读源文件正文或连接服务")
    actions.add_argument("--readiness", action="store_true", help="输出备份状态及未实测门禁；未验收返回2")
    actions.add_argument("--status", action="store_true", help="输出26小时过期状态；过期或最近失败返回2")
    actions.add_argument("--list", action="store_true", help="只列当前模式归档文件名；不宣称已校验")
    actions.add_argument("--verify", type=Path, help="解密认证并逐文件校验，不恢复数据库")
    actions.add_argument("--restore-test", type=Path, help="显式归档路径；只允许独立空白测试MySQL实例")
    parser.add_argument("--test-target", type=Path, help="独立容器ID、卷、端口与标记的600权限JSON")
    parser.add_argument("--approve-test-import", action="store_true", help="明确批准向已经隔离的空测试实例导入")
    args = parser.parse_args(argv)
    try:
        if args.keep < 1:
            raise backup.BackupError("--keep必须大于等于1")
        cfg = backup.load_config(args.config, recovery=bool(args.verify or args.restore_test or args.list or args.status))
        directory = Path(cfg["backup_dir"])
        code = 0
        if args.dry_run or args.readiness:
            result = backup.readiness(cfg)
            result["dry_run"] = bool(args.dry_run)
            code = 2 if args.readiness else 0
        elif args.status:
            result = backup.status(directory, current_mode=cfg["mode"])
            code = 2 if result["stale"] or result.get("last_failure") else 0
        elif args.list:
            backup.plain(directory)
            result = {"archives": sorted(p.name for p in directory.glob(f"aiops-{cfg['mode']}-*.aes")
                                         if not p.is_symlink() and p.is_file()), "verified": False}
        elif args.verify:
            backup.private_dir(directory)
            with backup.lock(directory / "backup.lock"), tempfile.TemporaryDirectory(
                    prefix=".check-", dir=directory) as tmp:
                manifest = backup.verify_archive(backup.plain(args.verify),
                                                  backup.read_key(cfg), Path(tmp))
            result = {"files_verified": True, "files": len(manifest["files"]),
                      "runtime_acceptance": "blocked", "temporal_restore": "未验证"}
        elif args.restore_test:
            if not args.test_target or not args.approve_test_import:
                raise backup.BackupError("恢复必须显式提供--test-target并批准--approve-test-import")
            result = backup.restore_test(cfg, backup.plain(args.restore_test),
                                         backup.read_json(backup.plain(args.test_target)))
            code = 2
        else:
            archive = backup.create_backup(cfg, args.keep, args.protected)
            result = {"archive": str(archive), "archive_verified": True,
                      "runtime_acceptance": "blocked", "offsite": "未配置（disabled）"}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return code
    except Exception as exc:
        reason = str(exc) if isinstance(exc, backup.BackupError) else "操作失败；底层错误已隐藏，请检查配置和权限"
        print(json.dumps({"error": reason, "runtime_acceptance": "blocked"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
