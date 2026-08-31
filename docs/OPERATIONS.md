# Ombre Brain 可靠性与恢复手册

## 安全部署向导

首次登录后打开 `/onboarding`，只选择部署意图：

- 本机：仅同一设备回环使用；默认保留鉴权，第三方客户端推荐静态 Token。
- 公网安全：HTTPS 域名远程使用，OAuth 强制开启。
- 高级：已有反向代理或外部鉴权时自行选择，系统仍持续报告风险。

向导写入现有 `config.yaml`，不会创建第二套配置。OAuth 与传输模式在进程启动时绑定，保存后必须重启。系统体检的“实际生效配置”同时显示已保存值、当前进程值、环境变量来源和持久卷状态；只有环境变量确实改变保存值时才告警。

公网安全模式中的“公网连接地址”是 OAuth 与 MCP 共同使用的外部来源地址。可以粘贴域名、`https://域名` 或完整的 `https://域名/mcp`，系统会保存为规范化的 HTTPS origin，并自动生成 `/mcp` 地址。修改地址后需重启服务，并让 MCP 客户端重新连接/授权；绑定旧地址的授权码或 refresh token 会返回 `invalid_grant`，不会继续签发随后必然 401 的 token。

Docker/Zeabur 的持久卷统一挂载 `/app/buckets`，配置路径为 `/app/buckets/config.yaml`。Zeabur 从 GitHub 部署时只需添加模型 Key、挂载该卷、绑定 HTTPS 域名，再从向导选择“公网安全模式”。不要在平台中长期保留 `OMBRE_MCP_REQUIRE_AUTH` 或 `OMBRE_TRANSPORT`，除非明确希望平台覆盖 Dashboard。

网络 MCP 关闭鉴权时，裸机由 `OMBRE_BIND_HOST` 表示进程监听地址，Docker 由 Compose 的宿主端口绑定决定，官方模板会把 `OMBRE_BIND_ADDRESS` 同步传入容器。OB 会诊断非回环、局域网/NAS 和未知 Docker 边界的匿名访问风险，但 2.11.1 起不再把明确的 `mcp_require_auth: false` 在内存中改回鉴权。Dashboard / 向导保存非回环免鉴权组合和内置 Tunnel 启动仍要求精确设置 `OMBRE_ALLOW_INSECURE_MCP=true`；直接由配置文件或平台环境变量关闭鉴权则由部署者自行保证外层边界。外部独立隧道无法由 OB 自动探测，应优先使用 OAuth 或静态 Token。

`OMBRE_BIND_ADDRESS=127.0.0.1` 证明的是官方 Compose 的**宿主端口映射**不向局域网开放，不等于隔离同一 Docker 网络中的其他容器。免鉴权部署必须同时保证该容器网络没有不可信成员；多实例、自定义网络或无法确认的编排应使用 OAuth/静态 Token。

这份文档说明 Ombre Brain 在断网、模型限流、外部编辑和备份恢复时真正保证什么。

## 数据边界

- `buckets/**/*.md` 是事件记忆真源。写入成功以 Markdown 原子落盘为准。
- `_sources/src_<sha256>.source` 是结构化 `grow` 可选生成的不可变原文证据资产；它不参与普通浮现或索引，但不能从事件 Markdown 重新推导，备份时必须与桶共同保存。
- GitHub 同步会把 `_sources` 原文明文提交到已配置仓库。它不是端到端加密备份；生产上应使用可信私有仓库并审计协作者权限，绝不能把包含私密对话的 path prefix 放进公开仓库。
- 本地导出的 ZIP 同样是未加密的敏感资产；应加密保管或放入可信存储，并在传输后清理不再需要的临时副本。
- `embeddings.db`、BM25 缓存和脱水缓存都是可重建的派生数据。
- `.embedding_outbox.json` 只保存待索引 ID、内容哈希和重试状态，不复制记忆正文。
- `<buckets_dir>/.you/you.sqlite3` 是“你 / You”首次开启后才创建的派生认识库，保存作用域、Claim、跨日重申收据和 Projection；自动派生时代的旧快照可能额外带无消费者的 `outbox` 表。关闭开关会立即移除 `You` MCP 工具并拒绝读写/撤回，但不删除既有状态，也不影响其余 MCP 工具。
- `<buckets_dir>/.them/them.sqlite3` 是“其他人 / them”首次开启后才创建的独立派生认识库，保存作用域、按人分份的名册与 Claim；它与 You 分库，没有 Projection 或 outbox。关闭开关会移除 `Them` MCP 工具并保留既有状态。人类可见的称呼、可见转述与一次性纠错提示也随该库备份。
- `config.yaml`、`.env`、API Key、OAuth/Tunnel token 不进入本地记忆导出包。

