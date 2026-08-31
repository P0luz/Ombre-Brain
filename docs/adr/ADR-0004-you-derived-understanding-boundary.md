# ADR-0004: You 派生认识与单工具显隐边界

## Status

`Accepted`，接受日期为 2026-08-18。项目所有者在确认规格后明确要求按方案实施，并确认
开关的含义是控制单个 `You` MCP 工具是否暴露，而不是开关整个 MCP。

## Decision

Ombre 新增默认关闭的 `You` 派生认识模块。前端只显示一个二元总开关；开启后，唯一公开变化
是 MCP `tools/list` 新增一个只读 `You` 工具，关闭后该工具从工具清单和 tool search 完全消失。
现有工具的名称、schema、权限和行为以及 `mcp_require_auth` 均保持不变。

`You` 的真源仍是普通记忆桶及其不可变原文证据。Claim、Evidence、Review Receipt 和
Projection 存在 `<buckets_dir>/.you/`，属于可重建的内部派生状态，不是新的普通记忆真源。
完整作用域 ID 在首次启用时生成并随该目录持久化；缺失、损坏或不匹配时一律按关闭处理，
不回退到显示名、`global` 或默认用户。

## Why this is not cognition

`You` 只允许明确称呼、明确边界、明确长期事实、普通偏好和相处习惯。性格判断、自我认同、
关系评价以及健康、创伤、财务、性与亲密经历由固定服务端策略直接拒绝，不生成候选。
系统只判断证据数量、独立事件、日期、版本和可调用状态；Claim 永远携带
`instructional_force=none`，不能控制当前模型的立场、推理或行动。

普通偏好与习惯至少需要两个独立 Evidence Group 和三个不同日期的有效 Review Receipt；
明确称呼、明确边界和明确长期事实可以在原始证据持久化后的整理中直接成为 formal。
新证据与现行 Claim 冲突时先形成不可调用候选，满足同样门槛后才能以新版本替代旧版本。

## Why this is not a database feature

这不是给记忆库加一张「用户属性表」。`You` 没有引入新的真源：Claim 全部由普通记忆桶
及其不可变原文证据推导而来，`.you/` 整个目录删掉之后可以从记忆重建，而记忆一条都不会少。
反过来不成立——删掉一条记忆桶，依赖它的 Claim 必须跟着失效，这个方向是不可逆的。
谁是真源，由这个不对称决定。

它也没有增加任何查询面。`You` 不提供按属性筛选记忆、不提供「查出所有认为用户 X 的证据」、
不提供聚合与统计。它只有一个读回口，返回的是已经立住的那几条，不接受结构化查询条件。
一个能按属性检索人的接口，无论叫什么名字，都会变成用户画像库——这正是要避开的形状。

前端只有一个二元开关，没有列表、没有筛选器、没有批量操作、没有审核队列。
Dashboard 上看不到 Claim、Evidence、Projection 或任何计数。人类这一侧能做的动作只有
「开」和「关」，这个克制是刻意的：一旦给出管理界面，它就成了一张要维护的表。

## How tombstones are preserved

`You` 不产生自己的墓碑，也不擦除任何已有的墓碑——它是派生层，没有权力对真源做任何删除。

普通记忆的 `deleted_at` 是唯一的墓碑，`You` 只读它、不写它。带 `deleted_at` 的删除归档
会阻断依赖该证据的 Claim，但被阻断的 Claim 本身也不是硬删除：它转为不可调用状态并保留
版本与 Review Receipt，记录「这条曾经立住过，因为依据消失而退场」。这条历史留着，是为了
让同一个判断在证据恢复后能被重新审视，而不是凭空再立一次。

关闭 `You` 同样不删除任何派生状态，只是冻结。「关掉之后一切照旧存在、只是停止参与」
与「关掉即抹除」是两种完全不同的承诺，这里选的是前者——抹除会让开关变成一个破坏性操作，
而人类按下它的时候未必知道这一点。

## How present thinking remains with the LLM

`You` 返回的是过去立住的判断，不是此刻的结论。每条 Claim 永远携带
`instructional_force=none`：它不能要求模型采取任何立场、不能约束推理方向、不能替代
当前对话里的判断。读回内容是描述性的历史上下文，不是系统指令，也不是用户此刻的命令。

系统不预先计算「应该怎么回应这个用户」，不生成建议、不排序倾向、不给出默认答案。
它只判断证据数量、独立事件、日期、版本和可调用状态——这些全是**结构性**的事实，
不涉及「这个人是什么样的」这类判断。那类判断如果要做，只能由当前的模型在当前的对话里做，
而且做完就随对话过去，不沉淀成一条新的 Claim。

