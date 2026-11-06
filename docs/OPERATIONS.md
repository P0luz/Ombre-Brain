# Ombre Brain 可靠性与恢复手册

## 来源识别（4.1.4）

登录、OAuth 授权、密保恢复、OAuth 动态注册、autorecall 和 hooks 都按“来源”分别限流。4.1.4 改了可信反代后面的来源取法，不新增环境变量。

- 只有直接连接 OB 的一方在 `OMBRE_TRUSTED_PROXY_CIDRS` 里时，才读 `X-Forwarded-For`；这道门不变。默认只信回环，内置 Cloudflare Tunnel 就从回环连进来。
- 过去取 `X-Forwarded-For` 的第一段。nginx 的 `$proxy_add_x_forwarded_for` 是“追加”：客户端自己带来的值留在第一段，每次换一个假地址就永远不被锁，只剩全局每分钟 60 次兜底。按 Cloudflare 文档，请求已带 `X-Forwarded-For` 时 Cloudflare 也是追加，所以内置 Tunnel 下同样能这样绕过（按文档推断，未在真实 Cloudflare 边缘实测）。
- 现在从最右一段往左走，跳过可信网段里的地址，第一个不可信的地址就是客户端。多条 `X-Forwarded-For` 头按收到顺序合并；`203.0.113.7:51234`、`[2001:db8::1]:443` 这类带端口的写法也认。遇到认不出的值（`unknown`、空段、带引号的值等）就停，不再往左看，改用已经跨过的可信地址或直接连接方。最多看最右 32 段。
- 照 README 用 `proxy_set_header X-Forwarded-For $remote_addr;` 覆盖的部署，取到的地址和以前一样。
- 直接连接方是 IPv4 映射的 IPv6 地址（如 `::ffff:127.0.0.1`）时，现在按对应的 IPv4 比对可信网段，和链里的地址同一口径。同一个判断也决定是否采信这个连接带来的 `X-Forwarded-Proto` / `X-Forwarded-Host`（Origin 校验、会话 cookie 的 Secure 标记、OAuth 元数据里的地址），所以这种对端现在在各处都和 `127.0.0.1` 一样对待。OB 自带的 uvicorn 监听 `::` 时只收 IPv6 连接，这种对端只会出现在自行改成双栈监听之类的环境里。
- `X-Forwarded-Proto` / `X-Forwarded-Host` 仍取第一段：多层代理追加时最左边才是客户端看到的协议和主机，伪造它们只影响同一个请求自己的回应。前提是这些回应不进共享缓存，不要让 CDN 对 `/.well-known/*` 和 `/oauth/*` 开“缓存一切”。

**会追加的上游不在可信列表里：升级后按上游节点共用一把锁。** 形状是“会追加 `X-Forwarded-For` 的上游（CDN、云负载均衡、公司代理）→ 追加写法的 nginx/Caddy/Traefik → OB”，且上游的网段不在 `OMBRE_TRUSTED_PROXY_CIDRS` 里。旧版在这种形状下取到的是客户端能随意伪造的值；新版取到的是上游节点地址，走同一个节点的人共用一把锁，同地区有人连错 5 次，你也会被锁 60 秒起、最长 1 小时；hooks 和 autorecall 的每来源额度也按节点共用。这是可用性下降，不是安全下降。日志 `[auth] login rate-limit: client <来源> locked` 里看到的是上游节点地址，就是踩到了这一条。按推荐顺序任选一种处理：

1. 改走内置 Cloudflare Tunnel，源站端口不对公网开放。上游就是本机回环，默认可信，不用改配置。
2. 先让源站只接受你自己账号的回源（例如 Cloudflare Authenticated Origin Pulls，或防火墙只放行带回源证书的连接），再把 CDN 公布的回源网段加入 `OMBRE_TRUSTED_PROXY_CIDRS`。
3. 同样先满足第 2 条的前提，再在 nginx 配 `real_ip`（`set_real_ip_from` 填 CDN 回源网段，`real_ip_header` 用 CDN 给的访客地址头，或 `X-Forwarded-For` 加 `real_ip_recursive on`），然后 `proxy_set_header X-Forwarded-For $remote_addr;`。

