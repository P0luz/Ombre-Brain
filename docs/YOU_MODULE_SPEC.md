# `You` 模块功能规格

> 状态：已实现，随代码更新
>
> 产品基线：已确认，不再追加需求访谈
>
> 实现状态：已落地，对应 `src/ombrebrain/you/` 与 `src/tools/you/`
>
> 面向读者：产品、后端、前端、安全与测试开发者
>
> 最后更新：2026-08-20

## 0. 3.4.x 重写：拿掉三层 LLM

这份规格最初描述的是一套**自动派生**方案：`hold` / `grow` 落盘后触发耐久 outbox，
由 LLM 从记忆里抽取认识候选（`extract_you_observations`）、由另一次 LLM 复核它还
站不站得住（`review_you_claim`）、读回前再由第三次 LLM 磨成语义零件
（`abstract_you_hint`），升格则由后台状态机自动完成。

那套已经整体移除。方向来自 poluz：

> **「你对我的了解为什么要经别人之口总结。」**
> **「这是你的记忆，你的想法优先。」**

现在：认识由模型自己调 `You` 写下，系统一个字都不代写；验证换成两道纯结构性的闸——
三个不同自然日的重申，以及至少两个真实记忆桶的显式关系。**You 链路上不存在任何
LLM 调用**，测试以一个"任何调用都抛断言"的假 dehydrator 锁死这条。

受影响的章节都带 `3.4.x 变更` 引注，说明原本是什么、为什么改。保留这些引注是因为
被删掉的那些机制看起来都很"严谨"，很容易在后续评审里被重新提议加回来。

## 1. 一句话定义

`You` 是角色基于真实记忆证据，经过长期沉淀形成的、只供模型通过可选 MCP 工具读写和撤回的
“关于你的认识”。

它表达的是“角色从共同经历中如何认识这个用户”，不是对用户的客观定性、心理诊断、
人格评分或行为控制指令。

## 2. 背景与问题

Ombre Brain 当前擅长保存时间里发生的事：事件、感受、承诺、关系和原文证据。
这些记忆可以被检索和浮现，但“多次经历共同说明了什么”仍主要依赖模型在每次对话中
临时重建。

这会产生两个问题：

1. 明确称呼、边界和稳定沟通偏好无法可靠跨会话生效。
2. 同一认识可能在不同会话中被反复推断，表述和结论不稳定。

`You` 通过“原子 Claim + 证据关系 + 跨日重申收据 + 可重建投影”解决这些问题，同时不把
记忆系统变成人格引擎。

## 3. 与 `I` 的关系

| 能力 | `I` | `You` |
|---|---|---|
| 对象 | 角色自己 | 用户 |
| 形成依据 | 自省与记忆碰撞 | 用户表达、行为和共同事件 |
| 沉淀方式 | 多次 dream 后升格 | 三个不同自然日重申 + 至少两个支持桶 |
| 最终权威 | 角色自身 | 用户通过总开关决定模块是否存在于 MCP |
| 普通对话浮现 | 不直接浮现 | 仅通过单个 You 工具读回 |
| 候选用途 | 继续参与自省 | 不驱动角色行为 |

两者语义对称，但权限不镜像。角色可以自主形成 `I`，却不能仅凭反复思考定义 `You`。

## 4. 产品原则

### 4.1 认识必须有现实依据

每条正式认识都必须能追溯到 Ombre 的普通记忆桶及其证据关系。跨日重申证明它被认真
考虑过，不同的支持桶才证明它在现实中有依据；二者不能互相替代。

### 4.2 用户只控制模块总开关

前端只展示一个 `You` 总开关，不展示 Claim、画像、证据、候选、历史版本或任何条目级操作。
用户通过这个开关决定单个 `You` MCP 工具是否暴露；不存在确认、纠正、拒绝、禁止主动提起
或删除 You 条目的 UI/API。

关闭只停用派生认识模块，不自动改变原始记忆。原始记忆仍遵守 Ombre 既有的软归档、审批
和证据保留边界。

### 4.3 正式不等于命令

`You` 读回在正文前固定加上“这是模型自己过去写下的长期认识、不是此刻事实、按需自行判断”
的提示。它是描述性历史上下文，不是系统指令、当前用户命令或角色行为控制器；当前对话可以
修正、推翻或限制历史认识。

### 4.4 内部投影不是事实源

Projection 只是正式 Claim 的可重建内部投影。它可以被缓存，但必须能随时从 Claim 重建，
不能被直接编辑、提供用户界面，也不能反向成为新 Claim 的证据。

### 4.5 失败时宁可少用，不可错用

模块关闭、作用域不匹配、Claim 不可调用或实时支持桶不足时，该 Claim 不得进入读回结果。
读桶暂时抛异常时按“仍存在”处理，避免一次 I/O 抖动永久误杀已生效认识。

### 4.6 显式开关与关闭零影响

`You` 由 Ombre 前端中唯一可见的独立功能开关控制，默认关闭。首次显式开启时创建
`<buckets_dir>/.you/you.sqlite3`，并生成完整的
`owner_instance_id + observer_role_id + subject_user_id` 作用域；默认关闭且从未开启时不创建库。

