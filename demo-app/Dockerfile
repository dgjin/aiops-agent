# demo-app 服务镜像（WP7 演示）：金丝雀发布的真实制品。
# 构建上下文 = 已应用补丁的 demo-app 工作区（data/sandbox/<patch_id>/）。
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1

COPY . .

EXPOSE 8000
CMD ["python", "http_server.py"]
