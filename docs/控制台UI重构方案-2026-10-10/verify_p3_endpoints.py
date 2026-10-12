"""P3 聚合端点验收（2026-10-12）：/api/systems/overview 与 /api/systems/{app_id}/timeline。

用法：AIOPS_VERIFY_USER=... AIOPS_VERIFY_PASSWORD=... \
        .venv/bin/python "docs/控制台UI重构方案-2026-10-10/verify_p3_endpoints.py" [base_url]
- 优先读 AIOPS_VERIFY_TOKEN；否则用 AIOPS_VERIFY_USER/PASSWORD 走 /api/auth/login；
- 打印两端点关键字段摘要 + 基本断言，退出码 0/1（验收留痕）。
"""

import json
import os
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8600"


def token() -> str:
    tok = os.environ.get("AIOPS_VERIFY_TOKEN", "")
    if tok:
        return tok
    user = os.environ.get("AIOPS_VERIFY_USER", "")
    password = os.environ.get("AIOPS_VERIFY_PASSWORD", "")
    if not user or not password:
        raise SystemExit("需要 AIOPS_VERIFY_TOKEN 或 AIOPS_VERIFY_USER/AIOPS_VERIFY_PASSWORD")
    payload = json.dumps({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        BASE + "/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["token"]


def get(path: str, tok: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    tok = token()
    ov = get("/api/systems/overview", tok)
    print("== /api/systems/overview ==")
    print("counts:", ov["counts"])
    print("apps:", len(ov["apps"]), "| running:", len(ov["running"]), "| unmanaged:", ov["unmanaged"])
    print("terminal_dist_7d:", ov["terminal_dist_7d"])
    assert {"counts", "apps", "unmanaged", "running", "terminal_dist_7d"} <= set(ov)

    apps = ov["apps"]
    if not apps:
        raise SystemExit("no monitored apps; timeline check skipped")
    app = apps[0]
    tl = get(f"/api/systems/{app['id']}/timeline?flows=5", tok)
    print(f"== /api/systems/{app['id']}/timeline（service={tl['service']}）==")
    for group in tl["groups"]:
        print(
            f"  [{group['exec_status']:>10}] {group['wf_id']} "
            f"stage={group['stage']} events={len(group['events'])}"
        )
        for event in group["events"][:3]:
            print(f"      {event['seq']}. {event['text'][:72]}  at={event['at']}")
    assert {"app_id", "service", "groups"} <= set(tl)
    print("P3 endpoints OK")


if __name__ == "__main__":
    main()