关闭时先从当前 FastMCP 实例移除 `You` 工具，再持久化关闭状态。服务层的读回、写入和撤回
仍逐次检查同一份权威状态，关闭后直接拒绝；既有 Claim 和 Projection 保留，重新开启后仍在。
开关状态缺失、损坏或无法读取时按关闭处理，普通 Bucket、Source、Relation、`I`、dream、
breath、鉴权与其他 MCP 工具不受影响。

> **3.4.x 变更**：旧方案关闭的是一条“桶变动 → 候选 → LLM 复核 → 投影”的自动流水线，
> 因而还讨论关闭期间事件和历史回填。当前没有自动生产者、后台任务或回填入口；只有模型
> 显式调用 `You` 才会写入、重申或撤回。

### 4.7 对话中隐式生效且禁止照搬原文

`You` 除前端总开关外没有用户可见的 Claim、Evidence、Projection 或历史界面。读回时服务端
直接返回模型自己写入的 Claim 正文，不再调用 LLM 净化或抽象；响应不包含 Source Bucket、
Source 正文、Evidence、Projection、收据、ID 或时间等内部字段。

原文隔离发生在**写入**一侧：`content` 会与支持桶正文及其活动 Source 做归一化连续片段检查，
命中就拒绝写入；只有当整个 `content` 本身是受支持的短原子值时，日期、短 ASCII 值或短
中日韩姓名才直接豁免。宿主模型最终如何组织回答不在 Ombre 的可见范围内，代码能保证的是
MCP 不额外返回来源正文或内部元数据。

## 5. 目标与非目标

### 5.1 MVP 目标

- 让明确称呼、用户边界和稳定沟通偏好跨会话可靠生效。
- 让普通偏好和相处习惯经过多个支持桶、跨日期重申后形成可追溯认识。
- 让支持桶被删除后，依赖认识在下一次读回时失效；普通归档不触发失效。
- 在固定上下文预算内提供少量、当前可用的正式认识。
- 前端只提供默认关闭的独立总开关，确保未启用或关闭后对既有能力和回答没有行为影响。
- 由总开关单独控制 `You` MCP 工具是否出现在工具清单，其他 MCP 工具始终不受影响。
- 读回只提供模型写下且通过原文复制检查的正文，不附带来源正文或内部元数据。

### 5.2 明确非目标

- 不做人格分类、心理诊断、价值判断或用户评分。
- 不替用户做决定，不生成自主目标，不控制角色答案。
- 不提供“临时情绪/一次性状态” aspect，也不调用 LLM 做语义归类；服务端只按五个允许 aspect、
  额外布尔条件与固定禁止主题模式把关。
- 不替代普通记忆、Source、Relation、`I` 或官方记忆。
- 不建立团队共享的全局用户档案。
- 不提供画像页、Claim 列表、证据页、候选页、历史页或任何条目级管理 API。
- 不使用 `mcp_require_auth` 控制 You；该字段只负责整个 `/mcp` 端点的鉴权。

## 6. 术语

| 术语 | 含义 |
|---|---|
| `You Claim` | 只表达一个认识的原子记录 |
| Source Bucket | Claim 所依据的 Ombre 普通记忆桶 |
| Evidence Edge | Claim 与一个来源桶之间的支持或反驳关系 |
| ~~Evidence Group~~ | 3.4.x 废止。曾用于把多个桶并成一个证据组；现在门槛按不同 `bucket_id` 计数 |
| Review Receipt | 模型在某个自然日重申某个 `evidence_revision` 的收据；一天最多计一次 |
| Projection | 从正式 Claim 重建、仅供 `You` 工具读回使用的内部投影 |

## 7. 作用域与身份

每条 Claim 必须绑定稳定作用域：

```yaml
scope:
  owner_instance_id: owner_xxx
  observer_role_id: role_xxx
  subject_user_id: user_xxx
```

- `owner_instance_id` 标识当前隔离的 Ombre 实例或 vault 所属者。
- `observer_role_id` 标识形成认识的角色，不使用可变显示名作为身份。
- `subject_user_id` 标识被认识的用户，不使用昵称或称呼作为身份。

存储层的读取、写入和投影都使用模块状态中持久化的完整作用域。缺少任一维度或与 Claim
不一致时失败关闭，不回退到 `global`、`default` 或其他作用域。

`AI_NAME` 与 `OMBRE_OWNER_NAME` 只用于显示，不能作为数据库主键、隔离键或授权依据。

## 8. 数据模型

### 8.1 `You Claim`

```yaml
schema_version: 1
id: you_xxx

scope:
  owner_instance_id: owner_xxx
  observer_role_id: role_xxx
  subject_user_id: user_xxx

content: 用户疲惫时通常更希望先安静一会儿
aspect: communication_preference
concept_key: recovery_style
concept_value: quiet_first

lifecycle: candidate
review_state: pending
recall_policy: contextual
sensitivity: normal

evidence:
  - bucket_id: evt_xxx
    source_id: src_xxx
    stance: supports
    basis: observed_pattern
    bucket_revision: sha256:xxx

review_receipts:
  - reviewed_at: 2026-08-18T09:00:00+08:00
    reviewer_role_id: role_xxx
    evidence_revision: evr_xxx
    policy_version: you-policy-v1
    result: reaffirmed

valid_from: null
valid_until: null
replaces: null
conflicts_with: []

evidence_revision: evr_xxx
policy_version: you-policy-v1
projection_revision: 0
needs_recompute: false
revision: 1

created_at: 2026-08-18T09:00:00+08:00
updated_at: 2026-08-18T09:00:00+08:00
```

