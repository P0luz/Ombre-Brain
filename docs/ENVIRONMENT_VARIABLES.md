# Ombre Brain 全环境变量清单

本文是环境变量名称的唯一文档真源。代码、Docker、Zeabur 模板和 README 只能引用这里已经登记的名称；禁止静默改名。确需更名时，旧名必须保留兼容映射、弃用提示和迁移说明。

## 模型与向量

- `OMBRE_COMPRESS_API_KEY`：打标、脱水和合并模型密钥。
- `OMBRE_COMPRESS_BASE_URL`：OpenAI 兼容模型地址。
- `OMBRE_COMPRESS_MODEL`：打标模型名。
- `OMBRE_COMPRESS_FORMAT`：打标 API 格式。
- `OMBRE_COMPRESS_API_FORMAT`：`OMBRE_COMPRESS_FORMAT` 的旧兼容名。
- `OMBRE_COMPRESS_TIMEOUT_SECONDS`：打标请求超时秒数。
- `OMBRE_EMBED_API_KEY`：向量模型密钥。
- `OMBRE_EMBED_BASE_URL`：向量 API 地址。
- `OMBRE_EMBED_MODEL`：向量模型名。
- `OMBRE_EMBED_FORMAT`：向量 API 格式。
- `OMBRE_EMBED_TIMEOUT_SECONDS`：向量请求超时秒数。
- `OMBRE_EMBED_BACKEND`：向量后端，例如 `api` 或 `local`。
- `OMBRE_OLLAMA_URL`：本地 Ollama 地址。

## 存储、媒体与日志

- `OMBRE_VAULT_DIR`：推荐的数据根目录。
- `OMBRE_BUCKETS_DIR`：`OMBRE_VAULT_DIR` 的旧兼容名。
- `OMBRE_MEDIA_DIR`：永久媒体目录；默认 `<数据根目录>/_media`。
- `OMBRE_MEDIA_MAX_BYTES`：单个媒体文件最大字节数，默认 25 MiB。
- `OMBRE_CONFIG_PATH`：持久配置文件路径。
- `OMBRE_CODE_DIR`：容器中持久运行代码目录。
- `OMBRE_LOG_DIR`：日志目录。
- `OMBRE_LOG_FILE`：日志文件路径。
- `OMBRE_EXTERNAL_CHANGE_POLL_SECONDS`：外部 Markdown 变动轮询间隔。

## HTTP、MCP 与鉴权

- `OMBRE_TRANSPORT`：`stdio` 或 `streamable-http`（legacy SSE 传输已于 2026-08-09 下线）。
- `OMBRE_PORT`：容器或裸机监听端口。
- `OMBRE_BIND_HOST`：进程实际监听地址；容器/PaaS 通常需要 `0.0.0.0`，裸机仅限本机访问时应设为 `127.0.0.1`。
- `OMBRE_MCP_REQUIRE_AUTH`：是否要求 MCP 鉴权。
- `OMBRE_MCP_AUTH_MODE`：`oauth`、`token` 或 `hybrid`。`hybrid` 保留 OAuth 动态注册，同时让 `Authorization: Bearer` 也接受预置静态 Token；关闭鉴权仍由 `OMBRE_MCP_REQUIRE_AUTH=false` 控制。
- `OMBRE_MCP_TOKEN`：静态 Token / OAuth + 静态 Token 共存模式的预置密钥。
- `OMBRE_ALLOW_INSECURE_MCP`：Dashboard / 部署向导保存非回环免鉴权组合、以及内置 Tunnel 免鉴权启动时的高风险确认。直接设置 `OMBRE_MCP_REQUIRE_AUTH=false` 会按明确配置生效；该变量不再是启动期暗中改写鉴权开关的条件。
- `OMBRE_DASHBOARD_PASSWORD`：Dashboard 密码。部署到能被公网/远程访问的机器前建议直接设置此项——首次访问 Dashboard 时弹出的"设置密码"表单只信任本机回环连接（见 `OMBRE_SETUP_TOKEN`），提前设好这个变量能跳过那道限制，远程也能直接登录。
- `OMBRE_SETUP_TOKEN`：首次设置密码的远程覆盖口令。默认只有从 `127.0.0.1`/`localhost` 直连（且没有反向代理转发头）的请求才能调用 `/auth/setup`；设置此变量后，远程请求带上请求头 `X-Ombre-Setup-Token: <此变量的值>` 也可以完成首次设置（Dashboard 网页本身不发这个头，需要手动 `curl` 调用一次）。适合"已经部署到云服务器、还没来得及先设 `OMBRE_DASHBOARD_PASSWORD`"这种场景的补救；密码设置成功后这个 token 就不再需要。
- `OMBRE_DASHBOARD_SESSION_DAYS`：Dashboard 登录会话天数。
- `OMBRE_TRUSTED_PROXY_CIDRS`：直接连接 OB 的最后一跳可信反向代理 CIDR；不是公网客户端 IP 或域名，禁止使用 `0.0.0.0/0`。官方 Compose 模板会从 `.env` 透传该值，修改后需要重新创建容器。

