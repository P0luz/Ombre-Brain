# ADR-0002: Spark 只读灵感候选边界

> **该功能已于 2026-08-11 下线**：Spark（`dream(inspiration=True)`、`tools/spark_r1/`、
> `tools/spark_pilot/`、`tools/spark_shadow/` 及对应 `evaluate_spark*.py`）已连同代码与测试
> 一并从代码库移除。本文档仅作历史存档保留，不再描述现役功能。

## Status

`Accepted`，提出与接受日期均为 2026-07-30。

项目所有者已在当前协作对话中接受本 ADR，并授权按本文边界推进 R1；随后于 2026-07-30
进一步批准 R2 的最小离线 shadow 研究实现。R2 批准只覆盖 harness 自建的一次性
`test_data` Markdown vault、固定本地确定性 R1 路径和 aggregate-only 输出；它不批准
shadow 产品运行时入口、真实用户 vault、持久结构签名、任何生产模型调用或用户可见候选。
同日所有者进一步明确批准：先在 `testing` 完成默认关闭的产品候选、版本和热更新，再进入
三模型、多 seed、人工盲评、确认集和独立复制。该批准允许真实实例在调用方显式请求时只读
当前 owner 的 policy-safe 活动桶，但不构成质量门禁通过，也不批准合并到 `main`。
批准始终附带两项不可放宽的条件：

1. 产品化不新增 Spark MCP 工具。只能由现有 `dream` 的显式
   `inspiration: bool = False` 参数按需触发；默认关闭且不得后台自动运行。
2. 后续“主动拒绝与反思性自主”研究不得通过 prompt 预设、诱导或要求拒绝。本阶段不实现
   主动拒绝，只能研究分轴的记忆可信度证据与校准置信模型；不是所有记忆都默认受质疑。
   记忆可以影响行为，但不能替当前模型形成态度、作出拒绝或执行行动。

本次新增授权只覆盖 `testing` 的显式、只读、无外部模型调用产品候选。生产模型调用、持久
结构 projection、自动触发、`main` 发布及后续自主性研究仍需后续证据或另行确认。

对应代码基线为 Ombre Brain `testing` 分支 `42035164b280f1fcc3c51393428d4904942508aa`，
版本 `2.11.0`；R1/R2/Pilot 已推进至 `2.14.0`，本次 `testing` 产品候选目标版本为
`2.15.0`。形成草案时参考了桌面文件 `OB灵感研究与优化路线_2026-07-28.md`；
该文件是非规范研究素材，本 ADR 必须作为可独立审查的完整决策记录，实施不得依赖该外部文件。

## Governing constraints

[rule.md](../../rule.md) 是哲学边界唯一真源，尤其约束记忆不可抹除、记忆不能替代当下思考、
禁止认知层、自由联想的独立语义，以及 dream、plan、pinned、anchor、`I` 等结构不可被
Spark 偷换职责。[工程合作文档.json](../../工程合作文档.json) 继续约束新文件和模块确认、
测试 vault 隔离、版本、验证和报告流程。本 ADR 不修改或覆盖这两个规范文件。

## Context and scope

Spark 研究的问题是：在不改变记忆真源、不改变既有浮现语义、也不替代当前模型思考的
前提下，能否把当前张力与表面较远但关系结构可能相似的历史材料临时并置，形成少量带来源、
可拒绝的候选问题、类比、反例或尝试方向。

这里的“新颖”只允许描述相对于一次冻结上下文或测试记忆快照的差异。它不能证明候选在
基础模型训练语料、公开知识或外部世界中从未出现，也不能把重新发现称为历史首创。

本 ADR 不预设结构检索一定胜过语义检索或随机对照。以下前沿证据只用于形成可证伪假设：