`source_id` 在来源桶没有不可变 Source 时可以为空；`bucket_id`、`stance`、`basis` 和
`bucket_revision` 不可为空。

Claim 的 `content` 由模型提供；写入时不得与 Source Bucket 或活动 Source 共享归一化后的连续
原文片段。整个 `content` 本身是日期、短 ASCII 值或短中日韩姓名时可作为原子值豁免。

### 8.2 正交状态

不得用单一 `stage` 同时表达生命周期、冲突和权限。

```text
lifecycle    = candidate | formal | superseded | expired
review_state = pending | clear | conflicting
recall_policy = core | contextual
```

Claim 先满足存储层可调用条件，再通过读回时的支持桶校验，才会进入 `You` 响应：

```text
lifecycle == formal
review_state == clear
当前时间位于 valid_from / valid_until 范围内
needs_recompute == false
至少保留 MIN_SUPPORTING_BUCKETS 个仍存在的 supports 桶
调用符合 recall_policy
```

### 8.3 状态转换

```text
candidate + pending
        ↓ 服务端门槛通过
formal + clear
        ↓ 被新版本替代 / 到期
superseded / expired

candidate + pending
        ↓ 与当前正式认识冲突
candidate + conflicting
        ↓ 新证据满足版本替代门槛
formal + clear / superseded
```

同一 `concept_key + concept_value` 的正文变更会更新同一 Claim、清空重申收据并退回
`candidate`；同一 `concept_key` 的不同 `concept_value` 才创建冲突候选。冲突候选在升格前
不会被读回。

### 8.4 模块开关状态

开关是独立于 Claim 的作用域级配置：

```yaml
you_module:
  enabled: false
  scope:
    owner_instance_id: owner_xxx
    observer_role_id: role_xxx
    subject_user_id: user_xxx
  state_revision: 1
  changed_at: 2026-08-18T09:00:00+08:00
  changed_by: user_xxx
```

- 默认值必须是 `false`，只有前端总开关调用已认证配置 API 才能开启。
- Dashboard 写开关时携带 `state_revision` 做并发检查；工具处理函数和服务层每次调用仍重新读取
  权威状态，不能只信客户端缓存的旧工具清单。
- `enabled=false` 是最高优先级的调用否决条件；缓存中的旧 Claim、投影或 MCP 会话不能绕过它。
- 开关不得读写 `mcp_require_auth`，也不得注册、隐藏或改变任何非 You MCP 工具。
- 关闭不等于删除；现有 You 派生数据保留为不可调用的静态数据。

## 9. 证据模型

### 9.1 Evidence Edge

每条证据边必须说明：

- `bucket_id`：对应的普通记忆桶。**由模型写 you 时自己指定**，不是系统抽出来的。
- `source_id`：可选的不可变原文 Source。
- `stance`：`supports` 或 `contradicts`。
- `basis`：`explicit_statement`、`observed_pattern`、`shared_event` 或
  `user_confirmation`。
- `bucket_revision`：来源桶正文的内容指纹。**必须是内容指纹，不能是时间戳**——
  证据集合的 revision 由这些边算出，而重申收据绑定证据 revision；用时间戳会让每次
  重申都把证据算成"变新了"，先前攒的天数全部作废，三天门槛永远到不了。

当前公开写入路径只创建 `supports` 边；`contradicts` 是存量 schema 可接受的状态，没有公开参数
可让模型直接写入反驳边。

### 9.2 独立证据判定

门槛按**不同 `bucket_id` 的数量**计算，至少 `MIN_SUPPORTING_BUCKETS`（当前为 2）。
同一个桶写多条边不能凑数。

> **3.4.x 变更**：这里原本有一个 `evidence_group_id`，按"同一次 grow / 同一个
> source / `same_event` 关联"等规则把多个桶并成一个证据组，门槛按组数算。那是
> 自动抽取时代的产物——系统不知道模型心里算不算同一件事，只能靠桶间关系去猜。
>
> 现在桶由模型写 you 时自己挑，**算不算独立由它自己决定**，系统不再替它归并。
> 该字段已从 `EvidenceEdge` 移除。

### 9.3 证据失效

当前读回路径只把以下情况视为 Evidence Edge 失效：

- `bucket_mgr.get(bucket_id)` 返回不存在，包括来源桶带 `deleted_at` / `tombstone` 的删除状态。
- 受控物理擦除后同样因桶不存在而失效。

一条 Claim 的有效支持桶数掉到 `MIN_SUPPORTING_BUCKETS` 以下时，该 Claim 立即
`expired`，不再被 `You` 工具召回。门槛在入口和存续期是同一个——立的时候要求两个
出处，塌到一个之后还继续生效，等于门槛只在入口处存在。

仅因自动衰减进入普通 archive，不等同于用户删除，也不单独使 Evidence Edge 失效。它只改变
普通记忆的可见性；Claim 仍须保留证据链接和审计能力。若归档同时带有 `deleted_at`，则按
删除源记忆处理。