这条边界的意义在于：`You` 让模型记得住上次弄清楚的事，但不替它决定这次该怎么想。

## How forgetting still works

普通自动归档只改变事件记忆可见性，不撤销证据；带 `deleted_at` 的删除归档、受控物理擦除、
正文或关键元数据变化会同步阻断依赖 Claim，并通过耐久 outbox 重算。`You` 不恢复记忆、
不改变权重、重要度、激活时间、pinned、protected、anchor 或普通检索结果。

关闭 `You` 只冻结派生数据：不产生、不重算、不读取、不投影，也不删除已有派生状态。
重新开启只处理开启后的新 bucket change event，不自动回填历史。

## Tool exposure and authentication

FastMCP 的工具管理器支持运行时 `add_tool` 和 `remove_tool`。切换由一个进程内控制器串行执行，
工具函数本身每次调用仍重新读取权威状态与 `state_revision`，防止旧会话或迟到任务绕过关闭。
关闭后缓存旧清单的客户端直调 `You` 得到与未知工具相同的错误，不返回 `feature_disabled`、
内部数量或存在性信息。

开关 API 使用 Dashboard 既有会话鉴权。它不读写 `mcp_require_auth`、OAuth、静态 Token、
传输方式或任何其他工具注册。现有 MCP 鉴权中间件继续覆盖整个 `/mcp`。

## Invisible UI and safe output

Dashboard 除总开关和真实生效状态外不显示 Claim、画像、证据、候选、数量、历史或条目操作。
`You` 工具不返回 Source、Evidence、Claim、Projection、ID、时间或评分，只返回固定预算内的
抽象 semantic hints。提示按不可信历史数据包装，不具备指令力。

Claim、Projection 和 semantic hint 在持久化或返回前都与 Source Bucket、不可变 Source 及
上游派生文本执行归一化连续片段检查。检查不可用、输入损坏或输出命中受保护原文时失败关闭，
本轮不返回提示，不降级为原文。模型必须结合当前 user turn 自行组织语言。

## Durable processing and storage

普通桶先提交，之后只把 bucket ID、动作、开关 revision 和幂等键写入
`<buckets_dir>/.you/you.sqlite3` 的 outbox 表；队列不复制原文。处理器执行前重新检查开关
revision，按至少一次投递和幂等提交处理。开关状态、Claim、Projection 与 outbox 在同一个
SQLite 权威库中事务提交，数据库使用 `journal_mode=DELETE` 与 `synchronous=FULL`，避免复制
数据库文件时依赖外置 WAL。

本地完整导出与 GitHub 备份必须显式携带 `.you` 数据并写入清单。迁移导入先验证路径、大小、
摘要、SQLite 完整性、固定 schema 与作用域，再整体发布；旧备份没有 `.you` 时保留当前状态，
新安装仍按默认关闭处理，且不从普通记忆自动回填。

## Public Tool Design Contract

`You` 是 `normal` 暴露级别的只读器官工具，但受独立 feature gate 约束。它不提供写 Claim、
强制升格、重建投影、读取证据或列举内部记录的参数。Public Tool Design Contract 只把
规范化名称 `you` 加入允许集合，不允许 `update_user_profile` 等工程或画像名称借道公开。

## Rejected alternatives

- 复用 `mcp_require_auth`：会同时改变整个 MCP 的访问边界，破坏功能隔离。
- 始终注册工具并返回 `feature_disabled`：会在关闭时泄露功能存在，不符合单工具显隐语义。
- 通过 breath、SessionStart 或 hook 自动注入：会让关闭态和其他工具行为发生交叉。
- 展示 Claim 审核页：违背“除总开关外全部不可见”的产品决定。
- 直接返回 Claim、Projection 或原文：会让宿主模型照搬存储文本，而不是自行复述。
- 用显示名作为作用域键：改名和多实例场景会串数据。

## Tests required

测试必须覆盖默认关闭、开关 revision 竞争、工具清单只增减 `You`、关闭后旧会话未知工具、
`mcp_require_auth` 不变、其他工具 manifest 逐字段不变、三维作用域隔离、敏感类别拒绝、证据组
独立性、跨日 Receipt、冲突版本、来源变化失效、迟到任务、原文归一化绕过、无旁路注入、
前端只存在一个总开关，以及本地/GitHub 备份恢复。
