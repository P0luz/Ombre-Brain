# 环境变量参考

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `OMBRE_API_KEY` | 是 | — | Gemini / OpenAI-compatible API Key，用于脱水(dehydration)和向量嵌入 |
| `OMBRE_BASE_URL` | 否 | `https://generativelanguage.googleapis.com/v1beta/openai/` | API Base URL（可替换为代理或兼容接口） |
| `OMBRE_TRANSPORT` | 否 | `stdio` | MCP 传输模式：`stdio` / `sse` / `streamable-http` |
| `OMBRE_PORT` | 否 | `8000` | HTTP/SSE 模式监听端口（仅 `sse` / `streamable-http` 生效） |
| `OMBRE_BUCKETS_DIR` | 否 | `./buckets` | 记忆桶文件存放目录（绑定 Docker Volume 时务必设置） |
| `OMBRE_HOOK_URL` | 否 | — | Breath/Dream Webhook 推送地址（POST JSON），留空则不推送 |
| `OMBRE_VECTOR_MIN_SIM` | 否 | `0.70` | `breath` 向量通道的放行线:余弦超过它的桶才以「语义关联」补进结果(2026-09-30 由写死的 0.5 改来,理由见 INTERNALS 3.3.1)。设 `0.5` = 旧行为;换 embedding 模型后要重量 |
| `OMBRE_RECALL_TOKEN` | 否 | — | **自动浮现**查询口 `GET /api/recall` 的 Bearer token(2026-09-30,见 INTERNALS 3.3.2)。**不设 = 该口子关着(404)**。值与 kelivo-shim 的 `RECALL_TOKEN` 相同,不入库 |
| `OMBRE_APP_TOKEN` | 否 | — | **App 的小钥匙**(2026-11-01):原生 App「Intertidal」的 Journal 页经 dwell-bridge 读 / 改 / 删记忆、信箱、便利贴,dwell 带 `Authorization: Bearer <它>`。**只开白名单**(`server.py` 的 `_APP_TOKEN_ROUTES`:桶列表 / 桶详情 / 改桶 / 删桶 / 信箱读改删 / 便利贴读勾改删),设置、导入、改密码、回收站一律不开。**不设 = 关着,与改动前完全相同**。值与 dwell-bridge 的 `OMBRE_APP_TOKEN` 相同,不入库 |
| `OMBRE_MCP_TOKEN` | 否 | — | **机器走的门的钥匙**(2026-10-03,`mcp_guard.py`)。设了之后 `/mcp`、`/breath-hook`、`/dream-hook` 要求 `Authorization: Bearer <它>`(或 `X-Ombre-Token`),否则 401。**不设 = 不上锁,与改动前完全相同**。晏(shim 的 `mcp-servers.json`)、Claude Code 会话、开场钩子、GitHub 体检(secret 同名,可选)用同一把。不入库 |
| `OMBRE_HOOK_SKIP` | 否 | `false` | 设为 `true`/`1`/`yes` 跳过 Webhook 推送（即使 `OMBRE_HOOK_URL` 已设置） |
| `OMBRE_DASHBOARD_PASSWORD` | 否 | — | 预设 Dashboard 访问密码；设置后覆盖文件存储的密码，首次访问不弹设置向导 |
| `OMBRE_DEHYDRATION_MODEL` | 否 | `deepseek-chat` | 脱水/打标/合并/拆分用的 LLM 模型名（覆盖 `dehydration.model`） |
| `OMBRE_DEHYDRATION_BASE_URL` | 否 | `https://api.deepseek.com/v1` | 脱水模型的 API Base URL（覆盖 `dehydration.base_url`） |
| `OMBRE_MODEL` | 否 | — | `OMBRE_DEHYDRATION_MODEL` 的别名（前者优先） |
| `OMBRE_EMBEDDING_MODEL` | 否 | `gemini-embedding-001` | 向量嵌入模型名（覆盖 `embedding.model`） |
| `OMBRE_EMBEDDING_BASE_URL` | 否 | — | 向量嵌入的 API Base URL（覆盖 `embedding.base_url`；留空则复用脱水配置） |
| `OMBRE_AWAKEN_FULL_SESSIONS` | 否 | `2` | awaken 的「最近对话归档」区出全文的条数（钳在 1~3）。默认 2 = 日记桶 + 原话桶各一条 |
| `OMBRE_AWAKEN_PINNED_FULL` | 否 | `1` | awaken 的「钉选」区是否出正文全文（2026-08-21）。设 `0` 回到只出摘要行的旧行为——**急救开关**，改值 + restart 即生效 |
| `OMBRE_AWAKEN_PINNED_CHARS` | 否 | `800` | 钉选区**每条**正文的字数上限，超出截断并标注（下限 100） |
| `OMBRE_AWAKEN_PINNED_TOTAL` | 否 | `6000` | 钉选区**整区**正文的字数总预算，用光后剩余的桶退回摘要行（仍列出，只是不带正文；下限 500） |
| `OMBRE_NETWORK_EDGES_PER_NODE` | 否 | `6` | `GET /api/network` 每个桶最多连几条边（2026-09-01）。设 `0` = 不封顶（相似度 >0.5 的边在 438 桶下实测约 1.2 万条）。改值 + restart 即生效，详见 `INTERNALS.md` 1.10。⚠️ **2026-09-03 起换了个前端在用它**：原来的「记忆网络」页已删，同日新建的 `/turbulence`（记忆乱流）接上了这条接口，用它画实线（「说的是相近的事」）。所以**这个变量仍然在生效**，调大调小会直接改变乱流页上的线密度，详见 `INTERNALS.md` 1.12 |
| `OMBRE_BACKUP_REPO_URL` | 否 | — | 每日备份推去的私有仓库地址(线上是 `Mia06250603ian/ob-backup`)。**和下面的 `OMBRE_BACKUP_TOKEN` 两个都设了才开启备份** |
| `OMBRE_BACKUP_TOKEN` | 否 | — | **OB 把备份 `git push` 进上面那个仓库用的 GitHub 钥匙**(细粒度个人令牌,只给那一个仓库 Contents 读写)。**只存在 Zeabur 上,GitHub 那边没有它**。⚠️ 失效时记忆库照常活着,只是备份推不上去(日志 `could not read Password`)—— 2026-09-21 起就这么断过三天。现值:2026-09-23 所有者新建,名 `ob-backup-2026-09`,**永不过期**。换法见 `rivers-system/OPERATIONS.md` 第 7 节《备份推不上去》 |
| `OMBRE_BACKUP_TRIGGER_TOKEN` | 否 | — | GitHub Actions 的 *Daily Backup* 敲门叫 OB 备份用的暗号(`POST /api/export-backup` 的 `X-Backup-Token`)。⚠️ **Zeabur 和 GitHub secret 两边必须同值**。**别和上一行搞混**:名字只差一个 `TRIGGER`,一个是「敲门」、一个是「推送」 |
| `OMBRE_BACKUP_INTERVAL_HOURS` | 否 | `24` | OB 自己的备份节拍(和 GitHub 那次戳是双保险) |
| `OMBRE_BACKUP_LOCAL_PATH` | 否 | `<桶目录的上一级>/ombre_backup_repo` | 备份仓库在容器里的本地克隆位置(线上 `/app/ombre_backup_repo`) |