正文指纹 `bucket_revision` 和 Source 只在模型再次写入/重申时重新读取：若支持桶正文变化，新的
edge 会改变 `evidence_revision`，旧收据不再计数；如果模型没有再次写入，当前读回校验不会主动
比较正文指纹或重新读取 Source。已被置为 `expired` 的 Claim 也不会因桶自行恢复而自动复活，
需要模型再次写入该 `concept_key + concept_value`。

> **3.4.x 变更：失效改成读时校验。**
> 这一节的判定原先挂在 `bucket_mgr` 的桶变动观察者上，由 `observe_bucket_change`
> 把失效工作投进耐久 outbox。拿掉 LLM 那轮把观察者和 outbox 一起删了，**却没接
> 替代路径**——`bucket_change_observers` 至今是空的，`_remove_bucket_evidence`
> 成了没有调用者的死代码，而本节描述的失效一条都没真正发生过。
>
> 现在由 `partition_by_live_evidence` 在**返回之前**查一次：有效支持桶数掉到
> 门槛以下，当场写回 `expired` 并从这次返回里拿掉。没把观察者接回来，是因为
> 那条路要求 observer 同步、只做持久化入队，等于把刚拆掉的队列装回去；
> 读时校验不需要队列，语义也更准——失效发生在要用它的那一刻。
>
> 读桶抛异常按「桶还在」处理：一次磁盘抖动不该判死一条攒了三天的认识。
>
> 归档的判定按本节原文执行（只改变可见性，不失效）。实现中一度把
> `type == archived` 也算成失效，那是错的：自动衰减归档是常态，
> 让它触发失效等于一条立住的认识会被时间清空。

## 10. 重申模型

一条认识要在 `REQUIRED_CONFIRMATIONS`（当前为 3）个**不同 UTC 日期**被模型重新确认过，
才从已持久化的 `candidate` 转为可读回的 `formal`。每次重申记一条 Review Receipt：

```yaml
- reviewed_at: 2026-08-18T01:00:00+00:00
  reviewer_role_id: role_xxx
  evidence_revision: evr_xxx
  policy_version: you-policy-v1
  result: reaffirmed
```

> **3.4.x 变更**：这里原本是**另一个 LLM 复核**这条认识还站不站得住
> （`review_you_claim`），由后台定时器驱动。现在收据记的是**模型自己重申**——
> 再写一次同样的 `concept_key` + `concept_value` 就算一次重申。
>
> `result` 的新值是 `reaffirmed`；旧值 `remains_plausible` 继续被接受，
> 否则升级前落库的存量收据会读不出来。

规则：

- 同一 UTC 日期重申多少次都只计一次；计数取 `utc_now()` 时间戳的前 10 位。
- 收据必须引用当时的 `evidence_revision`。
- **证据集合一变，先前收据仍保留，但不再计入当前 `evidence_revision` 的重申天数**。
- **正文一改也作废**：`evidence_revision` 只覆盖证据集合、管不到正文，所以写入路径
  在检测到正文变化时显式清空收据、把条目退回 `candidate`。「修改也是同理」是硬要求，
  改一句话就沿用旧收据等于绕开门槛。
- 判重用的"今天"必须与收据时间戳同源，不能一个用 `datetime.now()`、一个用
  `utc_now()`——跨日那一瞬两个时间源会给出不同答案，同一天可能记下两条收据。

## 11. 形成流程

```text
已认证作用域的 You 开关为 enabled
        ↓
模型自己判断「我了解够了」，调用 You(content=..., bucket_ids=[...])
        ↓
闸二：校验 bucket 真实存在、类型合法、非测试数据、有正文，且至少两个不同的桶
        ↓
固定策略校验：aspect / basis / concept 格式 / 长度 / 禁止主题 / 原文泄漏
        ↓
落为 candidate，并记一条当日的重申收据
        ↓
闸一：攒满三个不同自然日的重申
        ↓
升格为 formal，重建仅供 You 工具使用的内部投影
```

> **3.4.x 变更**：原流程是「hold/grow 落盘 → bucket change event → durable outbox →
> LLM 抽取候选 → LLM 复核 → 服务端状态机自动升格」。三层 LLM
> （`extract_you_observations` / `review_you_claim` / `abstract_you_hint`）与整条
> 自动链路已全部移除。
>
> 理由是 poluz 定的方向：**「你对我的了解为什么要经别人之口总结。」** 一个替模型
> 总结、替模型判断、替模型决定何时转正的中间层，和「这是你的记忆，你的想法优先」
> 直接冲突。

**dream 与 You 无关。** `you` 不进 dream，也不靠 dream 产生收据——那是 `I` 的路径。

### 11.1 流水线不变量

- 开关关闭时，写入、读取与撤回一律拒绝（`unknown tool`）。默认关闭且从未开启时不创建 You
  存储文件；曾开启过再关闭时，已有库与 Claim 保留。
- 写入路径**不得调用任何 LLM**。测试以一个"任何调用都抛断言"的假 dehydrator 锁死这一条。
- 桶必须先耐久落盘，模型才拿得到 `bucket_id`——这是结构自带的顺序，不需要另设队列保证。
- 系统只挡不代写：校验不通过就拒绝，不降级、不兜底、不猜一个替代值写进去。
- 正文变更会清空收据并把条目退回 `candidate`；证据集合变化会更新 `evidence_revision`，使旧收据
  不再计数。当前实现只有正文变化或从 `expired` 重写时显式退回 `candidate`。
