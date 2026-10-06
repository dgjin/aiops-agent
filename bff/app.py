"""AIOps 运维控制台 BFF：应用组装入口（设计方案 5.1–5.6、6.1–6.4）。

接口职责：
- 读侧：overview / flows / flow detail / result / approvals / window / audit / system；
- 写侧：approval / second-approval / deploy-command / queue-patch
  （前置 stage 校验 → Temporal signal → 操作审计，前端不可绕过）；
- 静态托管：web/dist 构建产物（同域提供，SPA fallback）。

模块结构（批次 6 按域拆分）：
- ``bff/deps.py``：共享基础（网关单例 gw / 统一响应 ok / ApiError）；
- ``bff/middleware.py``：缓存策略 / 集中式鉴权（限速 + kill switch）/ 安全头 / 指标；
- ``bff/routes/``：按域路由（health / flows / approvals / audit / tokens / system / monitored_apps）；
- 本模块只保留：lifespan、异常处理器、中间件与路由挂载、静态托管、可选 tracing。

运行（工程根目录）：
    .venv/bin/uvicorn bff.app:app --host 127.0.0.1 --port 8600
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiops_agent import tracing

from . import auth, escalations
from .deps import ApiError, WEB_DIST, gw, log, ok
from .middleware import register_middlewares
from .middleware import required_role as required_role  # noqa: F401 - 兼容测试引用（bff.app.required_role）
from .routes import include_routers


def _rotation_check_seconds() -> int:
    """自动轮换检查周期（秒）：到期检查很轻，默认 5 分钟一次足够。"""
    try:
        return max(1, int(os.environ.get("AIOPS_TOKEN_ROTATE_CHECK_SECONDS", "300")))
    except ValueError:
        return 300


async def _rotation_loop() -> None:
    """后台令牌自动轮换：到期即轮换（间隔 / 宽限期可配，间隔 0 表示关闭）。

    轮换不中断在用会话——旧令牌转 ``previous`` 并在**宽限期**内继续有效，
    因此自动轮换不会把运维锁在门外。
    """
    while True:
        try:
            status = auth.rotation_status()
            if status["due"]:
                result = auth.rotate(reason="auto")
                log.warning(
                    "[auth] 令牌已自动轮换：%d 个身份，宽限 %ss（旧令牌宽限期内仍可用）",
                    len(result["new_tokens"]),
                    result["grace_seconds"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 轮换失败不影响服务
            log.warning("[auth] 自动轮换检查失败：%s", exc)
        await asyncio.sleep(_rotation_check_seconds())


def _escalation_sweep_seconds() -> int:
    """转人工待办 SLA 巡检周期（秒）：单次巡检很轻，默认 60 秒一次。"""
    try:
        return max(5, int(os.environ.get("AIOPS_ESCALATION_SWEEP_SECONDS", "60")))
    except ValueError:
        return 60


async def _escalation_loop() -> None:
    """转人工待办 SLA 巡检（P1-3）：超时未处置则再升级一次。

    ``sweep_due`` 为同步（读存储 + 发通知），放线程池避免阻塞事件循环；
    每单只升级一次（``re_escalated`` 打标），巡检失败不影响控制台服务。
    """
    while True:
        try:
            escalated = await asyncio.to_thread(escalations.sweep_due)
            if escalated:
                log.warning(
                    "[escalations] %d 个待办超时未处置，已再升级（SLA %s 分钟）",
                    len(escalated),
                    escalations.sla_minutes(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 巡检失败不影响服务
            log.warning("[escalations] SLA 巡检失败：%s", exc)
        await asyncio.sleep(_escalation_sweep_seconds())


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await gw.client()  # 预热连接；失败不阻塞启动（接口按需重连，降级为只读提示）
    except Exception:  # noqa: BLE001 - Temporal 暂不可达时仍允许启动
        pass
    rotation_task = asyncio.create_task(_rotation_loop())
    escalation_task = asyncio.create_task(_escalation_loop())
    try:
        yield
    finally:
        rotation_task.cancel()
        escalation_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rotation_task
        with contextlib.suppress(asyncio.CancelledError):
            await escalation_task


app = FastAPI(title="AIOps Console BFF", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ApiError)
async def _api_error_handler(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=ok({"error": exc.message}))


# 注册顺序即洋葱顺序（metrics → security → auth → cache → 路由）
register_middlewares(app)
# 业务域路由（顺序即注册顺序；SPA catch-all 在下方最后注册）
include_routers(app)


# ----------------------------------------------------------------------
# 静态托管（web/dist 存在时启用；SPA fallback）
# ----------------------------------------------------------------------

if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    if WEB_DIST.is_dir():
        dist_root = WEB_DIST.resolve()
        candidate = (dist_root / full_path).resolve() if full_path else dist_root
        if full_path and candidate.is_file() and candidate.is_relative_to(dist_root):
            # 非 /assets 前缀的静态文件（如 favicon）：内容可能变化，不长期缓存
            return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
        index = dist_root / "index.html"
        if index.is_file():
            # SPA 入口**绝不缓存**：否则重新构建后浏览器仍加载旧包（引用旧哈希），
            # 表现为"页面行为与新代码不符"，且只能靠硬刷新恢复。
            return FileResponse(index, headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail="前端未构建（先在 web/ 执行 npm run build）或接口不存在")


# ----------------------------------------------------------------------
# OpenTelemetry 分布式追踪（可选，P2-04）：AIOPS_OTEL_ENDPOINT 未配置则 no-op
# ----------------------------------------------------------------------

tracing.setup_tracing("aiops-bff", instrument_fastapi_app=app)