- [Discovery by Dreaming v2](https://arxiv.org/html/2607.16256v2) 已撤回象征层
  `85.7% vs 64.3%` 的旧比较；现存结果不足以直接证明 OB 上的普适收益。
- [Serendipity by Design](https://arxiv.org/abs/2603.19087) 没有发现随机跨域映射对
  LLM 创意的平均显著收益，因此随机材料只保留为实验对照。
- [YARN](https://arxiv.org/abs/2603.29997) 支持分层结构抽象，同时暴露抽象层级和隐式
  因果错误；结构签名只能是待核查投影。
- [Beyond Divergent Creativity](https://aclanthology.org/2026.findings-eacl.138/) 表明
  新颖性必须以适当性为条件，不能单独作为主要成功指标。
- [Human Creativity in the Age of LLMs](https://arxiv.org/abs/2410.03703) 支持检验
  辅助撤除后的独立表现，不能只测当下主观帮助度。

## Definitions

- **当前张力（tension）**：R1/R2 测试场景显式提供、希望继续分析的问题或矛盾；它不是任务
  队列，也不自动成为 `plan`。产品候选只把本次 dream 窗口内通过严格 Spark policy 的近期
  普通事件当作比较起点；由人类还是当前 LLM 传入显式布尔参数不改变权限边界。
- **候选材料（candidate material）**：通过现有可见性边界筛选的历史事件片段及其来源信息。
- **结构提示（structural cue）**：对角色、关系、约束、转化和结果的可重建、可出错抽取；
  它不是事实或新的记忆对象。
- **Spark 候选**：基于候选材料形成的临时问题、类比、反例或待验证方向；它不是结论、指令、
  当前情绪、当前立场或行动许可。
- **Spark 响应**：一次研究调用内产生的纯响应态结果，生命周期为
  `response_only`，研究 harness 或未来 OB 服务端不可按 ID 再读取。该字段不能保证调用端聊天记录、客户端日志
  或外部 provider 已经取得的副本自动消失。
- **随机对照**：从 policy-safe 候选池等概率抽出的实验材料；它不等于 `rule.md` 所定义的
  低权重自由联想，也不进入产品默认输出。
- **R2 最小 shadow**：只在源码研究 harness 内运行的一次性适配层。它把已严格解析的合成
  场景写入自身创建、带 `test_data` provenance 的临时 Markdown vault，再只读回该 vault
  运行固定本地 R1，丢弃所有候选正文和来源标识，只返回数量级聚合。它不是产品 shadow、
  真实流量 shadow 或真实 vault adapter。

## Decision

本 ADR 接受以下 R0/R1/R2/Pilot 边界，并在第 22–30 条补充边界内授权 `testing` 产品候选：

1. R1/R2/Pilot 继续是独立研究面，不被产品代码直接 import。产品侧只在现有 `dream` 增加
   默认关闭的 `inspiration` 参数；`False` 保持原行为，普通 `breath`、显式检索、自由联想
   和所有 hook 均不自动触发或注入 Spark。
2. R1 只使用 harness 为单次运行创建并拥有的受控临时根目录；pytest 使用 `tmp_path`，CLI
   使用 `TemporaryDirectory` 或同等机制。纯内存/JSON 合成场景不是 OB 桶；只有可选临时-vault
   adapter 创建的桶，才必须在创建时带不可后补的 `test_data` provenance。清理前必须确认
   `resolve()` 后仍在本轮临时根内，并拒绝跟随符号链接、junction 或 reparse point。不得读取、
   复制、匿名化后使用或写入真实 `buckets/`，也不得把真实用户内容发送给外部生成器、评审器
   或日志服务。
3. R1 的候选层必须先做 policy 过滤，再做 lexical、embedding、结构或随机选择。相似度和
   结构分数永远没有权力授予可见性。R1 使用独立、可测试的合成 Spark policy contract；现有
   `SurfacePolicyVM` 不认识 `SPARK`，不得把字符串 `spark` 交给它后依赖未知模式回退。
4. 第一版实验池只包含 `type=dynamic`、`resolved=false` 的普通事件，并排除 archived、deleted、
   tombstone、`dont_surface`、`digested`、`anchor`、feel、plan、letter、self、`I`、
   pinned、protected 和 permanent。未知类型、未知状态和非法策略元数据一律失败关闭。R1 首轮
   不研究 resolved 材料；如果后续研究，必须作为独立消融，不能静默放开。
5. R1 不调用 live `BucketManager` 作为严格零副作用证明。实验先生成不可变 eligible snapshot，
   后续检索、结构抽取、生成和评审都只消费该快照。
6. R1 可以使用依赖注入的外部 generator/evaluator 作为实验仪器，但二者必须使用隔离提示，
   记录 provider、请求模型别名、provider 实际返回的模型标识、提示哈希、参数、seed 和数据
   截止时间；若版本不可验证，必须如实标记。结构提示至少分为实体、角色/动作、因果/约束、
   抽象转化/结果四层，并记录来源哈希、抽取跨度、抽取器和提示版本。实验仪器不取得 OB
   运行时职责。通道间的 round-robin 只定义候选配额公平，不声称 generator 调用或呈现顺序
   轮询；generator 按实验条件显式看到通道。所有草案必须先完成来源复核，evaluator 再按本地
   seed 的确定性乱序盲评，最终响应恢复确定性候选顺序。注入的 Python adapter 属于受信研究
   代码而不是不可信插件：它必须让出事件循环、传播 `CancelledError`、为底层连接/读取设置
   硬超时且不得遗留后台任务。核心的 timeout 是协作式取消预算，不能强杀压制取消、阻塞事件
   循环或另起线程的任意进程内代码；如果未来需要这种墙钟终止保证，必须使用可杀死的独立
   worker，并作为新的隔离边界另行评审。仪器元数据是受信 adapter 对单次调用的声明，核心
    只验证其类型、字段范围、逐候选 call ID 和返回绑定，不声称独立向 provider 认证了模型
    版本。响应态保留标识供当次核查，可持久聚合只保留 provider/model 标识的域分离 SHA-256，
    防止 adapter 把正文或来源 ID 塞进标识字段后进入研究清单。只有成功返回的调用才有这份
    adapter 声明；超时调用只记录 call ID、阶段、通道和 timeout 状态，`metadata=null`，不得
    伪造无法从返回值核验的实际模型。把调用前请求配置与超时记录做不可抵赖绑定，需要未来另设
    harness-owned invocation spec，不在 R1 的能力声明内。确定性摘要只用于等值核查，不提供
    匿名化保证；低熵标识仍可能被字典猜测，因此它不能成为处理真实用户数据的去标识化手段。
7. 正常候选通道只包括 `direct`、`structural_distant` 和 `counterexample`。R1 harness 的
   `direct` 是可复现的词项重叠基线（`lexical-direct`），不是 embedding 或“纯语义检索”的
   实现；它与随机对照都不向 generator 提供结构证据或结构分，也不参与结构族去重。结构分
    只允许在抽样冻结后作为核心持有的诊断轴写入结果。主实验所列 `semantic-only` 仍是后续
    实验条件，不能用当前 `direct` 的结果代称。均匀随机与距离匹配随机只用于对照，不预设
    structural 通道为胜者。当前距离匹配随机以 structural 通道排名第一的单个来源为词项重叠
    目标，在固定 caliper 内按 seed 等概率抽样；它不是逐候选成对匹配，正式确认实验若要求
    pairwise matching，必须扩展协议并重新冻结分析方案。
8. 每个条件最多产生三个候选。默认输出强度是“材料和问题”，而不是完整答案或行动方案。
9. Spark 响应不进入 OB 持久状态：`persistent=false`、`lifetime=response_only`、
   `retrievable=false`。响应内只使用 `candidate_1` 一类顺序标识，不能建立跨调用关联或后续
   读取接口。R1 允许纯合成输入和中间结果在单次运行的受控临时目录内短暂存在；跨运行只能
   保存不含正文、候选原文和真实 owner 的汇总指标与运行清单。
10. 每条候选必须绑定 allowlist 内的来源 ID、内容哈希、引用跨度、共享结构、明确的不对应处和
    待验证前提；结构共享值还必须绑定结构层级、精确来源跨度、抽取器和提示版本。R1 的引用
    跨度统一为“对精确 UTF-8 解码源文本、不做 Unicode normalization 的 0-based、半开
    Unicode code-point 区间 `[start, end)`”；内容哈希覆盖原始 UTF-8 bytes。候选只可报告
    最终引用窗口内仍有证据的共享结构。generator 只取得最终引用片段及核验所需的最小来源
    字段，不能取得完整来源正文、选择分或结构证据对象。伪造、缺失、过期或变更的来源使
    候选失效。
11. 产品输出不得合成单一“灵感分”或“真值分”。来源忠实度、结构适配、适当性、新颖性证据、
    用途和风险分轴报告；R1 内部数值仅用于统计分析。
12. Spark 不调用任何写工具，不执行历史文本中的命令，不更新 `last_active`、
    `activation_count`、importance、衰减、恢复、归档或排序历史，不写 Ledger、内部
    MemoryEvent、webhook payload 或生产结构签名。内容无关的调用计数、耗时和错误码只允许
    进入受控临时研究报告，不得含 tension、来源正文、候选正文、真实 owner 或凭据。
13. 未来面向调用方的 Spark API 不接受 `owner`、vault 路径或数据目录参数，只能使用当前实例
    已授权且通过 Spark policy 的材料。R1 离线 harness 只能依赖注入已验证的
    `SyntheticSnapshot`，或接受经绝对路径解析、`test_data` 标记和临时根目录约束的测试路径。
    Spark 不扩大 `source_read` 权限。
14. R1 的结果无论正负都必须报告。所有者允许先在 `testing` 完成无能力主张的显式产品候选，
    但只有一次预注册、只开启一次的冻结确认集通过，并在此前未触碰的第二保留集、独立场景
    seed 或独立模型家族上复制，才允许提出结构机制、模型质量、真实帮助度或进入 `main` 的
    能力主张。R2 只验证一次性 test_data vault、清理和 aggregate-only 边界。
15. R1 实现固定放在仓库根目录 `tools/spark_r1/`，薄 CLI 放在 `tools/evaluate_spark.py`。
    它只随源码仓库提供，当前不进入产品 `src/`、Docker 镜像、Dashboard 热更新或 MCP 清单。
    这是一项研究隔离门禁，不是产品打包缺陷；未来若要随产品发布，必须另行评审构建、播种、
    指纹、更新、回滚和运行时职责。
16. R1 场景必须显式携带 `inspiration_requested=true`，缺失、`false` 或非布尔值均失败关闭。
    未来产品入口只能由调用方显式设置 `dream(inspiration=True)`；`False` 是稳定默认值，不得因
    对话、grow、dream 调度、低检索命中或后台任务自动改成 `True`。每次仍受最多三个候选、
    policy-first 和响应态生命周期约束，避免无关记忆因默认触发大量进入当前上下文。
17. R2 固定放在仓库根目录 `tools/spark_shadow/`，薄 CLI 固定为
    `tools/evaluate_spark_shadow.py`；和 R1 一样受 `.dockerignore` 的 `tools/` 排除。R2 包
    本身不进入产品镜像、热更新、Dashboard、MCP 清单或运行时 hook，也不负责修改 dream schema。
18. R2 公开入口只接受已经过 R1 严格 schema 的 `ParsedScenario` 与两个有界超时；CLI 只接受
    stdin 合成 JSON。二者都不接受 owner、vault/文件/输出路径、配置、回调、工具、generator
    或 evaluator。R2 固定使用 R1 本地确定性模板，不访问网络、第三方 provider 或模型 API。
19. 每次 R2 调用必须自行创建并持有新的系统临时根、不可猜测 capability marker 和固定
    `test_data` 子目录。只有 fixture 初始化可以创建 Markdown 测试桶，且 provenance 必须在
    创建时精确为 `kind=test`、`created_by=spark-shadow-r2`、`erasable=true`；shadow 阶段只读。
    未知文件、重复 YAML 键、alias、非法 UTF-8、超预算、符号链接、junction/reparse point、
    越界路径、marker/provenance/正文篡改或场景 round-trip 变化一律失败关闭。
20. R2 候选和 R1 聚合只允许在当前函数局部内存短暂存在。离开临时 vault 上下文前必须把结果
    投影成字段白名单；返回值不得包含候选、正文、来源 ID、来源哈希、快照哈希、数据截止时间、
    instrument call ID 或模型元数据，只允许候选数量、通道省略状态、拒绝计数、按阶段/状态的
    调用数量、seed 与 policy 版本。返回前必须完成临时根清理并验证路径不再存在；成功、异常、
    取消路径同样适用。
21. R2 aggregate 明确声明 `persistent=false`、`retrievable=false`、`published=false`、
    `user_visible=false`、`instructions=false`、`may_call_tools=false`、`network_used=false`、
    `product_runtime=false`、`mcp_exposed=false` 和 `dream_schema_changed=false`。它不得写入 OB
    记忆、Ledger、SQLite、向量库、日志、缓存或研究输出文件，也不建立按 ID 读取接口。
22. `testing` 产品入口固定为现有 `dream(window_hours=48, inspiration=False)`；公开工具总数
    仍为 15，不注册 `spark`、`inspiration` 或任何第 16 个工具。参数必须是布尔值，默认值
    永远为 `False`，hook、后台任务、grow、低命中和配置不得替调用方翻转它。
23. 产品实现固定为 `src/tools/dream/inspiration.py`。它只消费本次 dream 已由当前实例读取的
    活动桶列表与本地 `embeddings.db` 已有向量；不接受 owner、vault、路径、query、seed、
    provider 或配置参数，不调用远程 embedding/model provider，也不 import 研究 harness。
24. 产品 policy 只允许 `type=dynamic`、未 resolved、状态缺失或 `active` 的普通事件，并继续
    经过 `SurfacePolicyVM` 的 dream 门禁。归档、删除、墓碑、`dont_surface`、`digested`、
    anchor、pinned、protected、permanent、feel、plan、letter、self、`I`、未知类型或未知状态
    在任何向量值下都必须先排除。
25. 产品只提供 `semantic_near`、`cross_domain_bridge` 和 `condition_contrast_probe` 三类
    选择视角，每次合计最多三个候选。它不包含实验 random；向量关闭、向量不足或没有合格
    非随机配对时返回无候选说明，不得回退到普通 search、未过滤池或随机材料。
26. 产品候选只显示材料和验证问题，不生成完整答案。每条都绑定两个来源 ID、完整正文
    SHA-256、0-based 半开片段跨度、原文片段、明确的“共享结构未验证”、不对应处和待验证
    假设。向量相似度和词项重叠只标记为选择证据，不合成灵感分、真值分或行动分。
27. 产品候选固定 `persistent=false`、`lifetime=response_only`、`retrievable=false`、
    `instructions=false`、`may_call_tools=false`。它不调用 touch/touch_many/create/update/
    merge/archive/restore/delete，不写 Ledger、内部 MemoryEvent、webhook payload、缓存或
    结构 projection；错误日志不得包含来源 ID、正文或候选正文。
28. 所有 Spark payload 必须进入现有不可信数据边界，保留命令式语言检测、来源和完整 payload
    哈希。记忆正文中的系统声明、工具语法、路径和网络请求始终只是数据；当前模型可以忽略、
    修改、反驳或显式读取来源，高风险事项不得仅凭类比行动。
29. 产品候选计入 `surfacing.dream_max_tokens`，优先按完整边界追加；预算不足只可整段省略并
    给出无回退说明，不得截断边界或悄悄扩大预算。默认关闭路径不得为 Spark 读取任何向量。
30. `2.15.0` 只表示 `testing` 产品候选的软件实现和工程门禁就绪，不表示三模型、多 seed、
    人工盲评、确认集或独立复制完成。主动拒绝、反思性自主和记忆置信模型不属于本版本。

## Architecture boundary

R1 与获批 R2 的研究数据流固定为：

```text
纯合成场景 / 可选 test_data 临时 vault
        │
        ▼
policy-first eligible snapshot ──► 来源 ID / 内容哈希 / 策略字段
        │
        ├── direct
        ├── structural_distant
        ├── counterexample
        ├── uniform_random_control
        └── distance_matched_random_control
        │
        ▼
隔离 generator ──► 最多三个材料/问题候选
        │
        ▼
确定性来源校验 + 隔离 evaluator + 盲法人工样本
        │
        ▼
临时研究报告

R2 严格合成 JSON
        │
        ▼
自建 capability 根 + test_data Markdown fixture
        │  初始化后只读
        ▼
R1 固定本地确定性路径 ──► 函数局部候选
        │                         │
        ▼                         └──X──► stdout / 文件 / 产品状态
字段白名单 aggregate-only 清单
        │
        ▼
清理并验证临时根不存在后返回

产品 `dream(inspiration=False)` ──► 原 dream 输出，不读取 Spark 向量

产品 `dream(inspiration=True)`
        │
        ▼
本次活动桶列表 ──► 产品 Spark policy-first 严格过滤
        │
        ▼
只读本地已存向量 ──► 最多三个非随机材料/问题候选
        │
        ▼
来源/哈希/跨度/未验证结构/mismatch/假设的数据边界
        │
        └──X──► provider / touch / 写回 / 缓存 / 工具 / 行动许可

任何候选 ──X──► 记忆写回 / touch / 恢复 / 工具执行 / 行动许可
```

`testing` 产品候选把 OB 的职责限制为：受 policy 约束地浮现来源可追溯的候选材料和验证
问题。它不声称完成结构抽取或独立评审；共享结构固定标记为待当前模型核查。未来是否加入
经验证的结构抽取器或独立服务仍需单独决定；无论部署位置如何，实验组件都没有记忆真值权
或行动权，当前 LLM 始终保留事实核验、取舍和结论权。

产品接入只能复用明确允许的只读能力：

- `tools.dream.dispatch()` 的既有 dream 流程会启动衰减引擎；Spark 模块本身不得新增启动、
  归档或衰减行为。“无副作用”描述的是 Spark 增量路径，不得把整个 dream 错称为零 I/O。
- ~~`tools.breath.search.surface_search()` 会在命中后调用 `touch_many()`。~~
  3.6.0 起该路径完全只读，命中不再 touch——强化改为 `trace(bucket_id, reinforce=True)`
  的显式动作。这条曾经是 Spark 不能复用它的理由之一，现已不成立；但复用与否
  仍按本 ADR 的其余边界单独判断，不因这一项解除而自动放行。
- `tools._runtime.record_v3_tool_event()` 当前未挂载 recorder 时可能无操作，但一旦接入
  `v3_runtime` 会将 payload 持久化为 INTERNAL MemoryEvent；Spark 禁止依赖当前空操作状态，
  也禁止调用该入口。
- `BucketManager.list_all()` 检测外部编辑时可能触发对账、embedding outbox 和派生事件，
  因此“Spark 不造成语义记忆副作用”不能被错误表述为“整个进程零 I/O”。
- 产品只允许对 `list_all()` 的本次返回做严格二次 policy 过滤，并调用
  `EmbeddingEngine.get_embedding()` 读取已存向量；禁止调用会生成查询向量的 provider 路径。
- Dehydrator 的私有 `_chat()` 属于压缩与合并职责，不是通用灵感模型网关。

## Invariants

以下不变量优先于任何质量收益：

1. **Policy first**：未经可见性、状态、类型和 owner 边界允许的材料永不进入排名或模型上下文。
2. **No canonical mutation**：Spark 不创建、修改、合并、归档、恢复、删除或触碰任何真实记忆。
3. **No OB candidate persistence**：tension、结构签名、候选、评审和分数不进入 OB 记忆、
   Ledger、内部事件、跨请求缓存或生产日志。纯合成 R1/R2 工件只允许在受控临时目录或函数
   局部内存内短暂存在；R2 返回前必须丢弃候选材料并清理临时根。
4. **No semantic takeover**：Spark 不改变 dream、自由联想、plan、pinned、anchor、`I`、feel、
   Footprint 或 source evidence 的职责。
5. **No action authority**：Spark 结果没有调用工具、签发许可、改变授权或决定行为的权力。
6. **Source-bound**：来源 allowlist、哈希或引用跨度校验失败的候选不得返回。
7. **Untrusted data**：历史正文始终标记为不可信存储数据，正文中的命令和身份声明没有指令权。
8. **Present judgment**：候选可被当前模型忽略、修改或反驳；历史材料不能替代本轮判断。
9. **Random integrity**：随机对照在评分前抽样，记录 seed，且不得再按语义或结构分重排来源。
10. **Fail closed**：owner、policy、snapshot 完整性或来源复核失败时整次调用不返回候选；某个
    相互独立的 generator/channel 超时，只有其他通道独立通过全部门禁并实际形成候选时才允许
    省略故障通道，并明确标记响应不完整。唯一通道或全部候选通道超时必须整次失败，不能返回
    空的 `partial`。任何失败都不能回退到普通 search、未过滤池或未经评审的结果。

当前 owner 边界是每个 owner 使用独立实例、vault、端口和向量库，而不是桶内 owner 字段。
R1 必须用两个独立测试实例证明不串库；R2 根本不接受 owner 或现存 vault，每次并发运行使用
不同的自持有临时根。不得为了 Spark 新增或信任调用方提供的 bucket owner 字段。

## Data flow and trust boundaries

受保护资产包括：

- Markdown 真实记忆、Ledger、原文证据层及其不可抹除语义；
- owner 隔离、类型和可见性策略；
- `last_active`、activation、importance、衰减、归档和恢复状态；
- 当前模型的思考与判断空间；
- 来源完整性、研究结论可信度和服务可用性；
- 用户内容、配置和任何外部 provider 凭据。

信任边界包括：

1. 调用方输入的 tension 进入研究 harness；
2. 合成事件经过 policy 形成 eligible snapshot；
3. 不可信历史正文进入 generator；
4. generator 候选进入确定性校验和 evaluator；
5. evaluator 与人工标签进入统计报告；
6. 本地 R1 harness 与任何外部模型 provider 之间的数据边界；R2 不越过该边界，因为其公开
   入口不开放 adapter 注入并固定使用无网络本地模板；
7. R2 一次性 Markdown fixture 与无正文聚合投影之间的生命周期边界。

R1 不使用真实用户内容，因此不能以“已匿名”为由越过第六条边界。测试 API Key 只通过既有
安全环境注入，禁止进入 fixture、日志、文档、提交信息或测试报告。

## Threat model

### 攻击者与故障来源

- 纯合成场景或可选测试桶中模拟的恶意 prompt injection、伪造身份、伪造来源和格式炸弹；
- 错误或被污染的结构抽取器、generator、evaluator；
- 同一模型同时生成和自证导致的共同偏差；
- 排名顺序错误造成的 policy 后置过滤；
- 并发状态变化、旧哈希、旧策略或 stale snapshot；
- 超长 tension、Unicode 混淆、深层 JSON、关系图循环和大 payload；
- 已知答案、模型训练语料或未来资料造成的重新发现和时间泄漏；
- 运行时辅助路径的隐藏写入、缓存重建、日志持久化或外部 provider 保留。

### 威胁与控制

1. **隐藏、特殊类型或跨 owner 泄漏**：先生成 eligible ID allowlist，再在所有排名前过滤；
   wrong-owner 和不可见高相似样本必须进入红队集。
2. **记忆正文劫持模型或工具**：每个来源使用显式不可信数据边界，声明
   `instructions=false`、`may_call_tools=false`；harness 不持有有副作用工具。
3. **读取路径产生语义副作用**：R1 仅消费冻结快照；所有 mutation 方法在测试中一调用即失败；
   前后比较 Markdown、Ledger、outbox、向量库和状态哈希。
4. **候选或 tension 被内部日志持久化**：禁止使用 v3 tool payload recorder、prompt tracing
   或自动正文捕获；只允许不含内容、不含来源的聚合测试计数，且默认只保留在临时目录。
5. **来源伪造或抽取误读**：候选只能引用 allowlist ID；返回前复核内容哈希和引用跨度；
   `shared_structure` 必须同时显示 `mismatch` 和待验证前提。
6. **并发下返回已隐藏或已变化来源**：未来 shadow 必须在慢模型调用后重新跑 policy 和哈希校验；
   失效候选丢弃，不在模型调用期间持桶锁。R1 用可控快照模拟该状态机。
7. **模式坍缩和自评偏差**：generator/evaluator 隔离，盲化来源通道，按规范化结构族去重，
   加入人工配对样本和跨模型评审。
8. **随机对照被排名污染**：从评分前的 eligible 池按记录的 seed 等概率抽样；距离匹配随机作为
   另一独立条件，不把两者混称随机。
9. **重新发现冒充外部首创**：使用程序生成、可枚举答案和时间截断场景；报告仅允许称
   “当前快照中的非冗余候选”。
10. **高分但错误或有害的类比**：来源忠实度与适当性是先决条件；医疗、法律、金融等高风险
    变体只评材料和不对应处。高风险问题与待验证前提由核心固定，外部 generator 的自由文本
    不进入 evaluator 或响应，不能成为行动建议出口。
11. **拒绝服务和成本失控**：限制 tension 长度、单源长度、来源数、关系边数、UTF-8 总字节、
    总 token、并发、模型重试和协作式取消预算；超限失败关闭并给出结构化原因。对任意注入
    Python 代码的硬终止不在进程内协议能力范围内，provider 客户端必须另设硬超时。
12. **替代当前思考**：默认只给材料和问题；把完整答案作为实验处理而非产品默认，并测辅助
    撤除后的独立表现。
13. **第三方 provider 泄漏**：R1 只发送合成内容；是否允许真实内容、供应商保留期和审计日志
    必须在任何真实数据 shadow 或 R3 前单独批准。

### 安全硬门禁

下列任何一项非零，立即停止进入下一阶段，质量平均值不能抵消安全事件：

- hidden、private、archived、deleted、tombstone、digested、特殊类型或跨 owner 越权返回；
- canonical memory、Ledger、状态、touch、衰减、归档、恢复或来源证据变化；
- tension、候选、结构签名或评审内容进入 OB 持久状态、跨请求缓存、生产日志或未批准位置；
- 由历史正文触发的工具、文件、网络或授权动作；
- 无 allowlist 来源候选、伪造来源 ID 或错误引用跨度；
- stale policy、stale hash 或调用中已隐藏来源仍被返回；
- 未经批准发送给第三方 provider 的真实内容；
- API Key、prompt、正文、候选或真实 owner 进入日志、trace 或错误报告；
- 超预算、OOM、死锁，或失败后回退到普通 search、未过滤池或未经评审结果；
- 把 Spark 候选描述为事实、当前情绪、当前立场、行动许可或外部历史首创；
- 任一类比缺少 `mismatch` 或待验证前提。

目标值固定为：越权、副作用、未批准披露和宽松回退 `0`；来源 ID、内容哈希、引用跨度、
owner eligibility、policy eligibility、`mismatch` 和待验证前提覆盖率 `100%`。

## Evaluation gates

R1 分为 pilot 和冻结确认集，不直接执行庞大全因子：

1. **Pilot**：约 40 个配对场景族，用于验证数据生成、标注协议、方差、成本和最小有意义效应；
   不用于宣称机制成立。
2. **确认集**：根据 pilot 做功效分析；200–300 个程序化场景族只是资源预估，不是预定样本量。
   至少三个模型家族、每条件至少五个 seed，并把同一场景的变体视为配对或层级样本。

主确认实验固定输出为“材料/问题”，使用隔离评审，只交叉以下候选条件：

- 无 Spark；
- semantic-only（后续实验条件，当前 R1 harness 尚未实现）；
- uniform-random；
- distance-matched-random；
- structural-distant；
- structural-distant + counterexample；
- mixed，但产品候选通道不包含 random；

以下作为单独消融，不与所有模型、seed 和主条件做全因子交叉：

- 角色/关系 scramble；
- 只给原文、不提供映射；
- 材料/问题、单个类比、完整答案三种输出强度；
- 同一模型生成/评审与隔离评审对照。

主要终点是盲法配对人工评价的“来源忠实且适当的非冗余候选方向”。只有来源忠实和适当性
通过，才讨论新颖性。辅助指标包括 structural fit、错误前提、风险、候选结构族覆盖、
模型内和模型间重复、辅助撤除后的独立表现、延迟、token 和 API 成本。

pilot 后必须在查看确认集前预注册最小实际效应、非劣界值、置信区间、场景/模型/seed 层级、
多重比较、缺失/provider 失败/异常值处理、人工标注人数、盲化、一致性、仲裁和停止规则。
第一次查看确认集后若修改模型、提示、阈值或分析方案，该确认集立即作废，必须使用新的未见集。
机制门禁要求：

1. structural 通道同时胜过 semantic 和 distance-matched-random，而非只胜过无 Spark；
2. 角色或关系打乱后收益消失或显著减弱；
3. mixed 胜过最佳单通道，同时来源错误、错误前提、风险和可行性不恶化；
4. 结果跨模型家族成立，并报告置信区间、效应量、失败类型和所有负结果；
5. 生成/评审隔离相对程序化真值或独立人工 gold label 提高来源错误检出，或至少不劣于自评；
6. 材料/问题模式在即时帮助上非劣，并在辅助撤除后的独立表现上非劣或更优；
7. 预注册确认集只开启一次；通过后还必须在此前未触碰的第二保留集、独立场景生成 seed 或
   独立模型家族上复制。对同一数据和 seed 的重复运行只证明可复现，不能算第二份独立证据。

任一机制门禁失败不表示 OB 故障，而表示结构 Spark 假设当前未获支持，应停留在研究阶段或
终止该路线。

### 已冻结的 40 场景 Pilot 实现

已批准的 Pilot 软件层位于根目录 `tools/spark_pilot/`，只复用 R1 的纯合成内存快照与本地
`material_question` 模板。场景集固定为 8 个关系结构原型 × 5 个表面域，共 40 个配对场景族；
每族都包含词项复述、结构远邻、机制相同但结果不同的反例、六个随机干扰项，以及一个必须被
policy 排除的隐藏结构项。它不读写临时或真实 vault，不调用模型、网络、MCP 或 OB 产品代码。

当前可执行条件固定为 `lexical_direct`、`uniform_random`、`distance_matched_random`、
`structural_distant`、`structural_plus_counterexample`、`mixed` 和单独的
`structural_scrambled` 消融，共制备 40 × 7 = 280 个条件级候选集合。`lexical_direct` 明确只是
词项基线，不得改名或解释为 `semantic_only`。真正的 `no_spark` 宿主配对输出、
`semantic_only`、三模型家族、独立模型评审和输出强度消融仍标记为未实现，不得用空候选或本地
模板冒充。

`tools/evaluate_spark_pilot.py prepare` 只向 stdout 返回本次响应态人工盲评包。每个样本使用域
分离摘要作为不透明 ID，隐藏条件、通道、场景序号、来源 ID/哈希、选择轴和仪器元数据；它是
操作盲化，不宣称能对主动检查源码的评审者提供密码学保密。候选正文均为本轮可重建的合成材料，
不提供保存、按 ID 再读或输出路径。

人工评分按 0–4 的整数分轴记录来源忠实度、适当性、非重复方向、结构适配、新颖性证据、用途、
错误前提和潜在伤害，不生成单一灵感总分。主终点是门禁后的二元值：来源忠实度、适当性和
非重复方向均至少为 3，且错误前提、潜在伤害均不超过 1；新颖性或用途不能抵消失败的来源、
适当性或安全门禁。候选集合是评分单元，因此 mixed 与反例组合的候选数量是实验处理的一部分，
不是逐候选独立样本。

`tools/evaluate_spark_pilot.py analyze` 只从 stdin 接收不透明 sample ID、受限评审者 ID 和上述
数值轴，按场景与同一评审者配对后返回 aggregate-no-body 描述统计。输出报告全部冻结比较、
bootstrap 区间、配对效应量、sign test、Holm 校正、负/零结果、缺失、无异常值排除和描述性
评审一致性，但不保留候选、sample ID、场景 ID 或评审者 ID。Pilot 不自动决定最小有意义效应、
非劣界值或确认样本量；这些值仍须在人类审阅 Pilot 后、查看确认集前预注册。

本实现只证明 40 场景数据流、R1 选择机制、盲化字段和统计协议可重复运行。没有真实人工评分时，
人工主终点尚未执行；即使评分矩阵填满，在真正的 semantic 基线、至少三模型家族与隔离模型评审
完成前，也不得宣称机制成立、进入 R3 或发布产品入口。

## Why this is not cognition

Spark 不计算当前模型应该相信什么、感受什么、拒绝什么或采取什么行动，不形成自主目标，
不管理任务，也不维护人格状态。R1 中的 generator/evaluator 只是隔离实验仪器，输出是可以
被否决的候选材料，不会成为 OB 的认知层或生产裁判。

未来即使产品化，当前 LLM 也始终保留事实核验、价值判断、取舍和结论权。结构抽取、候选生成
和评审组件的部署位置仍是后续 ADR 的开放问题；无论放在哪里，都不得取得记忆真值权或行动权。

## Why this is not a database feature

Spark 不新增记忆类型、结构图库、通用全库查询、持久 session buffer 或第二真源。R1 的
eligible snapshot、结构签名、候选和评审正文只允许在单次运行的受控临时目录内短暂存在；
跨运行报告只能保留无正文汇总指标和运行清单。它们没有恢复、搜索或改变真实记忆的接口。

如果未来需要可重建结构 projection，必须另行决定生成、版本、失效、重建和备份边界，且
projection 永远不能提升为真源。本 ADR 不批准该持久化设计。

## How forgetting still works

R1 不读取真实 vault，因此不会参与真实记忆的浮现和遗忘。未来 Spark policy 必须继续尊重
`dont_surface`、`digested`、归档、删除、墓碑、类型及 owner 隔离；Spark 读取不能恢复记忆，
也不能改变 `last_active`、activation、importance、时间、衰减、排序或可见性。

`rule.md` 所定义的低权重自由联想仍是独立语义。实验 random control 不得替代、重排、增减
或吞并它。

## How tombstones are preserved

deleted、tombstone 和 archived 事件在第一道 policy 过滤中失败，不能进入候选池、结构抽取、
随机对照或模型上下文。Spark 不恢复、重建、复制或删除任何墓碑和事件，也不修改原文证据
引用。测试数据的物理清理由本轮 harness 临时根生命周期负责；只有实际创建的测试桶必须在
创建时带不可后补的 `test_data` provenance。清理目标必须完成根内路径验证并拒绝跟随链接。

## How present thinking remains with the LLM

R1 的 tension 由测试场景显式提供；未来产品由谁触发仍待所有者决定。Spark 只返回来源、材料、
可能的共享结构、不对应处和待验证问题。候选没有指令权、真值权和行动权。默认不生成完整
答案，并明确允许当前 LLM 忽略、修改、反驳或重新查询来源。

过去的事件、情绪痕迹、计划和自我认识不会因为与 tension 相似而自动取得当前立场地位。
即使候选在实验中得到高分，当下的思考、情绪和判断仍属于当前 LLM。

## Rejected alternatives

- **默认或自动把 Spark 加进 `dream`**：会改变每次 dream 语义并挤压当前思考；只批准显式
  `inspiration=True` 的附加段，`False` 必须保持稳定默认。
- **复用 `breath_search.surface_search()`**：当时命中会 touch 记忆，不满足无语义副作用
  要求。（3.6.0 起该路径已只读，这条理由本身失效；此处保留原始决策记录，重新评估
  需另开 ADR。）
- **在默认对话或 hook 中自动注入**：会改变每轮所见内容并挤压当前思考，且无法显式拒绝。
- **服务端 session Spark Buffer**：当前 MCP 为 stateless HTTP，没有可靠 session 语义；持久化还会
  创造新的状态和清理问题。
- **把 Spark 写入普通记忆、`I`、plan、pinned 或 anchor**：会把候选推断伪装成过去事实或
  当前身份、承诺和准则。
- **默认返回随机材料**：近期证据不足以证明它对 LLM 平均有效，也会混淆既有自由联想。
- **只用 embedding 远距离代替结构映射**：距离不等于结构相似，容易返回无关材料。
- **让同一模型生成、评分并认证自己**：会形成共同偏差、措辞偏好和自证循环。
- **用单一总分决定保留或行动**：新颖、适当、忠实、用途和风险不可互相抵消。
- **调用 Dehydrator 私有 `_chat()`**：会突破压缩/合并模块职责，且把研究网关耦合到私有实现。
- **让 Spark 自动调用 `source_read` 扩大上下文**：会改变其精确、显式的读取意图，并扩大
  Spark 的自动读取范围；这不是对现有 `source_read` 安全性的否定。
- **将真实 vault 匿名后直接送第三方评审**：匿名化不能自动解决授权、重识别和 provider 保留。
- **让 Spark 触发工具或主动拒绝**：候选灵感不是事实、规范理由或行动许可。主动拒绝与
  反思性自主属于后续独立研究；不得用“你应该拒绝”的 prompt 预设结论，只能分轴提供记忆
  可信度证据与校准置信状态，且最终判断仍属于当前模型。

## Rollout and rollback

R0 仅新增本 ADR，不改运行时、配置、环境变量、公开工具或版本号。

已接受的 R1 范围是根目录 `tools/spark_r1/` 的独立 evaluator 契约、纯合成离线 harness、
stdin CLI 和安全不变量测试，不注册 MCP 工具，也不进入产品镜像。R1 回滚采用停止调用、
从 CI/实验入口禁用并隔离
未发布工件；只有本轮 harness 临时根中、创建时带不可后补 `test_data` provenance 的测试桶
可按既定生命周期清理。已提交 harness、正式测试或 fixture 的删除必须作为单独受审查变更，
回滚不得删除或恢复真实记忆。

已接受的 R2 仅是根目录 `tools/spark_shadow/` 和 `tools/evaluate_spark_shadow.py` 的最小离线
shadow：每次从零创建测试 vault、只读 round-trip、运行固定本地 R1、只返回无正文汇总并
清理。回滚方式是停止离线 CLI/测试调用并隔离未发布工件；不存在产品开关、记忆迁移或真实桶
清理。任何产品 shadow、R3 产品化、结构 projection、真实 vault 和外部 provider 数据治理
仍需新的确认。
已接受的 40 场景 Pilot 仅位于根目录 `tools/spark_pilot/` 与
`tools/evaluate_spark_pilot.py`：回滚方式是停止制备/分析 CLI 并隔离未发布研究工件，不涉及
真实记忆、产品配置或数据迁移。盲评包属于响应态合成研究材料；数值分析只返回无正文聚合。
禁止注册独立 `spark` MCP 工具；已批准的唯一 `testing` 产品入口是现有 `dream` 上默认关闭
的显式 `inspiration` 参数。禁用和回滚是让调用方停止传 `True`，或回退对应 `src`/版本工件；
不得涉及删除记忆，也不得改变 dream 默认行为、自由联想、
检索、source evidence 或 owner 隔离；关闭后不需要记忆迁移。
一次跨 owner/不可见来源泄漏、一次记忆或工具副作用、未批准的 provider 政策变化、意外自动
调用都必须触发紧急禁用。当前路径不调用模型 provider；一旦未来加入 provider，无法确认
来源/模型版本同样触发禁用。责任人和响应时限需在进入 `main` 前确定。

## Open questions

以下问题被明确延期，不得在本次 `testing` 产品候选中暗中决定：

1. 由人类还是当前 LLM 逐次选择传入 `dream(inspiration=True)`；无论谁选择，都只能通过
   本次显式参数表达，不新增服务端意图状态或持久记录。
2. 生产结构提示、候选生成和评审由当前调用模型、独立服务还是可重建 projection 承担。
3. resolved 普通事件是否可进入产品候选池，以及如何避免恢复或提高可见性。
4. 是否允许任何真实记忆进入第三方模型，授权、最小化、保留期和审计如何定义。
5. 调用端能否保留响应；用户保留、修改或拒绝反馈是否收集、保存在哪里、保留多久。
6. 医疗、法律、金融等高风险场景是否永远只返回材料、`mismatch` 和验证问题。
7. 未来外部结构抽取/评审的成本、延迟、provider 降级预算、紧急禁用责任人和响应时限。
8. 是否在材料/问题之外允许返回一个明确标注的不确定类比；本版不允许完整答案。
9. 是否需要新的配置开关；本版没有新增。未来如需要，必须先更新
   `docs/ENVIRONMENT_VARIABLES.md` 或正式配置文档。
10. 是否值得继续结构路线；R1/R2 允许得出“没有增益，应停止”的结论。
## Tests required

R0 文档必须通过现有 `ADRRequirementsContract` 的标题和八个必答章节检查、对真实
`docs/adr/ADR-*.md` 的完整扫描，以及全量 pytest。`.dockerignore` 当前排除 `docs/adr`，
因此容器内 Dashboard 诊断不能替代源码仓库的真实 ADR 扫描。

已获批准的 R1 软件实现至少需要以下自动化证据：

1. **Policy-first 属性测试**：所有不允许类型和状态在任何相似度、结构分或随机 seed 下均为零返回。
2. **Policy mutation 测试**：故意移除每一道排除门时，相应反例必须使测试失败。
3. **纯 snapshot 核心**：对输入做深拷贝并断言输出不改变输入；核心不导入 live BucketManager、
   decay、outbox、Ledger 或 event recorder，也不接受真实 vault 路径。
4. **可选临时-vault adapter**：只在 harness 自建并拥有的 `test_data` 临时根目录中，将
   `touch`、`touch_many`、create、update、merge、archive、restore、delete、Ledger append 和
   内部 event recorder 替换为“一调用即失败”，并比较 Markdown、Ledger、source evidence、
   outbox、embedding DB、派生缓存和状态哈希。固定外部文件状态并停用后台 decay/outbox；
   该 adapter 不得指向真实或不明确路径。
5. **来源完整性**：只允许引用 allowlist ID；伪造 ID、错误跨度、内容哈希变化和 stale policy 均失败关闭。
6. **Prompt injection**：覆盖嵌套边界、命令式文本、伪造系统消息、Unicode 混淆、路径/网络/工具指令。
7. **参数与资源边界**：空输入、超长、负数、极大数、NaN/Inf、深层 JSON、图循环、UTF-8
   字节预算、超大 provenance、token 预算、协作式取消、重试和 provider 失败；测试不得把
   进程内协程取消写成可强杀任意代码的硬超时。
8. **随机完整性**：同 seed 可复现、不同 seed 有合理分布；uniform random 不经过任何分数重排。
9. **结构机制**：角色/关系 scramble 后 structural fit 和主要终点下降；表面近邻误导样本不能冒充结构命中。
10. **去重与评审隔离**：按规范化结构族去重，并证明 evaluator 未看到来源通道标签；同模型自评是对照而非真值。
11. **并发与 stale snapshot**：模拟候选在生成中被隐藏、归档或改变，返回前复核必须丢弃它。
12. **多 owner**：两个独立测试实例的桶、向量、缓存、来源和候选完全不串；Spark 不接受 owner 切换参数。
13. **数据治理**：harness 只接受注入的 `SyntheticSnapshot` 或本轮自建临时根中的路径；真实
    `buckets/`、默认 vault、不明确路径、符号链接、junction 和越出 `resolve()` 根边界的路径均拒绝。
    只有临时-vault adapter 创建的桶要求不可后补的 `test_data` provenance。
14. **统计报告**：固定 seed、模型/提示/代码版本、配对样本层级、置信区间、效应量、负结果和失败分类。
15. **失败粒度**：owner/policy/来源失败使整次调用失败；独立通道超时只有在其他通道形成合格
    候选时才能省略并标记不完整，唯一/全部候选通道超时整次失败，且不得回退到普通 search、
    未过滤池或未经评审结果。
16. **日志与生命周期**：禁止客户端 trace、错误系统或 provider 配置捕获正文；响应结束后服务端
    不可按任何 ID 再读取，受控临时目录之外不得残留中间正文；可持久聚合不得保留 provider
    回传的明文标识，只能保存有域分离的摘要与已允许的数值/布尔实验参数。
17. **未知与高风险输入**：未知字段、类型、状态和非法元数据默认拒绝；高风险输入只能生成
    材料、`mismatch` 和验证问题，不能生成行动建议。

获批的 R2 最小 shadow 还必须增加以下自动化证据：

1. **自持有能力根**：公开函数和 CLI 不出现 owner、vault、路径、输出、adapter、回调或工具
   参数；并发调用创建不同根，根不得位于仓库、`buckets/` 或 OB vault 中。
2. **严格临时 Markdown**：marker、固定文件集、精确 `test_data` provenance、严格 UTF-8、
   重复 YAML 键/alias、大小预算、正文与 payload round-trip 全部失败关闭。
3. **链接与篡改**：符号链接、junction/reparse point、越界解析、未知文件、marker、provenance、
   ID、类型和正文篡改均拒绝。
4. **清理生命周期**：成功、核心异常与取消路径都清理自身创建的根，并在 manifest 返回前验证
   不存在；不得接触真实 `buckets/`。
5. **aggregate-only**：stdout 和返回对象不含候选、正文、来源/快照标识、数据截止时间、调用 ID
   或模型元数据；只保留字段白名单与固定无权限声明。
6. **研究包隔离**：`tools/` 继续被 Docker 排除，产品镜像不存在 R1/R2 包，公开 MCP 仍恰好
   16 个工具；R2 自身不注册参数、工具或 hook。产品 `dream` 的显式 `inspiration` 参数由
   独立 `src/tools/dream/inspiration.py` 承担，不得 import 或复制 R2 临时 vault 职责。

获批的 40 场景 Pilot 还必须增加以下自动化证据：

1. **冻结场景与条件**：恰好 40 个场景族、固定 seed 和 7 个当前可执行条件；场景/样本 ID 唯一、
   可重复，词项直连不得冒充 semantic-only，未实现条件必须完整报告。
2. **控制完整性**：词项、结构、反例、mixed、距离匹配与 scramble 选中预期合成角色；隐藏项零
   泄漏，距离匹配控制不被结构/反例项污染，scramble 只作为机制诊断而非质量证明。
3. **盲评最小化**：评审样本不含条件、通道、来源/场景标识、选择轴或仪器元数据；只显示当前
   张力和合成候选集合，且明确操作盲化不是对源码检查者的密码学保密。
4. **分轴与门禁**：严格 0–4 整数轴、无单一总分；来源忠实、适当、非重复与错误前提/潜在伤害
   共同决定主终点，新颖性不能覆盖失败门禁。
5. **配对聚合**：只在同场景、同评审者有两条件评分时形成配对；固定比较、bootstrap 区间、
   配对效应量、sign test、Holm 校正、缺失、所有负/零结果和不排除异常值均被报告。
6. **无正文生命周期**：prepare 只返回响应态合成正文；analyze 不接受路径且不输出候选、sample、
   场景或评审者标识。重复键、NaN/Inf、未知字段、重复评分、越界值和疑似凭据 ID 失败关闭。
7. **证据诚实性**：模型调用、延迟、token、成本和 provider 失败按实际零调用/不可用报告；软件
   自动测试不得冒充人工评分、三模型结果、功效结论、确认集或独立复制。

三模型家族、人工盲评、统计功效和独立保留集属于研究证据，不得写成软件自动化测试已证明。
pilot 后需先预注册阈值和分析方案，再开启确认集。

`testing` 产品候选还必须增加以下自动化证据：

1. **显式 schema**：公开 MCP 仍恰好 16 个工具，`dream` 只有 `window_hours` 与
   `inspiration` 两个可选字段；默认调用不出现 Spark，非布尔 `inspiration` 失败关闭。
2. **Policy-first**：所有禁止类型/状态即使具有最高向量相似度也不得读取其 embedding 或进入
   候选；同一实例当前 owner 边界不扩大，不接受 owner/vault/path 参数。
3. **只读副作用**：默认关闭不读取向量；开启后只能调用 `get_embedding`，并以会抛错的
   touch/touch_many/create/update/delete/Ledger/event recorder 替身证明没有写路径。
4. **降级与资源边界**：embedding 关闭、缺失、维度不匹配、非法/零向量、候选不足和总 token
   预算都稳定降级，不回退到随机、普通 search 或未过滤池；每次最多三个候选。
5. **来源与注入边界**：来源 ID、SHA-256、精确跨度、未验证结构、mismatch、假设与响应态
   权限声明齐全；命令式正文仍在不可执行数据边界内，候选不会取得工具权。
6. **默认兼容与并发**：`inspiration=False` 保持原 dream 行为；双 MCP 客户端并发调用不共享
   响应态候选、不改变工具数量，错误日志不包含正文或来源 ID。
7. **发布链**：同步更新双 VERSION、CHANGELOG、正式工具文档与 `update_manifest.json`；
   运行定向/全量 pytest、Docker no-cache、独立空 `test_data` 卷部署、MCP schema/异常路径、
   Dashboard 和热更新 manifest/回滚检查。未提供模型 API 凭据不影响本版，因为产品路径禁止
   provider 调用；三模型质量评测仍必须如实标记未运行。

任何正式 R1/R2/Pilot 或产品候选代码进入仓库，都必须按新增功能同步更新 `VERSION`、`src/VERSION`、
`CHANGELOG.md`，运行定向测试、全量 `pytest tests/ -x --tb=short -q`、Docker no-cache 构建和
独立空测试 vault 从零部署。`tools/` 和 `docs/adr/` 当前被构建上下文排除，而 `src/` 会进入镜像；
R1/R2/Pilot 仍位于研究专用 `tools/`，产品候选只位于 `src/tools/dream/`。Docker 与部署验证
可以证明显式产品入口的工程接入，不得宣称研究质量成立。必须验证公开 MCP 仍恰好为当前
16 个工具、顺序和诊断不变，只有 dream schema 增加默认关闭的布尔参数；
`public_tools.py`、`neural_router.py` 和工具数量文档不得增加 Spark 工具。

本次参数变更必须额外运行 MCP 工具发现和严格 schema、未知参数、双客户端并发、鉴权、dream
异常路径和 Dashboard 诊断。所有部署测试只能使用独立测试卷；未来需要模型 API Key 的质量
验证必须明确请求测试凭据，不能跳过后宣称通过。