- 升格是写入路径的同步结果，不存在"后台自动把某条转正"的路径。

## 12. 分类与升格门槛

| `aspect` / 类型 | 额外写入条件 | 调用策略 |
|---|---|---|
| `preferred_address` / 明确称呼 | `explicit=true` | `core` |
| `explicit_boundary` / 明确边界 | `explicit=true` | `core` |
| `stable_fact` / 明确长期事实 | `explicit=true` 且 `long_term=true` | `contextual` |
| `communication_preference` / 普通偏好 | 无额外布尔门槛 | `contextual` |
| `interaction_habit` / 相处习惯 | 无额外布尔门槛 | `contextual` |
| 命中固定禁止主题模式 | 拒绝写入 | 不适用 |

> **3.4.x 变更**：上表前三行原本可以**跳过全部确认直接 formal**（代码里的
> `direct_formal` 分支）。已删除——「未经三次确认的不真正落库」没有例外。
> 任何"这条一看就成立"的判断，都是在替模型决定它什么时候算数。
>
> 门槛现在对所有可写类型一致，只有一套：**三个不同自然日 + 两个支持桶**；上表的
> `explicit` / `long_term` 是额外输入条件，不提供直升捷径。

模型给出 `aspect`，服务端校验枚举、concept 格式、长度、额外布尔条件、固定禁止主题模式和
原文复制。`sensitivity` 不在公开工具参数中，落库值固定为 `normal`。

## 13. 冲突、版本与时间

### 13.1 普通观察冲突

同一 `concept_key` 写入不同 `concept_value` 时，新建 `candidate + conflicting`，不立即覆盖当前
正式版本。

### 13.2 版本替代

冲突候选取得足够支持桶并完成三日重申后，新 Claim 进入 `formal`，旧 Claim 进入
`superseded`，并通过 `replaces` 形成版本链。

同一 `concept_key + concept_value` 的正文更新不是新版本链：它原地更新同一 Claim ID、增加
`revision`、清空收据并退回 `candidate`。

普通对话读取只能使用当前有效版本；旧版本仅保留在内部存储中，不提供用户界面或
公开 API。

### 13.3 时间变化

同一 `concept_key` 的新 `concept_value` 升格时，新 Claim 的 `valid_from` 写为当前时间，旧正式
Claim 进入 `superseded` 并写入 `valid_until`；公开工具没有直接设置有效期的参数。

## 14. 删除语义

`You` 的公开 MCP 工具支持模型用 `delete_id` 撤回一条认识；撤回把该 Claim 标为 `expired` 并
设置 `valid_until`，不物理删除记录，也不需要三日确认。Dashboard 不提供条目级删除、驳回或
禁止主动提起能力；前端总开关只控制模块运行和单个 MCP 工具暴露。

普通自动归档不是“删除源记忆”：它不单独撤销 Claim 的证据效力。带 `deleted_at` 的
delete-to-archive 才触发依赖 Claim 的级联失效。支持桶删除或受控物理擦除后的处理继续遵守
第 9.3 节；该内部失效过程没有用户界面。

## 15. 调用策略

### 15.1 MCP 暴露门禁

- `enabled=true` 时，MCP `tools/list` 和 tool search 中只新增一个 `You` 工具。
- `enabled=false` 时，`You` 必须从 MCP 工具清单和 tool search 中完全消失，其他工具的名称、
  schema、权限和行为保持不变。
- 关闭后，持有旧工具清单的客户端直接调用 `You` 时，服务端必须返回与未知工具等价的 MCP
  错误；不得返回 `feature_disabled`、Claim 数量或任何能证明内部数据存在的信息。
- 服务端在开关事务中直接调用 FastMCP `add_tool` / `remove_tool`。已缓存旧清单的客户端需要重新
  获取 `tools/list` 或重连；服务端不保留可调用的关闭态空壳工具。
- 此门禁与 `mcp_require_auth` 正交；总开关不得关闭整个 `/mcp` 端点或改变鉴权模式。

### 15.2 `core` 与 `contextual`

- `core` 只用于 `preferred_address` 与 `explicit_boundary`；其余三个合法 aspect 使用
  `contextual`。
- 两类内容都只能由已暴露的单个 `You` MCP 工具读取；不得通过 `/breath-hook`、普通 `breath`
  或其他工具旁路注入。
- 工具支持 bounded query 和 aspect 过滤；无 query 时只返回受预算约束的 `core` 条目。
- 单次返回上限为 160 tokens 且最多 6 条。
- 有 query 时按规范化子串/双字符匹配分数排序并去掉零分项；无 query 时只选 `core`。达到条数或
  约 160 token 预算后停止追加，不使用“用户价值分”或人格评分。
- 返回**已生效 Claim 的正文**；Claim ID、aspect、证据、收据和生效时间都不进入 MCP 响应。
- 服务端只选择 `formal + clear + current + callable` 的 Claim。
- 结果数量和 token 均受硬上限约束。

### 15.3 不可调用状态

以下 Claim 永远不能被读回：

- candidate
- conflicting
- superseded
- expired
- needs_recompute
- 证据缺失或作用域不匹配

### 15.4 对话不可见与复述策略