## 写入与恢复保证

1. embedding 不可用、限流或超时时，Markdown 仍先保存，后台 outbox 持久重试。
2. 连续 provider 故障会打开全局熔断，避免每条待办都重复撞击同一个故障端点；冷却后自动恢复，也可在 Dashboard 手动补齐。
3. Obsidian、Git 或手工修改 Markdown 后，BucketManager 会按配置的轮询间隔发现文件集合/mtime/size 变化，刷新内存与 BM25，并只对正文变化重新排队向量。
4. 本地导出对正在使用的 SQLite 调用 backup API，得到事务一致快照；不会直接复制可能处于 WAL 写入中的数据库文件。
5. v2.10.1 起，新导出会同时包含 `buckets/*.md`、`sources/src_<sha256>.source` 和 `backup_manifest.json`，逐文件记录字节数与 SHA-256。创建过模块库的实例还会包含事务一致的 `you/you.sqlite3` 和/或 `them/them.sqlite3`；GitHub 备份中的对应路径为 `.you/you.sqlite3` 与 `.them/them.sqlite3`。恢复预检要求清单与归档/远端文件集合一致，并校验每个成员的大小与 SHA-256。
6. 迁移执行时先安装全部已校验证据，再写入引用它们的桶。v2.10.0 旧包缺证据时仍可恢复事件桶，但界面会明确提示这些原文不可核对，不会伪装成完整证据恢复。
7. 当前版本创建的新本地/GitHub 备份会交叉检查桶的 `source_refs`；任一引用缺文件或格式非法时整次备份失败。GitHub 恢复先把全部远端 blob 暂存并验证，再安装全部证据，最后才覆盖 Markdown。
8. You 与 them 快照在导出和恢复前都会只读执行 SQLite `PRAGMA quick_check`，核对固定表/索引、唯一模块状态、完整 Scope，并反序列化每条记录检查作用域一致性；任一检查失败都拒绝发布该快照。
9. 本地记忆包迁移只有在所有桶按原 ID 完整导入时才安装 You / them 快照；出现 `skip`、`keep_both` 或其他 ID 变化时会记录“快照未恢复”，并保留当前模块库。安装采用同目录临时文件 + `fsync` + `os.replace`，完成后重新同步各自工具；同步失败时把对应模块按关闭处理。
10. GitHub 恢复会在写入 vault 前验证 `.you/you.sqlite3` 与 `.them/them.sqlite3`，但当前 Web 恢复处理只主动刷新 `You` 的运行时门禁，没有刷新 `Them`。恢复包含 them 的仓库后应重启服务，再让 MCP 客户端刷新工具列表；磁盘上的 `.them/them.sqlite3` 在重启前已经恢复，但当前进程可能仍使用旧开关缓存/工具显隐。

清单只能发现残缺或意外篡改，不能证明备份由谁创建。需要来源认证时，应在可信存储或带签名的发布/备份系统中保管 ZIP。

## 日常检查

Dashboard 的“系统诊断”与命令行使用同一套只读检查：

```bash
python tools/check_buckets.py
python tools/check_buckets.py --json
```

检查项包括：