## Tunnel、Hook 与 GitHub

- `OMBRE_GITHUB_TOKEN`：GitHub 备份或更新访问令牌。
- `OMBRE_HOOK_URL`：外部 Hook 地址。
- `OMBRE_HOOK_TOKEN`：Hook 鉴权令牌。
- `OMBRE_HOOK_SKIP`：跳过 Hook。
- `OMBRE_HOOK_ALLOW_PUBLIC`：允许公网 Hook 地址。
- `TUNNEL_EDGE`：cloudflared 边缘节点覆盖值。
- `TUNNEL_TRANSPORT_PROTOCOL`：cloudflared 传输协议。

## 更新与容器维护

- `OMBRE_ALLOW_CUSTOM_UPDATE_REPO`：允许自定义更新仓库。
- `OMBRE_ALLOW_UNTRUSTED_MIRROR`：允许未受信任镜像源。
- `OMBRE_UPDATE_ALLOW_PIP`：允许热更新在依赖清单变化时自动执行 `pip install`（安全加固 #2：默认关闭，自动装依赖会把"谁能点热更新"放大成任意 PyPI 包的执行面）。默认关闭时遇到依赖变化会在写入任何文件前直接拒绝更新（不会写文件再回滚）。Dashboard「热更新」面板里也有一个等效开关（写 `config.yaml` 的 `update.allow_pip_install`，立即生效不需要重启），效果与设置本变量相同，二者取其一即可，不用两个都配。
- `OMBRE_FORCE_CODE_RESEED`：下次启动强制从镜像重播代码；使用后应移除。
- `OMBRE_IMAGE_ROOT`：镜像内置代码根目录。
- `OMBRE_BOOTSTRAP_ONLY`：仅执行启动引导和诊断。
- `OMBRE_DOCKER_INTEGRATION_URL`：Docker 集成服务地址。
- `OMBRE_DOCKER_WEB_BASE_URL`：Docker Web 基地址。

## 部署编排与多所有者

- `OMBRE_BIND_ADDRESS`：Compose 宿主机对外绑定地址，默认 `127.0.0.1`；官方模板同时把该值传入容器，供 MCP 安全门禁判断真实宿主边界。
- `OMBRE_HOST_PORT`：Compose 宿主机端口。
- `OMBRE_HOST_VAULT_DIR`：Compose 宿主机数据目录。
- `OMBRE_CONTAINER_NAME`：目标容器名。
- `OMBRE_OWNER_NAME`：当前所有者名。
- `OMBRE_OWNER_COUNT`：多所有者实例数。
- `OMBRE_MING_VAULT_DIR`、`OMBRE_HONG_VAULT_DIR`：示例多所有者数据目录。
- `OMBRE_MING_PASSWORD`、`OMBRE_HONG_PASSWORD`：示例多所有者密码。
- `OMBRE_MING_MCP_TOKEN`、`OMBRE_HONG_MCP_TOKEN`：多所有者 Compose 中每个实例各自的静态 MCP Token；不要让多个 owner 共用同一密钥。
- `AI_NAME`：AI 显示名称（letter 署名、prompt、面向用户的文案）。
  **优先级：`config.yaml` 的 `ai_name` → 本变量 → 回退 `"AI"`。**
  自 3.0.0 起首选写进 `config.yaml`：它随 vault 一起持久化，Docker 下容器
  重建/重启都不会丢；本变量则依赖 compose 逐个透传。`config.yaml` 里默认留空，
  表示「没配」，此时行为与旧版一致。
  > 3.0.0 之前 `deploy/docker-compose.yml` 并未透传本变量，导致 Docker 用户
  > 实际设不上 AI 显示名，带锁 Letter 因取不到实际关系名而无法创建；现已补上透传。

## v1.x 兼容变量

这些旧名仍然生效，但新部署应使用右侧正式名称：

- `OMBRE_API_KEY` → `OMBRE_COMPRESS_API_KEY`
- `OMBRE_BASE_URL` → `OMBRE_COMPRESS_BASE_URL`
- `PASSWORD` → `OMBRE_DASHBOARD_PASSWORD`

正式名称和旧名同时存在时，以正式名称为准。删除兼容名必须经过一次明确的主版本迁移，并在发布说明中写出截止版本，禁止在普通重构中删除。
