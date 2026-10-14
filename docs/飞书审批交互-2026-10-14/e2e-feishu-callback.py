#!/usr/bin/env python3
"""e2e 验证：需求流程 → WAIT_APPROVAL → 模拟飞书卡片回调 approve → 链路验证（c8）。

链路覆盖：
1. admin 登录（会话令牌）→ 创建需求分析 → 轮询分析完成（Ollama）；
2. 批准 → 需求修复流程 aiops-req-ra-app-d42214-{entry}-v1；
3. 轮询至 WAIT_APPROVAL：校验审批卡片按钮 wf_id == 真实流程 ID（c2 修复核心）；
4. 模拟飞书 v2 card.action.trigger 回调（operator=demo-ops，operator 角色）
   → 断言 toast success + card type=raw（处置结果卡）；
5. 校验流程 approval=approve、stage 推进到 NOTIFYING（闸门 3）→ 二次回调防重复；
6. deploy-command cancel 收尾（NOTIFYING 取消发布 → CANCELLED，不实际发布）。

运行（在仓库根；需先 export AIOPS_VERIFY_TOKEN，与 BFF 的 AIOPS_FEISHU_VERIFY_TOKEN 同值）：
    .venv/bin/python docs/飞书审批交互-2026-10-14/e2e-feishu-callback.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("AIOPS_E2E_BASE", "http://127.0.0.1:8600")
VERIFY_TOKEN = os.environ.get("AIOPS_VERIFY_TOKEN") or os.environ.get("AIOPS_FEISHU_VERIFY_TOKEN") or ""
ADMIN_USER = os.environ.get("AIOPS_E2E_USERNAME", "admin")
ADMIN_PASS = os.environ.get("AIOPS_E2E_PASSWORD", "admin123")
if not VERIFY_TOKEN:
    sys.exit("缺少 AIOPS_VERIFY_TOKEN（与 BFF 的 AIOPS_FEISHU_VERIFY_TOKEN 同值）：先 export 再运行")
APP_ID = "app-d42214"
ENTRY_ID = "e2e-cb-1"
ROOT = Path(__file__).resolve().parents[2]
NOTIFY_DIR = ROOT / "data" / "notify"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg: str) -> None:
    log("FAIL: " + msg)
    sys.exit(1)


def need(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)
    log("PASS: " + msg)


def call(method: str, path: str, body: dict | None = None, token: str | None = None,
         timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}


def poll(fn, desc: str, timeout: int = 900, interval: int = 5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    fail(f"轮询超时（{timeout}s）：{desc}")


# ---------- 1. 登录 ----------
st, r = call("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASS})
need(st == 200 and bool(r.get("token")), f"admin 登录（HTTP {st}）：{json.dumps(r, ensure_ascii=False)[:200]}")
token = r["token"]
log(f"会话令牌 user={r.get('user')} role={r.get('role')}")

# ---------- 2. 创建需求分析 ----------
st, r = call("POST", "/api/requirements/analyses", {
    "app_id": APP_ID,
    "entry": {
        "id": ENTRY_ID,
        "kind": "feature",
        "title": "审批回调链路端到端验证（自动化）",
        "content": "验证飞书卡片回调网关：管理员点击「批准」后流程应立即生效并回执处置结果。",
        "status": "BASELINED",
        "priority": "P2",
        "submitter": "e2e",
    },
}, token=token)
need(st == 200 and bool(r.get("analysis")), f"创建需求分析 {ENTRY_ID}（HTTP {st}）：{json.dumps(r, ensure_ascii=False)[:300]}")
analysis_id = r["analysis"]["id"]
log(f"analysis_id={analysis_id} started={r.get('started')}")

# ---------- 3. 轮询分析完成 ----------
def analysis_done():
    st, r = call("GET", f"/api/requirements/analyses/{analysis_id}", token=token)
    if st != 200:
        return None
    a = r["analysis"]
    if a.get("status") == "analyzed":
        latest = (a.get("versions") or [{}])[-1]
        if latest.get("degraded"):
            fail(f"分析降级（degraded）：{latest.get('reason')}")
        return a
    return None

a = poll(analysis_done, "需求分析完成（analyzed）", timeout=1500, interval=10)
log(f"分析完成：version={a.get('current_version')} status={a.get('status')}")

# ---------- 4. 批准 → 启动修复流程 ----------
st, r = call("POST", f"/api/requirements/analyses/{analysis_id}/approve", {}, token=token)
need(st == 200 and bool(r.get("wf_id")), f"批准启动修复流程（HTTP {st}）：{json.dumps(r, ensure_ascii=False)[:300]}")
wf_id = r["wf_id"]
log(f"wf_id={wf_id}")

# ---------- 5. 轮询至 WAIT_APPROVAL ----------
def flow_detail():
    st, r = call("GET", f"/api/flows/{wf_id}", token=token)
    return r if st == 200 else None

def wait_approval():
    d = flow_detail()
    if d and (d.get("status") or {}).get("stage") == "WAIT_APPROVAL":
        return d
    return None

d = poll(wait_approval, "流程到达 WAIT_APPROVAL（FIXING → TESTING → 闸门 2）", timeout=1500, interval=5)
status = d["status"]
patch_id = status.get("patch_id")
need(bool(patch_id), f"补丁已生成：patch_id={patch_id}")

# ---------- 6. 校验审批卡片按钮载荷（c2 修复核心） ----------
card_path = NOTIFY_DIR / f"approval-{patch_id}.json"
need(card_path.is_file(), f"审批卡片已落盘：{card_path.name}")
card = json.loads(card_path.read_text(encoding="utf-8"))
action_els = [e for e in card["card"]["elements"] if e.get("tag") == "action"]
need(bool(action_els), "卡片含按钮区（action）")
approve_btn = action_els[0]["actions"][0]
btn_wf = (approve_btn.get("value") or {}).get("wf_id")
need(btn_wf == wf_id, f"按钮 wf_id == 运行中真实流程 ID（{btn_wf}）")

# ---------- 7. 模拟飞书 v2 回调（第一次：approve） ----------
def v2_payload() -> dict:
    now_ms = str(int(time.time() * 1000))
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"e2e-{now_ms}",
            "event_type": "card.action.trigger",
            "create_time": now_ms,
            "token": VERIFY_TOKEN,
            "app_id": "cli_e2e",
            "tenant_key": "e2e-tenant",
        },
        "event": {
            "operator": {"open_id": "demo-ops", "user_id": "demo-ops"},
            "action": {
                "tag": "button",
                "value": {"wf_id": wf_id, "gate": "approval", "decision": "approve"},
            },
        },
    }

st, r = call("POST", "/api/im/card-callback", v2_payload())  # 无 Bearer（豁免通道）
need(st == 200, f"回调 HTTP 200（{st}）：{json.dumps(r, ensure_ascii=False)[:300]}")
toast = r.get("toast") or {}
need(toast.get("type") == "success", f"toast.success：{toast.get('content')}")
card_resp = r.get("card") or {}
need(card_resp.get("type") == "raw", "响应携带处置结果卡（card.type=raw）")
card_text = json.dumps(card_resp.get("data") or {}, ensure_ascii=False)
need("一级审批已批准" in card_text, f"结果卡正文含「一级审批已批准」：{card_text[:160]}")
need("green" in card_text, "结果卡 header 为绿色（批准）")

# ---------- 8. 校验审批信号生效 ----------
def approved_flow():
    d = flow_detail()
    if ((d or {}).get("status") or {}).get("approval") == "approve":
        return d
    return None

d = poll(approved_flow, "流程 approval=approve 生效", timeout=180, interval=3)
log(f"审批生效：stage={d['status'].get('stage')} approval={d['status'].get('approval')}")

def notifying():
    d = flow_detail()
    if ((d or {}).get("status") or {}).get("stage") == "NOTIFYING":
        return d
    return None

d = poll(notifying, "stage 推进到 NOTIFYING（闸门 3 公告期）", timeout=900, interval=5)
need(True, "闸门 3 公告期开始（NOTIFYING）——审批交互全链路贯通")

# ---------- 9. 二次回调（防重复提交） ----------
st, r = call("POST", "/api/im/card-callback", v2_payload())
need(st == 200, f"二次回调 HTTP 200（{st}）")
toast2 = r.get("toast") or {}
need(toast2.get("type") == "error", f"二次点击被拒：{toast2.get('content')}")
need("未执行" in (toast2.get("content") or ""), "二次点击提示「未执行：…」（防重复）")

# ---------- 10. cancel 收尾（避免真实发布） ----------
st, r = call("POST", f"/api/flows/{wf_id}/deploy-command", {"command": "cancel"}, token=token)
need(st == 200, f"cancel 信号已发出（HTTP {st}）：{json.dumps(r, ensure_ascii=False)[:200]}")

def cancelled():
    d = flow_detail()
    s = (d or {}).get("status") or {}
    if s.get("stage") == "CANCELLED":
        return d
    return None

d = poll(cancelled, "流程 CANCELLED 终态", timeout=300, interval=5)
log(f"终态：stage={d['status'].get('stage')} exec_status={d.get('exec_status')}")

log("=== E2E 全部通过（审批回调链路端到端验证完成）===")
print(json.dumps({
    "analysis_id": analysis_id,
    "wf_id": wf_id,
    "patch_id": patch_id,
    "final_stage": d["status"].get("stage"),
}, ensure_ascii=False, indent=2))