- Markdown 是否都能以 UTF-8 + frontmatter 解析；
- 是否存在重复 bucket ID 或指向 vault 外的软链接；
- `embeddings.db` 的 `PRAGMA quick_check`；
- 已没有对应 Markdown 的孤儿向量；
- 活跃 Markdown 缺向量时，是否已经进入 outbox。

历史兼容工具：

- `python tools/diagnose_permanent_reads.py` 只读检查旧版 permanent 召回问题，不再导入完整 server runtime。
- `python tools/migrate_feel_domain.py` 默认只读预演；确认旧 feel 元数据后必须显式加 `--apply`。
- `python tools/fix_unpinned_permanent.py` 默认只读。`--force-demote` 只用于人工确认的旧数据；当前显式 permanent 是合法类型，不能批量自动降级。

## 备份与恢复演练

1. 在 Dashboard 导出完整记忆包，确认请求成功且文件非空。
2. 准备一个全新的临时 vault/测试实例，不要直接覆盖唯一的生产目录。
3. 在迁移页面上传 ZIP。新包应显示“备份清单与 SHA-256 校验通过”；无清单旧包会显示“未验证”，有原文引用但缺证据的 v2.10.0 包会显示兼容性警告。
4. 检查 bucket 数、冲突决策和 embedding 模型/维度，再执行导入。
5. 导入完成后运行 `python tools/check_buckets.py`，并用 `breath_search(query=...)` 抽查可检索性；若测试包含原文证据，检查 `<vault>/_sources/` 下的 `.source` 文件已随导入落盘（3.0.0 起没有回读工具，原文只能从磁盘核对）。
6. 确认 outbox 待处理数最终回到 0。模型离线时允许保持 pending，但 Markdown 必须完整可读。
7. 若备份包含 You / them，登录 Dashboard 检查两个独立开关；本地记忆包迁移会自动同步门禁，GitHub 恢复包含 them 时按上节说明重启服务。让 MCP 客户端重新获取工具列表：两个都关为 16 个基础工具，只开一个为 17 个，两个都开为 18 个；新增项应分别只有 `You` / `Them`。
8. You 没有 Dashboard 条目浏览入口；them 可用名册核对人名与来源分组，但不要把人类看不到的第一手 Claim 正文当作恢复验收项。

导入冲突的语义：

- `skip`：保留当前记忆，不导入冲突项。
- `keep_both`：导入项获得新 ID；可复用的向量同步映射到新 ID。
- `overwrite`：当前项不会被物理抹去，而是归档并获得唯一的 `*-superseded-*` 历史 ID；导入项接管原 ID。

## 故障处置