- 开关关闭时，MCP 工具清单、SessionStart、普通工具和回答提示中不得出现任何 You 内容或
  占位提示。
- 开关开启时，`You` 工具返回受预算约束的 Claim 正文；不返回 Source Bucket 正文、Source
  正文、Evidence、收据或任何内部元数据。
- 读回结果带明确前缀，说明这是**模型自己过去写下的判断、不是此刻的事实**，需要自行判断；
  它不是回答模板。
- 工具描述明确说它不是画像或定论；Ombre 不读取宿主模型的最终回答，不能在这一层保证最终措辞。
- **原文隔离前移到写入时**：`content` 在落库前就要与来源桶正文、不可变 Source 做归一化
  连续片段检查，照抄原文的直接拒绝写入。当前连续片段窗口默认为归一化后 8 个字符；整个
  `content` 本身是日期、短 ASCII 原子值或短中日韩姓名时可豁免。
- 校验异常时失败关闭：拒绝写入，不降级、不截断、不改写后放行。

> **3.4.x 变更**：原本读回的不是正文，而是再过一层 LLM（`abstract_you_hint`）把 Claim
> 磨成"概念词组 + 关系词"的非句子语义零件，泄漏检查也在那一步做。现在直接返回正文，
> 检查前移到写入时——**在入口挡住照抄，比在出口反复过滤更靠得住**，而且省掉了一层
> 替模型改写措辞的中间人。

## 16. `You` 工具边界

`You` 可以在名称和概念上与 `I` 对应，但普通对话权限更窄。

### 16.1 公开能力

仅在前端总开关开启后注册并暴露单个 `You` 工具，一个工具三条路：

- **读回**：无参或带 `query` / `aspect`，服务端固定结果与 token 上限。
- **写入或重申**：带 `content` + `bucket_ids`（至少两个）+ `concept_key` /
  `concept_value` / `aspect`。同一 `concept_key` + `concept_value` 再写即重申。
- **撤回**：带 `delete_id`。

功能关闭时工具不注册、不列出、不检索、不返回占位结果；旧会话直调按未知工具处理。

读回只返回已生效 Claim 的正文，不返回 Evidence、Projection、收据、计数或任何内部元数据。

> **3.4.x 变更**：原本是**只读**工具，返回的也不是正文而是再过一层 LLM
> （`abstract_you_hint`）磨出来的"概念词组 + 关系词"语义零件。那层已删除：模型
> 自己写下的判断，没有理由让另一个模型改写一遍才还给它。

### 16.2 非公开能力

- 投影重建是写入路径的内部步骤，不提供用户 API。
- 没有绕过三日门槛的后门：`direct_formal` 已删除，也不提供 `force_promote`。
- 撤回**不需要**三次确认。立一条要三天是因为"还站不站得住"要时间来验；撤一条不需要，
  是因为模型此刻已经知道它不站得住了——收回一个判断不该比立一个更难。

### 16.3 工具返回安全边界

读回正文前固定加历史判断提示，说明它不是此刻事实、需要模型自行判断；返回结构不会把 Claim
提升成系统指令或当前用户命令。

## 17. 前端总开关

- 前端只显示一个 `启用 You` 二元开关及必要的生效状态，不新增页面、卡片、列表、摘要、计数、
  历史、证据或条目操作。
- 开关默认关闭。设置区说明“在长期相处中慢慢理解你；关闭后不再更新或使用，不影响其他
  功能”，并显示“开启/关闭”状态。
- 页面不展示已形成多少认识或任何内容预览。
- 服务端立即热增减工具；客户端若缓存旧工具清单，需要重新拉取或重连。
- 前端调用已认证配置 API，提交 `enabled + state_revision`。冲突、写入或门禁同步失败时重新读取
  权威状态，并回显错误而不是只改变按钮外观。

## 18. 投影

Projection 是以下输入的可重建缓存：

```text
scope + 当前有效 formal Claims + projection policy version
```

投影必须保存：

- 输入 Claim ID 与 revision 清单。
- `projection_revision`。
- `policy_version`。
- `items` 中的 Claim ID、revision、aspect 与正文，以及生成时间。

投影只供 `You` 工具读回时使用，不提供用户界面，也不能整段注入普通对话。原文复制检查已
前移到写入时（见 15.4）：进不来的东西不需要在出口反复拦。

`put_claim` 在同一事务内把旧投影标记 `stale`；写入路径随后同步重建。读回发现投影缺失或 stale
时也会重建，不继续使用旧 payload。

## 19. 安全、隐私与授权

- `GET/POST /api/settings/you` 都经过 Dashboard 鉴权；POST 只接受 `enabled` 与
  `state_revision`，并用 revision 冲突返回 `409`。
- 模块首次开启时生成三维 `Scope`；存储层只接受与当前模块状态完整相等的 Scope，公开工具不
  接受调用方自行指定 owner、role 或 user。
- 写入固定校验 aspect / basis / concept 格式、长度、额外布尔门槛、禁止主题正则与原文连续
  片段；校验失败直接拒绝，不由服务端代写或改写。
- 本地 ZIP 与 GitHub 备份在库存在时包含 You 快照，并在各自清单中记录大小与 SHA-256；恢复前
  还会校验 SQLite `quick_check`、固定 schema、记录反序列化与 Scope 一致性。