## 说明

- `OMBRE_API_KEY` 也可在 `config.yaml` 的 `dehydration.api_key` / `embedding.api_key` 中设置，但**强烈建议**通过环境变量传入，避免密钥写入文件。
- `OMBRE_DASHBOARD_PASSWORD` 设置后，Dashboard 的"修改密码"功能将被禁用（显示提示，建议直接修改环境变量）。未设置则密码存储在 `{buckets_dir}/.dashboard_auth.json`（SHA-256 + salt）。

## Webhook 推送格式 (`OMBRE_HOOK_URL`)

设置 `OMBRE_HOOK_URL` 后，Ombre Brain 会在以下事件发生时**异步**（fire-and-forget，5 秒超时）`POST` JSON 到该 URL：

| 事件名 (`event`) | 触发时机 | `payload` 字段 |
|------------------|----------|----------------|
| `breath` | MCP 工具 `breath()` 返回时 | `mode` (`ok`/`empty`), `matches`, `chars` |
| `dream` | MCP 工具 `dream()` 返回时 | `recent`, `chars` |
| `breath_hook` | HTTP `GET /breath-hook` 命中（SessionStart 钩子） | `surfaced`, `chars` |
| `dream_hook` | HTTP `GET /dream-hook` 命中 | `surfaced`, `chars` |

请求体结构（JSON）：

```json
{
  "event": "breath",
  "timestamp": 1730000000.123,
  "payload": { "...": "..." }
}
```

Webhook 推送失败仅在服务日志中以 WARNING 级别记录，**不会影响 MCP 工具的正常返回**。