| 现象 | 数据状态 | 处理 |
|---|---|---|
| embedding 超时/限流 | Markdown 已保存，向量 pending | 检查网络/额度；等待熔断冷却或手动补齐 |
| 语义检索不可用 | 关键词/BM25 仍可读，返回明确降级提示 | 修复 provider 后等待 outbox 清空 |
| Obsidian 修改后结果旧 | 等待外部变更轮询周期 | 检查 `storage.external_change_poll_seconds`，再看系统诊断的外部变更计数 |
| ZIP 上传被拒绝 | 本地 vault 未写入 | 按错误修复损坏、路径穿越、重复项或清单不一致，重新导出 |
| SQLite quick_check 失败 | Markdown 真源通常仍在 | 先备份 Markdown，移走损坏的派生库，再重建向量；不要删除 Markdown |
| outbox 长时间不下降 | 记忆正文仍安全 | 查看熔断状态、最近错误、Key/模型/维度和 provider 连通性 |
| You 开关保存失败或开启后工具列表没有 `You` | 系统保持关闭或立即回退为关闭；其余 MCP 工具不受影响 | 查看服务日志与 `<buckets_dir>/.you/` 的写入权限；修复后重新开启，并让 MCP 客户端刷新工具列表或重连 |
| MCP 客户端在关闭 You 后仍尝试调用旧工具 | 服务端返回 `Unknown tool: You`，不会读取或返回派生认识 | 让客户端重新获取工具列表或重连；不要为兼容旧缓存保留一个可调用的空壳工具 |
| them 开关保存失败或开启后工具列表没有 `Them` | 系统保持关闭或立即回退为关闭；You 与其余 MCP 工具不受影响 | 查看服务日志与 `<buckets_dir>/.them/` 的写入权限；修复后重新开启，并让 MCP 客户端刷新工具列表或重连 |
| MCP 客户端在关闭 them 后仍尝试调用旧工具 | 服务端返回未知工具，不会读取或返回 them 认识 | 让客户端重新获取工具列表或重连；不要保留关闭态空壳工具 |
| GitHub 恢复显示 them 已导入，但当前工具清单/开关仍是恢复前状态 | `.them/them.sqlite3` 已写入，当前 Web 路径未刷新 `ThemToolGate` 与状态缓存 | 重启 Ombre，再刷新 Dashboard 与 MCP 工具列表；重启前不要继续编辑 them 名册 |
| You / them 快照在 ZIP 或 GitHub 恢复时被拒绝 | 模块库不会发布；本地记忆包迁移保留当前模块状态，GitHub 整次预检失败时不写入本地 vault | 按错误检查 `quick_check`、schema、Scope、清单 SHA-256 与路径；不要手工绕过验证覆盖 SQLite |
| nginx 下输入正确 Dashboard 密码却提示密码错误 | v2.10.1 及更早版本会把代理返回的 HTML、空响应或网关错误统一回退为“密码错误” | 升级到 2.10.2+ 后按页面显示的真实类型处理；同时检查 `/auth/login` 状态码、下方完整 nginx 转发头和 `OMBRE_TRUSTED_PROXY_CIDRS`。若返回 200 但会话未建立，核对 `X-Forwarded-Proto` 与 `Set-Cookie` |
| 编辑记忆、热更新或重启提示 `Cross-origin request rejected` | 写请求被来源防护拒绝，原数据未改动；这不是 CORS 缺失 | 优先手动升级到 2.7.1+；nginx 必须保留公网 authority，传入 `X-Forwarded-Proto: https`，并让应用精确信任最后一跳代理 CIDR。不要添加 CORS 头或改写浏览器 `Origin` |
| Polaris 报 `Failed to fetch`，`/health` 为 200，但 `OPTIONS /mcp` 为 401 且无 CORS 头 | 2.8.1 及更早版本中 CORS 位于 MCP 鉴权内层，静态 Token 模式错误拦截了不携带 Token 的浏览器预检 | 升级到 2.8.2+ 并重建/重启服务；确认预检返回 200，且响应包含 `Access-Control-Allow-Origin`、允许 `POST` 和客户端使用的 Token 请求头 |
| `mcp_require_auth: false` 但 `/mcp` 仍返回 `401`，CC 云 session 报 `MCP error 32003` | 2.8.12–2.11.0 的启动门禁在非回环/未知云边界中覆盖了关闭鉴权配置 | 升级到 2.11.1+ 并重启；确认 Dashboard 的“当前生效 MCP 鉴权”为关闭。公网匿名 MCP 风险很高，条件允许时仍优先使用 OAuth 或静态 Token |

### nginx 反代与 v2.7.0 脱困

`Cross-origin request rejected` 是应用的 CSRF 来源校验，不是浏览器 CORS
预检失败。nginx 增加 `Access-Control-Allow-Origin` 不会改变请求的
`Origin`、Host 或协议，因此不能修复这个 403，还可能制造重复 CORS 响应头。

同机 nginx 反代到默认 Docker 端口时可使用：