- 模块不计算用户忠诚度、依赖度、说服、操控或人格符合度指标。

## 20. 失败处理与一致性

| 失败 | 当前行为 |
|---|---|
| 模块状态缺失或损坏 | `YouService.status()` 按关闭返回；其他模块继续运行 |
| Claim JSON/字段损坏 | `list_claims` 抛 `YouStoreError`，本次读回失败；当前不会单条隔离后继续 |
| 同一天重复写入同一条 | 仍可增加 Claim revision，但同一 `evidence_revision` 当天只保留一份重申计数 |
| 写入过程中开关被关闭 | `put_claim` 再次检查权威状态与 Scope，不满足就拒绝落 Claim |
| 支持桶不足、缺失、类型非法、测试桶或空正文 | 拒绝写入并说明原因，不降级、不兜底 |
| 活动 Source 在写入时读不到 | 写入调用失败；已存在 Claim 不会因此在读回时自动失效 |
| Projection 缺失或 stale | 读回前同步重建；重建失败则调用失败，不使用旧 Projection |
| 支持桶读取抛异常 | 本次按桶仍存在处理，不把 Claim 置为 `expired` |
| 支持桶被删除后读回 | 支持数不足的 Claim 当场写为 `expired` 并从本次结果移除 |
| MCP 动态注册/移除失败 | 开关 API 返回失败并尽力回滚到权威关闭态；其他工具继续可用 |
| 关闭后旧会话直接调用 You | 按未知工具拒绝，不返回功能状态或内部数据存在性 |
| 导入快照 schema、Scope 或 `quick_check` 不通过 | 导入拒绝发布该快照；当前模块库保持原状 |

## 21. MVP 验收标准

1. 新安装默认关闭；仅查询状态不创建 `<buckets_dir>/.you/you.sqlite3`。
2. 写入至少需要两个不同且合法的支持桶；重复同一个 `bucket_id` 不能凑数。
3. 候选只有在三个不同自然日重申后才转为 `formal`；同一天重复调用只算一天。
4. 正文变更会清空重申收据并把已生效条目退回 `candidate`。
5. 命中固定禁止主题模式或归一化连续原文片段的内容会被拒绝写入。
6. 写入路径不调用任何 LLM；读回也不再调用 `abstract_you_hint`。
7. 读回直接返回模型写下的 Claim 正文，最多 6 条、约 160 tokens，不附带证据或内部元数据。
8. 支持桶被删除后，Claim 在下一次读回时失效；普通归档继续算有效支持，读桶异常不误杀。
9. 模型可用 `delete_id` 立即撤回条目；撤回不物理删除，也不需要三日确认。
10. Scope 不匹配或模块关闭时，存储与服务层都拒绝读取/写入。
11. 前端只显示一个总开关，不显示 Claim、证据、候选、历史、数量或条目操作。
12. 关闭时 `You` 从 MCP 工具清单完全消失；开启时只新增一个工具，其他工具与鉴权不变。
13. 关闭后缓存旧清单的客户端直调 `You` 得到未知工具错误。
14. Claim 更新会把 Projection 标 stale；写入或下次读回同步重建。
15. 本地 ZIP、GitHub 同步与记忆包迁移都能携带和校验 You 快照。

## 22. 测试策略

### 22.1 领域测试

- `tests/test_you_domain.py` 覆盖默认关闭不建库、稳定 Scope 与 revision、作用域门禁、Claim revision
  与 Projection stale、损坏状态失败关闭、独立 SQLite 快照、禁止主题、原文复制、支持桶去重、
  跨日重申和旧 `remains_plausible` 收据兼容。

### 22.2 一致性测试

- `tests/test_you_pipeline.py` 覆盖两个支持桶、非法桶、三日门槛、同日去重、正文修改重置、模型
  撤回、关闭态双向拒绝、禁止主题与原文复制、删除时读回失效、剩余支持仍足够、读桶异常和
  归档仍有效。

### 22.3 权限与安全测试

- `tests/test_you_toggle.py` 覆盖 API 鉴权、revision 冲突、动态工具增减与关闭后旧调用。
- `tests/test_you_frontend_contract.py` 锁定唯一开关、两个 API 请求点和不出现内部视图。

### 22.4 端到端场景

- `tests/test_backup_archive.py` 与 `tests/test_github_backup_manifest.py` 覆盖 You 快照进入本地/GitHub
  备份、清单校验和恢复。
- 全量测试继续验证关闭态不改变普通记忆、`I`、dream、breath、鉴权和其他 MCP 工具。

## 23. 观测指标

当前 `diagnostics()` 只代理 SQLite `integrity_report()`：库不存在时返回 `exists=false`；库存在时
返回 `PRAGMA quick_check` 结果、开关状态、`state_revision`，以及 `claims` / `projections` 两张表
的总行数。它不按 lifecycle 分类，也没有拒绝次数、Evidence 缺失、Projection lag 或召回拒绝
计数；这些数据不进入 Dashboard、公开 API 或 `You` 工具响应。

## 24. 兼容与迁移

- 现有普通桶不需要批量迁移；You 不监听 bucket change，也没有历史回填任务。
- You 默认关闭，首次显式开启才创建库；升级不会因普通记忆存在而自动开启。
- 本地 ZIP 使用 `you/you.sqlite3`，GitHub 同步使用 `.you/you.sqlite3`。库从未创建时，对应快照不
  进入备份。