不满足第 2 条的前提，信 CDN 的公共回源网段（不管是写进 `OMBRE_TRUSTED_PROXY_CIDRS`，还是写进 nginx `set_real_ip_from`）就等于信这家 CDN 的所有租户：别人可以用自己的账号（例如 Cloudflare Worker）把请求打到你的源站 IP，转发头由他随便填。把一个网段加入可信列表后，从这个网段直接连来的请求带的 `X-Forwarded-Proto` / `X-Forwarded-Host` 也会被采信。

改了也解决不了的情况：

- 反代必须写 `X-Forwarded-For`，覆盖或追加都行。nginx 只写 `proxy_pass` 时会把客户端带的头原样转给 OB，任何取法都能被伪造。
- 可信网段里不能有客户端。例如把 VPN 客户端网段写进去，那个网段里的人就能伪造左边的值；`0.0.0.0/0` 和客户端 IP 一律不要写。
- 本机 TCP 级转发（`ssh -R`、frp 或 cpolar 的 tcp 模式、socat、部分 rootless 容器的端口转发）从回环连进来，默认可信，却不写 `X-Forwarded-For`，客户端带的头会原样到达 OB，新旧版本都能每次换一个地址。裸机或源码部署可以设 `OMBRE_TRUSTED_PROXY_CIDRS=`（空串）不信任任何代理，代价是内置 Tunnel 也不再被信任：所有经隧道进来的请求共用一个来源；隧道带来的 `X-Forwarded-Proto` / `X-Forwarded-Host` 也不再采信，Dashboard 会话 cookie 因此不带 Secure 标记，没填“公网连接地址”时 OAuth 元数据还会退回 `http://` 内部地址，claude.ai 之类的客户端会拒绝连接。用内置 Tunnel 又要这样设的，先填好“公网连接地址”。官方 Compose 模板里空串会被换回默认值，容器部署只能改成只写真实反代的网段，或者不用这类转发。
- IPv6 按 /64 分组，手里有更大前缀（例如 /48）的人仍能不断换来源，只剩全局每分钟 60 次兜底。
- 上游被设置成不转发访客地址（例如 Cloudflare 的“Remove visitor IP headers”）时，所有人共用一个来源。
- 本机进程直接连 OB 时，连接方是回环、默认可信，仍可以自报地址，和以前一样。

## 环境变量口令轮换（4.1.4）

口令放在 `OMBRE_DASHBOARD_PASSWORD` 时，Dashboard 的改密入口会让你直接改环境变量；但过去改完重启，已签发的 Dashboard 会话、MCP 访问令牌和刷新令牌（刷新令牌一年有效）照样能用，刷新令牌还能接着换新令牌，只能手动删文件。4.1.4 起改完重启会自动撤销，不新增环境变量。