```nginx
location / {
    proxy_pass http://127.0.0.1:18001;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

`$http_host` 会保留非默认公网端口；不要用上游地址覆盖 Host，也不要只发送
RFC 7239 `Forwarded` 而省略上述 `X-Forwarded-*`。如果 nginx 前面还有 CDN，
先正确配置 nginx `real_ip`，再使用清洗后的 `$remote_addr`。

v2.7.1+ 只采信来自 `OMBRE_TRUSTED_PROXY_CIDRS` 的转发头。这里应填写
**直接连接 OB 的最后一跳 nginx/代理地址或网段**，不是浏览器公网 IP、域名或
`0.0.0.0/0`。Docker 中该 peer 常是精确的 `172.x` 网桥网段；修改
`deploy/.env` 后要用 `docker compose ... up -d --force-recreate`，仅重启旧容器
不会注入新增环境变量。

v2.7.0 的热更新和重启按钮本身也是 POST，因此可能一起被旧 CSRF bug 卡住。
先按上面配置并 reload nginx；若仍无法使用 Dashboard，请不要给更新接口关闭
CSRF，直接从宿主机升级：

```bash
# 预构建镜像部署
docker compose -f deploy/docker-compose.user.yml pull
docker compose -f deploy/docker-compose.user.yml up -d --force-recreate

