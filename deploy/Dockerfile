# AIOps Agent 生产镜像（worker / BFF / webhook 三入口共用；K8s manifests 引用 aiops-agent:1.0.0）
#
# 构建（在仓库根 aiops-agent/ 下执行）：
#   docker build -f deploy/Dockerfile -t aiops-agent:1.0.0 .
#   docker push <registry>/aiops-agent:1.0.0    # 推送到集群可达的 registry（K8s 中替换镜像地址）
# 网络受限环境可换依赖源：
#   docker build -f deploy/Dockerfile --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t aiops-agent:1.0.0 .
#
# 运行角色（K8s 通过 command 覆盖；本地验证示例）：
#   worker : docker run --rm aiops-agent:1.0.0
#   BFF    : docker run --rm -p 8600:8600 aiops-agent:1.0.0 \
#              python -m uvicorn bff.app:app --host 0.0.0.0 --port 8600
#   webhook: docker run --rm -p 8099:8099 aiops-agent:1.0.0 \
#              python alert_webhook.py --host 0.0.0.0 --port 8099
#
# 说明：
# - 生产模式（默认 production）需注入 AIOPS_DATABASE_URL / AIOPS_CONSOLE_AUTH_TOKENS 等
#   （见 deploy/k8s/secret.example.yaml）；本地演示请显式 AIOPS_MODE=demo；
# - demo-app/ 为当前沙箱测试默认仓库（生产请挂载/替换为被监控应用真实代码仓库）。

# ---------- 阶段 1：构建前端控制台（web/dist 由 BFF 静态托管） ----------
FROM node:20-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------- 阶段 2：Python 运行时 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AIOPS_DATA_ROOT=/var/lib/aiops

WORKDIR /app

# git：Qoder 修复引擎在隔离工作区建立基线提交；libssl3：PyMySQL caching_sha2 认证依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libssl3 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# 依赖源：网络受限环境可覆盖（如 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

COPY aiops_agent/ ./aiops_agent/
COPY bff/ ./bff/
COPY alert_webhook.py demo_cli.py index_codebase.py ship_app_logs.py ./
COPY release-gate-policy.yaml demo-policy.yaml ./
COPY demo-app/ ./demo-app/
COPY --from=web /build/dist ./web/dist

# 非 root 运行；数据目录（审计兜底文件 / 沙箱工作区 / Qoder 工作区；生产经 PVC 挂载）
RUN useradd --create-home --uid 10001 aiops \
    && mkdir -p /var/lib/aiops \
    && chown -R aiops:aiops /app /var/lib/aiops
USER aiops

EXPOSE 8600 8099 9090
CMD ["python", "-m", "aiops_agent.worker"]