- **先升级重启一次，再改口令重启。** 升级后第一次启动只记录、不撤销：之前没有记录，无从判断口令换没换，也不能让升级把所有人踢下线。如果已经在升级的同一次重启里换了口令，日志里会有一条带个数的 WARNING，请按下面的手动步骤撤销。
- 手动撤销：停掉服务，删除数据目录里的 `.dashboard_sessions.json` 和 `.dashboard_mcp_tokens.json`，再启动。Docker 的数据目录是容器内 `/app/buckets`，也就是你挂载的那个卷。
- 怎么判断：`.dashboard_auth.json` 里的 `environment_password_fingerprint` 记着上次生效的环境变量口令的加盐 PBKDF2 指纹（和文件口令 `password_hash` 同一种算法，不存明文，也不存快哈希）；上次生效的不是环境变量口令时记 `null`。HTTP 进程开始接请求前比一次：环境变量口令换了值，或者在环境变量口令和文件口令（含还没设密）之间切换，就先撤销全部 Dashboard 会话、MCP 访问令牌、刷新令牌和待兑换的授权码，再记下新指纹。口令没变就什么都不做；热更新重启也会比一次。stdio 进程不判断。
- 撤销后要重新登录 Dashboard；MCP 客户端（例如 claude.ai 的连接器）手里的令牌失效，需要重新授权。OAuth 客户端注册不撤销，撤销前还会把内存里的注册落盘一次，老版本留下的、靠刷新令牌推断为“已授权”的注册不会因此降成待激活。
- `config.yaml` 里的静态 MCP Token（`mcp_token`，Dashboard 设置页“重新生成”写的就是它）一并作废：拿着旧口令登录过的人可以事先生成一把，再把鉴权模式存成 Token 或共存模式，等你换口令重启时一起生效。还要用 Token / 共存模式的话，登录 Dashboard 到设置页重新生成，再更新 MCP 客户端；纯 Token 模式下重新生成之前 `/mcp` 一律拒绝。作废时会重写 `config.yaml`（和在 Dashboard 保存设置一样，手写的注释不保留）；`config.yaml` 里没有这把 Token 时不碰它。
- 不撤环境变量 `OMBRE_MCP_TOKEN`（你在环境变量里自己管的钥匙，Dashboard 改不了它）、密保问答和登录限流；设了 `OMBRE_MCP_TOKEN` 时日志会提醒，怀疑它和口令一起泄露请一并更换。别人用旧口令登录期间改过的其他设置（鉴权模式、GitHub 备份目标、Webhook 等）也不会被撤回，怀疑口令泄露时请到设置页逐项核对。
- 有意在环境变量口令和文件口令之间切换，也会撤销一次，需要重新登录。
- 没设 `OMBRE_DASHBOARD_PASSWORD` 的部署也会走一遍：升级后第一次启动在 `.dashboard_auth.json` 写入 `environment_password_fingerprint: null`（还没设密的全新安装会因此新建这个文件，仍会要求设密），之后不再改写；每次 HTTP 启动还会试绑一次监听端口再立刻放开。
- 监听端口还被旧进程占着（例如裸机上旧进程没退就又启动了一个）时，本次跳过核对并打 ERROR，旧进程退出后再启动即可完成。否则旧进程之后写令牌或会话文件时，会把撤掉的授权整份写回去。这个探测只看得见同一个网络命名空间里的同一个监听地址：Docker / K8s 的新容器有自己的网络，看不见旧容器；同时改了 `OMBRE_PORT` 或 `OMBRE_BIND_HOST` 时也看不见旧进程。换口令前请确认旧容器或旧进程已经停掉（`docker compose up` 重建容器时默认先停旧的）。
- 撤销写盘失败（包括 `config.yaml` 写不进去）时打 ERROR：本进程内存里的旧授权照样清掉、不再认，但不记新指纹，下次启动再撤一次。新指纹写不进去时也是下次启动再撤，直到 `.dashboard_auth.json` 可写。升级后第一次记录就写不进去时，每次启动都按“第一次”只记录、不撤销，ERROR 会一直提示，修好之前换口令不会自动撤销。
- `.dashboard_auth.json` 读不了（例如 JSON 被改坏）时打 ERROR。设了 `OMBRE_DASHBOARD_PASSWORD` 的，无从判断口令换没换，按换过处理、撤销全部授权：环境变量口令模式下登录不读这个文件，不这样做就只剩一行日志，换口令会悄悄不生效；修好或移走这个文件之前，每次启动都会撤一次。没设的，本次跳过核对（登录本身就要读这个文件，问题会直接暴露）。删掉或移走这个文件会连指纹一起删掉，下一次启动按“第一次”只记录、不撤销。
- 同一个数据目录被两个口令不同的 Web 进程轮流使用（不支持的部署）时，每次启动都会撤销一次。
- 指纹和文件口令的 `password_hash` 一样，拿到它可以离线爆破口令。迁移 ZIP 和 GitHub 同步都不带它；但如果把整个数据目录同步到 Obsidian、iCloud 之类，它会跟着走（同一目录里的令牌文件本来就更敏感）。

## 密保与安装下载防护（4.1.3）

4.1.3 收紧现有入口，不迁移恢复码、不改变 MCP 权限。

- 设置或修改密保必须随请求提供当前密码，验证结果绑定凭据版本，保存前拒绝过期验证；首次设密页面也提交刚设置的密码。
- 恢复请求使用独立 SQLite 事务计数：同一来源每 15 分钟最多 5 次，全部来源合计最多 20 次。先持久预占额度再验证答案，失败、断开和重启均不退还额度；来源沿用可信代理规则。恢复限额不消费登录额度，密码计算仍共用并发上限。
- 限流保存在 `<buckets_dir>/.recovery_limits.sqlite3`，不存答案或密码，只保存来源摘要和请求时间；存储失败返回 503，不绕过限流。滚动窗口到期自动释放，不永久封 IP。
- Ollama 安装包只允许 HTTPS；每次 HTTP 重定向发出请求前检查新地址，继续执行现有域名策略、跳转次数限制和固定 SHA-256 校验。显式允许自定义镜像仍只放宽主机，不放宽 HTTPS。
- 验收覆盖：缺失/错误/正确密码、并发凭据变化、恢复限额与正常登录隔离、重启和多进程共享限额、存储失败、合法及恶意跳转、HTTPS 降级。使用隔离测试库，不安装真实程序。