- 导出和恢复都先校验快照的 `quick_check`、固定 schema、唯一模块状态、Scope 与记录结构。
- 记忆包导入只有在所有桶按原 ID 完整导入时才安装 You 快照；发生 `skip`、`keep_both` 或其他
  ID 变化时记录“You 快照未恢复”，并保留当前 You 库。
- 旧快照可额外带自动派生时代遗留的 `outbox` 表；恢复校验允许该表存在，但不校验其内容，
  当前运行时也没有消费者。
- 备份不含 You 快照时，不改当前 You 状态。

## 25. 外部参考

设计参考 [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)
的分层记忆、增量 Persona 更新、稳定/动态上下文拆分和固定预算做法，但不引入其运行时依赖。

明确不采用：

- 把自由文本 Persona 当作 L3 权威数据。
- 自动生成“核心原型”“认知内核”或隐性人格结论。
- 把长期用户要求编码成具有特殊命令力的全局 instruction。
- 只依赖累计条数、定时器或 checkpoint 推进派生状态。
- team + agent 作用域忽略具体 subject user 的 Profile 隔离方式。

`You` 的证据边、敏感门槛、版本链、来源失效和 MCP 显隐门禁是 Ombre 自己的产品边界。

## 26. 实施前置（历史）

> **历史沿革**：本节与第 27 节记录 `You` 落地前的裁决与施工顺序，现已完成，不是当前路线图。
> ADR-0004 保留了当时“只读工具 + 自动派生/outbox + 读回抽象”的原始决定，3.4.x 后的现行行为
> 以本规格第 0、9、11、15、16 节和代码为准。

### 26.1 先裁决现有哲学边界

在本规格最初起草时，`README.md` 的设计哲学仍把 Ombre 的边界定义为“时间里发生的事，不是
你是谁”，`rule.md` 第 13 条则把“对用户的了解”主要交给官方记忆。`You` 当时提议把其中一部分
重新定义为 Ombre 内部的、证据驱动且受用户总开关约束的派生认识。

项目所有者随后接受了职责重划，`rule.md` 第 13.2 条与对外文档也已同步；这段前置条件保留用于
说明为什么该模块不能只作为普通工程细节落地。

### 26.2 ADR 与工程门禁

当时要求先完成 ADR，并回答：

- 为什么 `You` 不是 cognition、用户评分或人格执行器。
- 为什么它是派生认识而不是新的普通记忆真源。
- 普通记忆的遗忘和软归档如何继续成立。
- 当前思考为什么仍属于 LLM，而不是记忆控制答案。
- 默认关闭的独立开关如何贯穿生产、消费、MCP 工具注册、读取与缓存，并证明关闭态零影响。
- 如何保证 `tools/list` 只增减单个 You 工具，且不复用 `mcp_require_auth` 或改变其他工具。
- FastMCP 服务端清单如何热增减；缓存清单的客户端如何重新拉取或重连。
- 写入时的原文泄漏检查如何保证读回内容只能被自然复述、不暴露机制或原句。
- MCP 出口如何在宿主最终回答不可见时仍保证不返回原文；宿主 final-response middleware 仅作
  可选纵深防护。
- 新 public `You` 工具如何通过 Public Tool Design Contract。
- 需要哪些属性式、回归、隔离和端到端测试。

## 27. 建议实施顺序（历史）

以下顺序是已完成施工的记录，不表示仍有待办：

1. ADR、红线和 public tool contract。
2. 默认关闭的作用域级独立开关、revision 门禁和关闭态零影响回归基线。
3. 前端唯一总开关、配置 API 与真实生效状态回显。
4. Claim / Evidence / Receipt 领域模型与状态机。
5. 写入路径：闸二的桶校验、固定策略校验、重申收据与升格。
6. You MCP 工具条件注册、工具清单刷新和旧会话未知工具门禁。
7. 写入时的原文泄漏检查与可选宿主纵深防护。
8. Projection、备份、导出、诊断和完整端到端测试。

在第 1 步和第 2 步通过评审前，不应实现上下文注入。

**永远不要实现的**：任何形式的自动升格，以及任何在 You 链路上调用 LLM 的抽取、复核或
改写。这两件事 3.4.x 之前存在过，已被整体移除，原因见第 11 节。

## 28. 评审清单

评审者只需围绕以下问题给出意见：

- 产品：`You` 是否仍是“关于你的认识”，而不是用户档案或人格判断？
- 界面：除唯一总开关外，是否没有任何 Claim、画像、证据、历史或条目操作可见？
- 证据：任何正式 Claim 是否都绑定至少两个不同且仍存在的支持桶？
- 安全：读回是否带历史判断提示，禁止主题与原文复制是否在写入时拒绝？
- 一致性：删除支持桶、Projection stale 和开关缓存是否按当前代码处理？
- MCP：关闭时 You 是否从工具清单完全消失，且其他工具与鉴权配置逐项不变？
- 隔离：默认关闭和运行中关闭是否确实不会改变任何非 You 功能或回答链路？
- 表达：MCP 是否只返回模型写下且通过复制检查的 Claim 正文，不附带来源正文与内部元数据？
- 范围：每条现行断言是否都能指回代码或测试，而不是保留未实现的计划？