# 源码构建部署则先 git pull，再重建
git pull --ff-only origin main
docker compose -f deploy/docker-compose.yml up -d --build --force-recreate
```

## 访问控制

- Dashboard 会话默认 30 天过期，可通过 `OMBRE_DASHBOARD_SESSION_DAYS` 调整为 1-365 天。认证文件与 token 文件使用原子写入，并在支持的系统上限制为仅文件所有者可读写。
- 网络 MCP 免鉴权只允许已确认的本机回环边界。启动门禁、Dashboard 配置、部署向导与内置 Tunnel 共用同一规则；`OMBRE_ALLOW_INSECURE_MCP=true` 是不在 Dashboard 暴露的高风险逃生阀，只接受精确的 `true`。
- 登录和 OAuth 授权共用失败限流。`X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host` 只在请求确实来自可信反代时采用；内置 Tunnel 使用回环地址，外置 nginx/Caddy/容器反代应通过 `OMBRE_TRUSTED_PROXY_CIDRS` 添加直接连接 OB 的最后一跳代理 CIDR，不能使用 `0.0.0.0/0`。三个官方 Compose 模板都会把该变量从 `.env` 传入容器。
- 内置 JSON OAuth 状态按单进程部署设计。官方 Docker/Render 启动方式使用单 worker；自行部署时不要启动多个 Web worker 或多个共享同一数据卷的副本，否则授权状态不具备跨进程事务保证。
- `limits.max_management_request_bytes` 限制普通 Dashboard/OAuth 写请求；导入文本和迁移 ZIP 仍使用各自更大的流式上限。
- `/api/update-info` 包含数据目录和容器信息，因此需要 Dashboard 登录；公开健康检查仅使用 `/health` 和 `/api/version`。

## Docker 热更新与代码播种

容器内的记忆真源和运行代码是两类资产。记忆始终以 `buckets/**/*.md` 为准；运行代码由 `entrypoint.sh` 从镜像播种到可写卷上的 `OMBRE_CODE_DIR`，Dashboard 热更新只修改后者。

启动器用两项信息判断镜像是否需要重新播种：

1. 根目录 `VERSION`；
2. 镜像 `src/` 与 `frontend/` 的稳定 SHA-256 代码指纹。

`.seeded_image_fingerprint` 保存“上次播种所用镜像”的指纹，而不是当前运行目录指纹。这个区别是刻意的：Dashboard 热更新会让运行目录不同于镜像，但只要镜像基线没变，重启必须继续保留热更新；本地以相同 `VERSION` 重建了不同代码的镜像时，镜像指纹会变化并触发重新播种。

重新播种先复制到暂存目录并检查 `src/server.py` 与 `frontend/`，完成后才切换活动树。原先健康的运行树会进入 `_prev`；新树连续启动失败达到阈值后自动回滚，回滚结果不会在同一次启动中再次被同一坏镜像覆盖。

常用日志状态：

| 状态 | 含义 |
|---|---|
| `code-state=image-match` | 活动代码与镜像完全一致 |
| `code-state=runtime-override` | 活动代码来自热更新或回滚，镜像未变化，因此保留 |
| `code-state=reseed reason=image-fingerprint-changed` | 版本号相同，但镜像代码内容变化，已重新播种 |
| `code-state=legacy-residue` | 数据目录里发现非活动的历史 `_app`，只提示、不自动删除 |

排障必须先看日志中的“活动代码目录”。默认部署的 `<数据目录>/_app` 可能正在使用，不能仅凭其中的 `VERSION` 新旧决定删除。只有明确出现 `code-state=legacy-residue` 时，该路径才是非活动遗留；建议先备份再手工清理。

紧急情况下可为单次启动设置 `OMBRE_FORCE_CODE_RESEED=1`，强制丢弃卷内运行覆盖并从镜像重新播种。确认启动成功后必须移除该变量，否则每次启动都会重新播种。

`entrypoint.sh` 本身来自镜像，不在 Dashboard 热更新覆盖范围内。升级到带有新播种逻辑的版本时必须先拉取/重建镜像一次，不能只点击 Dashboard 更新。

Dashboard 热更新会限制下载包、成员数、单文件大小、总解压量和压缩率。建立 `_prev` 回滚点失败时不会继续覆盖；逐文件写入采用原子替换。依赖变化以正式发布使用的 `requirements.lock.txt` 为准，旧更新包缺少锁文件时才回退 `requirements.txt`；真实依赖变化且未显式开启 `OMBRE_UPDATE_ALLOW_PIP=1`（或 Dashboard 「热更新」面板里的等效开关）时，热更新会在写入任何文件前直接拒绝并要求重建镜像，避免”代码更新成功但重启后缺包”——2.16.2 起这道检查提前到下载/解析完依赖清单之后、真正覆盖 src/frontend 之前，不会再出现”先备份写文件、再整体回滚”的多余一轮。

已经停留在 2.8.4、且出现“新版依赖清单有变化”回滚提示的实例，可以直接重新点击官方 `main` 热更新。官方 GitHub 归档通过 `.gitattributes` 排除仅供开发者使用的宽松 `requirements.txt`，同时保留带 hash 的 `requirements.lock.txt`；旧更新器会跳过错误的源清单比较，新更新器接管后则继续按发布锁判断。自建镜像/镜像站若重新打包了根级 `requirements.txt`，仍应重建镜像，或仅在明确理解风险时临时设置 `OMBRE_UPDATE_ALLOW_PIP=1`。

上述直升兼容仅在发布锁与 2.8.4 相同期间启用。测试会固定校验该锁的规范化 SHA-256；任何真实依赖变更都必须先移除归档兼容规则并设计旧实例迁移，禁止让旧更新器在依赖已经变化时静默跳过安装。

若希望代码与记忆目录彻底分离，生产环境优先使用命名卷或 bind mount，不要依赖无法稳定重新挂载的临时目录。例如：

```yaml
services:
  ombre-brain:
    environment:
      OMBRE_CODE_DIR: /app/ombre-code/_app
    volumes:
      - ombre-code:/app/ombre-code
      - ./buckets:/app/buckets

volumes:
  ombre-code:
```

命名卷默认可跨 `docker compose down` / `up` 复用；执行 `down -v` 会主动删除它。Dashboard 对独立代码卷的检测来自 `/proc/self/mountinfo`，不会再把它误报成容器 overlay 临时层。

## 配置

```yaml
storage:
  external_change_poll_seconds: 1.0

embedding:
  background_indexing: true
  retry_base_seconds: 5
  retry_max_seconds: 300
  circuit_failure_threshold: 3
  circuit_base_seconds: 30
  circuit_max_seconds: 600
```

轮询设为 `0` 表示每次活跃桶列表读取都检查文件状态。生产环境一般保留 `1.0`，避免高频目录扫描。
