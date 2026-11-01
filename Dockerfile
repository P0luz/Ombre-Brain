# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build:
#   docker build -t ombre-brain .
# 本地运行（最小必填项）:
#   docker run \
#     -e OMBRE_COMPRESS_API_KEY=your-llm-key \
#     -e OMBRE_EMBED_API_KEY=your-gemini-key \
#     -e OMBRE_DASHBOARD_PASSWORD=xxx \
#     -p 18001:8000 ombre-brain          # 对外 18001 → 容器内 8000
# 推荐用 deploy/docker-compose.yml（开发）或 deploy/docker-compose.user.yml（用户）启动。
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# cloudflared（用于 Dashboard 的 Tunnel 一键管理）。
# 用镜像自带的 python 直接从 GitHub Releases 下载（带重试），不装 curl、不跑
# apt-get update —— 从根上避开 Debian 镜像源间歇性 502 导致的构建失败（用户反馈 #3）。
# 不需要 Tunnel 的用户可 `docker build --build-arg INSTALL_CLOUDFLARED=0 ...` 完全跳过。
ARG INSTALL_CLOUDFLARED=1
COPY deploy/fetch_cloudflared.py /tmp/fetch_cloudflared.py
RUN set -eu; \
    if [ "$INSTALL_CLOUDFLARED" = "1" ]; then \
        python /tmp/fetch_cloudflared.py /usr/local/bin/cloudflared; \
        chmod +x /usr/local/bin/cloudflared; \
    else \
        echo "[build] INSTALL_CLOUDFLARED=0 → 跳过 cloudflared（Tunnel 一键管理将不可用）"; \
    fi; \
    rm -f /tmp/fetch_cloudflared.py

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
# 可选 pip 镜像源：受限网络（宿主代理掐断 PyPI）时传
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
# 默认留空 → 官方 PyPI，行为不变。
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
# GitHub 源码归档会排除开发者使用的宽松 requirements.txt；镜像安装只依赖
# 带 hash 的权威生产锁，因此 clone 与归档构建统一只复制该文件。
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
        ${PIP_INDEX_URL:+-i "$PIP_INDEX_URL"} \
        ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
        --require-hashes -r requirements.lock.txt

# Copy project files / 复制项目文件
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY VERSION ./VERSION
COPY config.example.yaml ./config.default.yaml
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 面向用户的说明文档：Docker 用户本地无源码、镜像此前也不含这些文件，
# 导致「给 Claude 的使用指南」（README）指向的 docs/CLAUDE_PROMPT.md 拿不到，
# 出现「服务装完了但模型没拿到使用约定」的 onboarding 断点。内部设计稿
# （docs/superpowers、docs/secrets 等）不在此列，仍被 .dockerignore 挡在外面。
COPY docs/CLAUDE_PROMPT.md docs/ENVIRONMENT_VARIABLES.md docs/INTERNALS.md docs/MULTI_OWNER.md docs/OPERATIONS.md ./docs/
# ADR 与 preflight CLI：两项系统诊断（adr_requirements / preflight_cli_diagnostics）
# 在运行时目录下分别读 docs/adr/ 与 tools/vnext_preflight.py。镜像此前不含这两处，
# 诊断在任何 Docker 部署上都报 not found / missing_files。
COPY docs/adr/ ./docs/adr/
COPY tools/ ./tools/
COPY kernel/ ./kernel/
COPY README.md ./README.md
COPY CHANGELOG.md ./CHANGELOG.md

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
# 容器内固定监听 8000；对外通过 host 端口映射 18001:8000 暴露（保持 Cloudflare
# ingress 指向 :8000 不变）。裸机（非容器）不读此 ENV，走 server.py 默认 18001。
ENV OMBRE_PORT=8000
ENV OMBRE_BUCKETS_DIR=/app/buckets
# config 默认落在持久卷 /app/buckets 里，而不是镜像可写层 /app/config.yaml。
# 关键：很多 PaaS（Zeabur / 部分 Render 配置等）用**只读根文件系统**，只有挂载的卷可写——
# 这时 entrypoint 往 /app/config.yaml 写默认配置会 "Read-only file system" 失败 → FATAL →
# 无限崩溃重启（本地 root + 可写 /app 复现不出，平台上才炸）。放到 /app/buckets 既避开只读根，
# 又让 Dashboard 改的 key 落在卷上、重启/重部署不丢。VPS（deploy/docker-compose.yml）显式覆盖回
# /app/config.yaml 保持原有文件挂载不变。
ENV OMBRE_CONFIG_PATH=/app/buckets/config.yaml
# Embedding 使用 API 后端（Gemini）
# 必须通过运行时 -e 或 docker-compose environment 传入 OMBRE_EMBED_API_KEY
ENV OMBRE_EMBED_BACKEND=api

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