兼容影响：旧页面提交密保会被拒绝，应刷新 Dashboard；HTTP 镜像需要改成 HTTPS。一次性恢复码替代密保另行设计，当前密保答案仍是恢复凭据。

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
10. 派生 SQLite（`embeddings.db`、`dehydration_cache.db`）在启动时打不开会被自动隔离成 `<原名>.corrupt-<时间戳>`，随即重建空库继续启动。这两个库都不含真源数据，向量与摘要会按需重新生成。**服务不会因为派生索引损坏而拒绝启动**——那等于让用户为了一个缓存丢掉全部记忆的访问权。
11. GitHub 恢复会在写入 vault 前验证 `.you/you.sqlite3` 与 `.them/them.sqlite3`，但当前 Web 恢复处理只主动刷新 `You` 的运行时门禁，没有刷新 `Them`。恢复包含 them 的仓库后应重启服务，再让 MCP 客户端刷新工具列表；磁盘上的 `.them/them.sqlite3` 在重启前已经恢复，但当前进程可能仍使用旧开关缓存/工具显隐。

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
| 记忆库里出现 `embeddings.db.corrupt-<时间戳>` 或 `dehydration_cache.db.corrupt-<时间戳>` | 对应派生库启动时打不开，已被自动隔离并重建；Markdown 未受影响 | 确认服务已正常启动、向量在后台补齐后，这些隔离文件可以删除。留着只为取证，不会被读取 |
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
先正确配置 nginx `real_ip`，再使用清洗后的 `$remote_addr`；信 CDN 网段之前先看
上文「来源识别（4.1.4）」的前提。4.1.4 起改用 `$proxy_add_x_forwarded_for`
追加也不会再被伪造地址绕过锁定，但 `$remote_addr` 仍是最省事的写法。

v2.7.1+ 只采信来自 `OMBRE_TRUSTED_PROXY_CIDRS` 的转发头。这里至少要填写
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
- 改 `OMBRE_DASHBOARD_PASSWORD` 的值再重启，会撤销旧的 Dashboard 会话、MCP 授权和 `config.yaml` 里的静态 MCP Token（4.1.4 起，升级后第一次启动除外，见「环境变量口令轮换（4.1.4）」）。
- 网络 MCP 免鉴权只允许已确认的本机回环边界。启动门禁、Dashboard 配置、部署向导与内置 Tunnel 共用同一规则；`OMBRE_ALLOW_INSECURE_MCP=true` 是不在 Dashboard 暴露的高风险逃生阀，只接受精确的 `true`。
- 登录和 OAuth 授权共用失败限流。`X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host` 只在请求确实来自可信反代时采用；其中 `X-Forwarded-For` 从右往左跳过可信网段，取第一个不可信地址（4.1.4 起，见「来源识别（4.1.4）」）。内置 Tunnel 使用回环地址，外置 nginx/Caddy/容器反代应通过 `OMBRE_TRUSTED_PROXY_CIDRS` 添加直接连接 OB 的最后一跳代理 CIDR，不能使用 `0.0.0.0/0`。三个官方 Compose 模板都会把该变量从 `.env` 传入容器。
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
  circuit_base_seconds: 300
  circuit_max_seconds: 3600
  abandon_after_failures: 10
```

`abandon_after_failures` 默认 `10`，设为 `0` 可恢复永不放弃。设为正整数 N 后，某一条向量只有在「上次失败之后别的向量成功过、这一条仍失败」攒够 N 次时，才会被标成已放弃：不再后台重试，也不计入待处理数，Markdown 原文不受影响；Dashboard、错误面板和 `pulse` 会单独显示已放弃的条数。供应商整体故障（例如余额耗尽）期间没有成功的向量，不会累计罚点；整轮失败按 5、10、20、40、60 分钟退避，之后保持 60 分钟并继续自动重试，任何成功会重置退避。新入队内容可立即探测供应商，避免被单条毒数据阻塞。修改这条记忆的内容、点击「补齐缺失向量」或更换向量引擎，都会让已放弃的向量重新尝试。

轮询设为 `0` 表示每次活跃桶列表读取都检查文件状态。生产环境一般保留 `1.0`，避免高频目录扫描。
