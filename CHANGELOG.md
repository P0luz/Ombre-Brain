# 更新日志 / Changelog

本项目版本号见根目录 `VERSION` 文件，Docker 镜像 tag 与之对应（`p0luz/ombre-brain:<VERSION>`）。

## 3.6.3

### 修复 / Fixed

- **`delete_id` 此前是够不着的：You 与 Them 都从不交出 claim id。**
  - 两个工具的签名里都有 `delete_id`，描述也写着「带 delete_id 是撤回一条」，
    但**没有任何读取路径把 id 交出来过**。Them 的读回只给
    `{aspect, content}`，候选清单只给「还差几天」；You 的读回只给
    `- 正文`。一个宣称得到、却拿不到前提的能力，比没有这个能力更糟。
  - Them：`claim_id` 直接进读回的 JSON，候选清单每条加 `id=`。它的输出没有
    token 硬预算，无条件带上即可。
  - You：新增 `with_ids=True`。**默认不带**——实测一个 id 约 12 token，而 You
    的读回总预算只有 160，无条件带上会把 4 条正文挤成 3 条。为一个偶尔才用的
    能力常年砍掉 1/4 正文不划算。照 `breath_search(quotes=True)` 那套：
    默认一字不少，想撤回时明确要。

### 变更 / Changed

- **`known_via` 从 `origin` 里拆出来，成为独立字段。**
  - 此前 `known_via` 是推导出来的：`"heard_from_user" if person.human_visible
    else "met_myself"`，而 `human_visible` 就是 `origin == "human"`。
  - 于是模型写一个「只在人类口中听说过」的人时，没有任何办法把它标对——**而想
    标对就必然连带把自己关于这个人的私有认识对人类公开**，因为那是同一个开关。
    `origin` 同时管着四件事：给模型看的 `known_via`、人类能不能看见模型写的
    认识、人类能不能留纠错、同名不自动合并的判据。
  - 拆开之后：`origin` 继续只管可见性（rule.md 13.3 的线，模型改不动），
    `known_via` 只管「我见没见过这个人」（模型自己说了算）。
  - `Them(content=..., known_via="heard_from_user")` 写入时指定；发现标错了，
    下次写这个人时带上正确的值就订正过来。空串 = 不动。
  - **存量数据零影响**：老记录没有这个键，`__post_init__` 按老规则从 `origin`
    推一次，表现与拆分前逐字一致。persons 存的是 `payload_json`，不需要迁移
    schema。

## 3.6.2

### 修复 / Fixed

- **`You` 的拒绝提示现在说得清是哪一条、允许值是什么。**
  - 之前 `_validate_observation` 有九道各自独立的闸，但**全部只返回 `None`**，
    调用方只好把九种可能拼成一句「aspect / basis 必须是允许值，concept_key 用
    snake_case……」丢回去。收到的人既不知道允许值是什么，也不知道自己撞了哪一条。
  - 更糟的是其中两条根本没被提到：`preferred_address` / `explicit_boundary`
    需要 `explicit=True`，`stable_fact` 还要再加 `long_term=True`。枚举填对了
    照样被拒，而报错一字不变。
  - 非法 `basis` 甚至走不到那句话——`_build_edges` 先跑到，`EvidenceEdge`
    直接抛裸英文 `invalid evidence basis`。模型层这道闸同样补上了枚举。
  - 枚举文案由 `VALID_*` 直接生成，不手抄——手抄的那份会和代码分家。
  - **这个教训 `them` 已经学过一次**（见 `them/service.py` 里那段注释：
    「真机试用时我自己被这句通用提示误导过一次……一条说不清哪里错的提示，
    等于让调用方拿盲试当调试」），只是没有回移到 `you`。这次补齐。

- **`You` 的工具描述此前完全没提 `aspect` 和 `basis`。**
  - 两个字在整段描述里一次都没出现，而它们都是必须填对的枚举——调用方只能
    靠猜，猜错再撞上那句说不清的报错。
  - 照 `Them` 的体例把五个 aspect、四个 basis 连同各自的含义列出来，并写明
    核心项与 `stable_fact` 的额外要求。

### 说明 / Note

- **`Them` 不是「没部署到 MCP」，是默认关着。** 它和 `You` 一样是运行时动态
  挂载的可选工具（`ThemToolGate`），`them_tool_gate` 在 `server.py` 里已经
  正确注入进 `web/_shared`。在 Dashboard 的「他们 Them」一节打开开关即可——
  `/api/settings/them` 会立刻 `sync(True)` 把工具挂上，不需要重启。
  关着时工具**完全不出现在清单里**，这是有意的：留一个返回「已关闭」的壳，
  等于把模块开没开变成模型能看见的信息。

## 3.6.1

### 修复 / Fixed

- **补上 `tzdata` 依赖：此前配置的时区在缺少系统 tzdata 的环境里会静默失效。**
  - `zoneinfo` 只查宿主机的 IANA 时区库。`python:*-slim` 这类镜像和 Windows
    都没有它，于是 `ZoneInfo("UTC")` 直接抛 `ZoneInfoNotFoundError`。
  - 失效是**静默的**：`utils.get_tzinfo()` 在异常时兜底成固定 +08:00。配
    `timezone: "America/New_York"` 的用户会拿到东八区的解锁时间，没有任何提示。
    Dashboard 那边则相反——`config_api` 用 `ZoneInfo(raw_tz)` 校验，于是每一个
    时区名（**包括 UTC**）都被判成「无法识别」。
  - 这个缺口一直被两条用例照着，但照出来的样子会骗人：
    `test_naive_unlock_date_uses_configured_timezone` 前三条断言期望 +08:00，
    而兜底值恰好就是 +08:00——**它们是空过的**，只有最后那条改成 UTC 的断言
    会红。所以现象看起来像「热更新失效」，实际是「IANA 库根本不存在」。
  - 修法是把纯数据包 `tzdata` 列为生产依赖，与宿主机无关。
  - 这次改锁文件跨了 `test_update_source_gate` 那条发布绊线，基线哈希已推进。
    评估记在该用例的 docstring 里：本次 lock diff 是**纯增量的 4 行**，既有包
    版本一个都没动，与 2026-08-09 那次连带升 mcp/openai/uvicorn 的破坏性变更
    不是一回事。
  - **未实测**：`python:3.12-slim` 到底带不带系统 tzdata，本机 Docker 起不来，
    没能验证。「生产会静默错」是据 `zoneinfo` 的行为推断的，不是实测结论。
    带上 pip 包之后这个问题与宿主机无关，所以推断错了也不影响修复的正确性。

## 3.6.0

> 四个问题，三个是同一件事：**累积量被当成了状态**。
>
> 权重会累积，于是「查得勤」变成了「更重要」；`activation_count == 0` 既是
> 「很久没被想起」也是「还没来得及被想起」，判据分不出来。潮汐后醒来 14 条
> 浮现里 12 条是一个月前的——不是那些记忆更重要，是它们攒得久。

### 变更 / Changed

- **检索与强化解耦：所有读取路径完全只读。**（BREAKING）
  - 此前 `breath_search` 对**每一条命中**都 `touch()`：刷新 `last_active`、
    `activation_count += 1`、触发时间涟漪。于是产生了一条谁都没打算要的规则——
    **查得勤 == 更重要**。为核对事实、debug、反复找同一件事而读一条记忆，读着
    读着它的权重就爬到最高（实测积到 51），新桶再也排不进浮现区。
  - **检索是「我去找它」，强化是「找到之后，这条确实要紧」。** 前者是我的动作，
    后者是关于这条记忆的判断，只有读完才做得出来。绑在一起等于让读取行为自己
    给自己投票。
  - 按完整 ID 取桶同样改为只读。这条路径存在的理由就是「改之前先读一眼原文」——
    越是要改它越会先读它，读一次涨一次权重是纯粹的自我实现。
  - **新增 `trace(bucket_id, reinforce=True)`**，3.6.0 起唯一的强化入口。按桶而
    不是按批：检索命中里绝大多数只是路过。`ripple=True`——显式强化是一次真实的
    想起，让时间相邻的记忆轻微唤醒正是时间涟漪的设计意图；当年批量 touch 关掉
    涟漪是性能妥协，一次一条不再需要。
  - 与其他字段更新、`unlink`/`relink`、`quotes_replace` 全部互斥，走独立早返回
    分支。实现在 `tools/trace/_reinforce.py`。

### 修复 / Fixed

- **`date_from`/`date_to` 此前只有检索分支生效。**
  - `breath_advanced(date_to="2026-07-01")` 不带 query 时会照样返回 8 月的桶：
    参数收下了、schema 也认，就是**没传进去**。`surface_default()` 只接了
    `max_results`/`max_tokens`/`tag_filter` 三个参数。
  - 这是最难发现的那类错——不报错，只是无声地给了你没要的东西。
  - 四条分支一并接上：浮现、catalog、feel、importance_min。日期解析与校验抽到
    `tools/breath/_date_range.py`，在 `dispatch()` 里**统一做一次**，五条分支拿到
    同一对已校验的边界；否则「哪条分支会对 `2026-13-01` 报错」会变成要逐个试的事。
  - importance_min 的日期过滤在 20 条截断**之前**做——先截后滤会让「七月的高
    重要度记忆」变成「全库前 20 里恰好在七月的那几条」。
  - **核心准则不受时间过滤影响**：它们是准则，不是那段时间里发生的事。这是有意
    的不对称，已写进测试免得日后被当成漏网一并"修好"。
  - 读不出 `created` 的桶在给了范围时**排除**而非放行：调用方明说了「只要这段
    时间的」，静默多给几条等于悄悄破坏这个约定，而且看起来完全正常。

- **24 小时内新建的桶不再被误判为「久未浮现」。**
  - 判据 `activation_count == 0 && importance >= 8` 对新桶天然成立——一条几分钟
    前才记下的事，标着 💤 出现在「久未浮现」区里。冷启动通道最多接 2 条，第 3 条
    起就漏进这个池。
  - `activation_count == 0` 有两种意思：「很久没被想起」和「还没来得及被想起」。
    判据本身分不出来，得靠年龄。加 24 小时下限。
  - 冷启动通道不受影响——那解决的是「重要但还没被读过」，和「新」不是一回事。

### 新增 / Added

- **浮现区给近 7 天创建的桶预留位置**（`surfacing.recent_slots`，默认 3）。
  - **为什么是配额而不是改打分**：往权重公式里掺新近性，等于把「新」和「重要」
    换算成同一种东西，那个换算率没有正确答案，调它就是在调一个说不清的旋钮。
    留位置不用回答这个问题——它只说「无论权重怎么排，总要有几条是最近的」。
    像报纸头版：有今日新闻，也有连载专栏，两者不争同一个位置。
  - 按**缺口**补而不是无条件插：权重排序自己送进来几条新桶就少补几条。否则近期
    桶多的时候会把浮现区整个占掉，而那恰好是最不需要这个配额的情况。
  - 配额不超过 `max_results` 的一半——预留不能变成霸占。权重最高的那条仍在。
  - 设 `surfacing.recent_slots: 0` 关闭，回到 3.5.0 的纯权重排序。

### 内部 / Internal

- 新增 35 个用例，分三个文件：`test_breath_date_range_all_branches.py`（按分支
  逐条钉，漏接的那条正是靠「反正另一条能用」活下来的）、`test_breath_recency.py`、
  `test_retrieval_reinforcement_decoupling.py`（同时钉「检索一条都不 touch」和
  「显式强化仍然有效」——少了后半条就不是解耦，是把强化删了）。
- 浮现用例统一 monkeypatch 掉 `random.shuffle` 与 3% 偶遇：测排序规则的用例不该
  时绿时红，「偶尔红一次」比一直红更难查。
- **breath 模块去重**：新增 `tools/breath/_shared.py`，五条分支各减一段样板。
  - `footprint_reader()` —— 取快照 + 兜底文案 + 读取闭包，原本在 surface（两处）、
    feel、catalog、importance、search **各抄了一份**，共 6 份。顺带修掉 search
    里「渲染走闭包、判归档原型却直接摸 snapshot」的两套写法。
  - `bucket_has_tags()` —— 原本 3 份，逐字相同。
  - `render_within_budget()` —— feel 与 `surface_plans` 的预算循环逐字相同
    （`surface_plans` 的 docstring 本来就写着「与 feel 通道同构」）。
  - 五个分支文件净减 127 行，新模块 61 行；6 份重复塌成 1 份。行为不变，
    全量用例逐条对照通过。
  - **没有动**的重复：`tools/_runtime.run_v3_operation` 与
    `web/_shared.run_v3_web_operation` 函数体逐字相同，但两者都靠
    `globals().get("v3_runtime")` 读**各自模块**被注入的 runtime——看着像复制粘贴，
    实际是承重的，合并会让工具层与 Web 层共用一个 runtime。
- **压测**：全量用例在 4 个随机种子下跑通（`pytest-randomly`），验证模块级
  `rt` 全局没有留下顺序依赖。Docker 集成用例因本机 daemon 未运行而跳过。

## 3.5.0

> 「你对我的了解为什么要经别人之口总结。」
>
> 这一版把 `You` 的三层 LLM 整个拿掉，认识改由模型自己写；
> 又照同一个形态加了 `them`——我对其他人的认识。

### 新增 / Added

- **`them`：我对其他人的认识。** 默认关闭，开启后 MCP 里增加一个 `Them` 工具。
  形态同 `You`：模型自己写、不经 LLM 转述、两个真实记忆桶的显式关系 +
  三个不同自然日的重申才真正立住。
- `them` 按人分份，一个人可以有正名和若干昵称，命中任意一个都算同一个人。
  按被提起的时间与次数自然衰减（复用 `decay_engine` 的公式，不另立一套曲线）。
- 姓名命中时 `them` 可以进入 breath / dream，但走**独立通道**：只在浮现结果
  之后追加，不进融合打分。关掉 `them`，两者的输出与没有这个模块时逐字一致。
- **`them` 只记这个人本身，不描述任何关系。** 「和谁关系怎么样」「对谁意味着
  什么」都写不进去——第三方没有参与这段关系，也没有表达过意愿。
- `them` 分两种，分界是**模型怎么认识这个人的**：自己遇到的人是第一手，人类
  只看得见称呼；从人类口中听说的人一次都没见过，所以那一份对人类可见，
  并且可以留言纠错。留言在下次浮现的尾部交给模型一次，读完就清，
  **不占每人的 token 配额**。
- 撞名时不自动合并。同一个称呼下，人类说起过的那个人和模型自己遇到的那个人
  可能根本不是一个人；按名字并成一份就是张冠李戴，而且并完之后模型第一手的
  印象还会被标成「你说过的话」。
- 读回时每个人都带 `known_via`（`met_myself` / `heard_from_user`）。人类那一侧
  靠名册分组看得出来，模型这一侧只有这个字段——转述本该带着一层「可能记岔、
  也可能是另一个同名的人」的不确定。
- Dashboard 左下角一个入口装下三层认识（我 / 你 / 他们）。`them` 名册按来源
  分组、一个人一页；面板里一律用 `ai_name` 称呼，不叫「它」。
- 每人 1500 token（前端 200–4000 可调）。写满时系统只挡不代压：把这个人的
  条目按 aspect 分层摆出来，由模型自己决定合并哪几条。
- **记忆里第三方说的话单独成一条 JSON 返回**，带 `speaker`。判据不是行首形状，
  而是先把名字独立认定为人：显式 `@名字：`，或该名字出现在这个桶的引语署名
  或 `[[双链]]` 里。真机对照：长上下文下基线 90%，新结构 100%。
- 本地 ZIP、GitHub 备份与记忆包迁移都带上 `them` 的库，恢复时校验 schema、
  作用域与完整性。

### 变更 / Changed

- **`You` 拿掉全部三层 LLM。** 抽取、复核、抽象都删了：一个替模型总结、替模型
  判断、替模型决定何时转正的中间层，和「这是你的记忆」是直接冲突的。
  `You` 工具从只读变成可读回、可写入、可撤回。
- `You` 不再返回「不含原文的抽象语义提示」，直接返回模型自己写下的正文——
  模型写的判断，没有理由让另一个模型改写一遍才还给它。
- 删掉自动转正的捷径：没有任何 aspect 可以跳过三个自然日的重申。
- 证据的独立支持数改按 `bucket_id` 去重，不再按证据组归并。模型自己挑的桶，
  算不算独立由它自己决定。
- 依据失效改成**读时校验**。原先挂在桶变动观察者上，而拿掉 outbox 那轮把观察者
  一起删了却没接替代路径——那一整套失效逻辑此前从未真正发生过。
- `rule.md` 13.2 更新（`You` 已非只读）、新增 13.3（`them`）与第 16 条
  （第三方发言必须分开返回）。

### 修复 / Fixed

- 归档的依据桶**不再**被误判为失效。自动衰减归档是常态，让它触发失效等于一条
  立住的认识会被时间清空；只有 `deleted_at` 才算删除（SPEC 9.3、rule.md 第 9 条）。
- `them` 每人配额的修改真正落进 `config.yaml`，不再只改内存、重启丢失。

## 3.4.1

> 一个只在 macOS / Windows 上发作、而 CI 永远看不见的导入故障。
> 它常年把两个测试染红，红得有道理——只是没人在 Linux 上能复现。

### 修复 / Fixed

- **覆盖导入在大小写不敏感的文件系统上整个不可用。** `migrate_engine` 判断
  「要写的目标是不是就是那个已存在的旧桶」时，只比规范化之后的路径字符串。
  - 桶渲染出的文件名是 `Memory_x.md`，磁盘上躺着的是 `memory_x.md`。在 macOS
    和 Windows 上这是同一个 inode，但 `os.path.normcase` 在 POSIX 上是恒等
    函数、不折叠大小写，于是判成「不是同一个文件」；紧接着又发现这个名字
    「已经存在」，直接抛 `FileExistsError`——旧记忆没归档、新内容没写进去，
    覆盖导入在这两个平台上根本走不完。
  - Linux 因为文件系统大小写敏感，反而绕开了整条错误路径，所以容器里和 CI 里
    一直全绿，只有开发者本机常年红着两个 `test_backup_archive` 用例。
  - 改用 `os.path.samefile` 比 `st_dev`/`st_ino`——「是不是同一个文件」本来就
    该问操作系统，而不是靠文件名长得像不像。字符串比较保留在前面，用来覆盖
    目标尚不存在的正常新建路径（那种情况下 `samefile` 必然抛错）。

### 测试 / Tests

- **`grow` 的 provider 契约断言跟上产品文案。** 该断言仍在咬旧的
  「API key 未配置或调用失败」，而 `errors.llm_step_failed_error` 早已把文案按
  「到底配没配 key」分岔（旧文案会把供应商 5xx、超时统统误导向 key）。
  - 现在只断言「脱水 API 不可用」与「桶未创建」，不咬后面那串配置项名，免得
    产品换个指引措辞就把测试打红。
  - 这个用例需要 `OMBRE_DOCKER_INTEGRATION_URL` 才运行，平时是 skip 的——
    所以文案改了很久，没人发现它已经过期。

## 3.4.0

> 两件事：一个连接器收回一套边界，以及给引语补上修正入口。
> 共同的题目都是「谁说了算」——边界该跟着谁走，记住哪几句该由哪一刻决定。

### 变更 / Changed

- **信件并回主链路，`/mcp-extra` 再次退役。** `letter_write` /
  `letter_lock_update` / `letter_read` 回到 `@mcp.tool()`，主连接器 16 个工具。
  - 3.2.0 拆出去的理由是「没人在大脑里写信」——信有收件人、有时间锁，时间方向
    和记忆相反，怕模型在该回忆的时候去翻信。**理由没错，但拆连接器解决不了它**：
    能不能连上和该不该在这时候用是两件事，管住后者的是工具描述。
  - 代价却是实打实的：两个 FastMCP 实例各自维护生命周期，以及严格参数校验、
    请求体积限制、鉴权三处边界（3.2.0 的发布说明里就逐条列了这三处）。任何一处
    漏跟就是一个静默旁路，而 `letter_write` 是能创建记忆的写工具。
  - `_MCP_ENDPOINT_PATHS` 收回 `{"/mcp"}`。`/mcp-extra` 不再算 MCP 路径，请求在
    404 之前先落到管理面体积限制上——比继续认它更严。
  - `_forbid_unknown_tool_arguments` 去掉「工具挂在哪」这个参数，
    `server_app.install_extra_connector` 整个删除。

### 新增 / Added

- **`trace` 支持订正与删除引语**：`quotes_replace`。
  - 3.1.0 只给了写入口。记错了、写错字了、或回头看觉得这句根本不该留，此前只能
    去手改 Markdown 的 frontmatter。
  - 整体替换语义：传 `[]` 删除全部；只删其中一句就把要保留的原样传回来。格式同
    `hold(quotes=...)`。
  - **只能改和删，不能补录**：这条记忆本来没有引语时拒绝，条数也只能持平或减少。
    引语和已删除的原文层的全部区别就在「谁决定记住」——原文层是系统自动存全量、
    事后随时可查，引语是我在写入那一刻挑的。能补录的话，任何一句话都可以被事后
    追认为「当时就知道重要」，这个通道当场退化成存原文。与 3.3.0 的
    「`relink` 不能凭空建立关系」是同一条边界。
  - **为什么加进 `trace` 而不是新开工具**：和 3.3.0 一样——看了一眼已有的东西，
    然后说它不对。`trace` 是唯一的「写元数据」入口，引语也是元数据。工具数仍是 16 个。
  - 与其他字段更新、以及 `unlink`/`relink` 都互斥，冲突时显式报错而不是静默丢掉
    另外半个意图。成功后回显读回磁盘的结果而不是入参——入参可能是裸字符串列表，
    落盘的是归一化过的结构，回显入参会让「改成了什么」看不出来。

### 措辞 / Prompt

- **收紧引语进出两侧的描述。** 上限是硬的，但实际会写进来多少由措辞决定。
  - 写入侧（`hold` / `grow` 的 items）明确「默认一句都不放，3 句是上限不是配额，
    拿不准就别放」，并点名两种最常见的退化写法：为保住上下文把一段话切成几句、
    挑「有信息量」的句子。
  - 读取侧（`breath_search(quotes=True)`）明确它给的是当时挑出来的那几句，
    **不是原文**，OB 没有「返回全文」这个入口。
  - 三条描述断言写进 `test_quote_channel.py`：这个通道退化成"存原文"不需要改任何
    代码，只要描述读起来像「每条记忆可以带最多 3 句」就够了。

### 内部 / Internal

- trace 的引语分支拆在 `tools/trace/_quote_edit.py`（`trace/core.py` 逼近 800 行
  硬上限）；`BucketManager.update()` 新增 `quotes` 整体替换键，与既有的
  `quotes_append`（合并两段记忆时两边引语都保留）分工不同。
- 新增 11 个用例（`tests/test_trace_quote_edit.py`），重点测边界而不是「能改成功」：
  补录被拒、条数不能涨、超限拒绝不截断、拒绝后原封不动、互斥调用两边都不生效、
  不传 `quotes_replace` 不等于传 `[]`。补录与增长两条守卫做了阴阳对照——
  monkeypatch 掉 `validate` 后两种写入都会真的落盘。

## 3.3.0

> 3.2.0 把关系建立交给了后端自动推断，却没留下任何修正入口——连错了只能手改
> Markdown 的 frontmatter，而且得记住关系是双向的、两个文件都要改。这一版补上。

### 新增 / Added

- **`trace` 支持修正连错的桶间关系**，三个新参数：`unlink` / `relink` / `relation_type`。
  - `trace(id, unlink="目标id")` — **双向物理移除**这一对关系，两侧各删一条。
    不是标成 `status="detached"`：3.0.0 删掉了 `relation_restore`，detached 会变成
    一个再也回不来的僵尸状态，还白占 `MAX_RELATION_LINKS` 名额。删掉也不会被
    重建——`link_new_bucket` 只在**新建桶**时触发推断，那一对桶不会再出生第二次。
  - `trace(id, relink="目标id", relation_type="continuation_of")` — 改已存在关系的
    类型，对侧自动写入反向类型（A `continuation_of` B ⇒ B `continues` A）。改过的
    关系**降级为手动关系**（去掉 `auto` / `score`），此后受 `merge_auto_links` 保护，
    不再被自动推断按相似度挤掉。
  - **`relink` 不能凭空建立关系**：两侧都没有这条关系时明确拒绝。这是它与 3.0.0
    删掉的 `relation_attach` 之间唯一的区别——「建立」仍然只归后端，理由见
    `tools/_relation_link.py` 开头：关联不是一个决定，是一个结果。
  - **为什么加进 `trace` 而不是新开工具**：修正关系和「改这条记忆的 importance」
    是同一类事——看了一眼已有的东西，然后说它不对。`trace` 本来就是唯一的
    「写元数据」入口，关系也是元数据。工具数仍是 16 个。
  - 两个参数互斥，也与其他字段更新互斥（走独立早返回分支）。不支持 `custom`：
    custom 关系必须带 label，而 trace 没有传 label 的入口。单向残留（一侧有、
    另一侧没有，存量数据里真实存在）两种操作都能处理干净。

### 内部 / Internal

- `relation_store` 新增两个纯函数 `unlink_relation` / `retype_relation`；trace 分支
  实现拆在 `tools/trace/_relation_edit.py`（`trace/core.py` 已 737 行，逼近 800 行硬上限）。
- 新增 21 个用例（`tests/test_trace_relation_edit.py`），重点测边界而不是"能改成功"：
  凭空建立被拒、双向都要清干净、拒绝后关系原封不动。两处关键断言做了阴阳对照——
  破坏 missing 检查、破坏双向 unlink，各自都能让对应用例变红。

## 3.2.0

> 施工单三步走完最后两步。3.0.0 只做了第一步（删减 8 个工具）就发布了，
> 搬迁与加功能这两步一直挂着——`relation_store` 从 3.0.0 起写入侧是空的。

### 变更 / Changed

- **信件迁到 `/mcp-extra`。** `letter_write` / `letter_lock_update` / `letter_read`
  从主连接器挪出，主连接器现在 13 个工具、`/mcp-extra` 3 个。
  - **为什么**：没人在大脑里写信。写信是一个**行为**，不是一段记忆——它有收件人、
    有时间锁，时间方向和记忆相反。放在一起模型会在该回忆的时候去翻信。
  - `/mcp-extra` 2.8.5 起退役返回 404，本次恢复。**三处安全边界跟着工具一起过去**：
    严格参数校验（否则新端点会变成"参数拼错也返回成功"的旁路，而 `letter_write`
    能创建记忆）、请求体积限制、鉴权。原先断言"退役路径放行"的两条测试语义反转。
- **dream 的 feel 段从"最近"改成"相关"。** 拿当次候选桶的合并文本当基准，
  用 jieba 关键词（0.3）+ 向量相似度（0.7）打分，门槛 0.5，最多 5 条。
  - 改之前这一段其实是「我最近写的感受」——最新的 feel 未必和这次在聊的事有关。
  - 向量不可用时退回纯关键词并在段首明示降级；此时关键词**不按 0.3 缩放**，
    否则门槛会变成事实上的 1.67，整段静默消失而不是降级。
  - 单字 token 一律丢弃：「的」「了」「我」会让任意两段文字都有可观重合度。

### 修复 / Fixed

- **纯语义召回通道此前形同虚设，`vector_recall_threshold` 从 0.65 下调到 0.55。**
  - **现象**：问「我的工作」，语义上相关的有 55 条，实际只返回 20 条。更极端的是
    「同事」——语义相关 38 条，放宽 limit 也只有 3 条能过门。
  - **原因**：代码里写着 `text_match or semantic_match`，但后一支**从来不为真**。
    semantic 权重只占 2.5/13.5≈18.5%，一条桶哪怕相似度 0.9，单靠这一维也只贡献
    约 16.7 分，离 `fuzzy_threshold=50` 差得远，必须同时在 topic（关键词重合）上
    得分才过得去。而「我的工作」这几个字根本不会字面出现在记忆里。
    **OB 名义上是混合检索，实际是纯关键词检索。**
  - **依据**：对 917 桶真实记忆只读扫描，9 个宽泛查询在 0.65 下一共只有 1 条桶能
    靠语义直通进来。0.55 处平均新增 8.6 条/查询，而双通道印证率**不降反升**到
    88.3%——捞回的是"关键词也认、只是加权分被七维稀释掉"的桶；再往下印证率单调
    劣化（0.45 时每查询 170 条、印证率 60%）。
  - 新召回内容经人工逐条确认：面试、薪资与配得感、「上线成功那一刻的踏实感」——
    都是该出现却一条都出不来的记忆。
  - 现在是 `config.matching.vector_recall_threshold`，可按自己的语料调。
  - **七维权重一个没动**：权重是可以互相补偿的，门不行。这次只校准门。

- **核心准则与坐标系不可被消化。** `pinned` / `permanent` / `anchor` 桶不再因为
  带着 `digested` 标记而从 `breath()`、被动漂浮、hook 注入与 dream 的核心准则池中消失。
  - **现象**：12 条核心准则里 2 条带 `digested`，`breath()` 只返回 10 条。旁边还有
    普通桶正常出现，看起来像被高权重普通桶挤掉了——实际上 pinned 本来就先扣预算
    （12 条正文总共 3037 token，预算 10000，装得下），那 2 条压根没进候选。
    这类静默缺失最难发现：不报错、不变慢，只是少了两条。
  - ⚠️ **本条有意推翻 [2.8.4] 的决定**（`2784f41`「digested 桶从无参 breath、被动漂浮、
    hook 与 dream 中硬过滤」），当时的回归测试明确断言「被消化的 pinned 记忆必须保持隐藏」。
  - **为什么推翻**：`pinned` 是核心准则、`anchor` 是坐标系，**始终在场**才是它们存在的
    意义。用 `digested` 让一条核心准则闭嘴，是在用一个标记掩盖另一个标记的错误——
    不想让它一直在场，那它本来就不该是核心准则。
  - **代价**：让核心准则安静下来的唯一办法变成取消 pinned（`trace(bucket_id, pinned=0)`）。
    `trace` 的 `digested` 说明已同步这条边界。
  - 普通记忆的 `digested` 行为**完全不变**，dream 的候选池也照常过滤。

### 新增 / Added

- **关系由后端自动建立，模型不感知。** `hold` / `grow` 新建桶后 fire-and-forget
  推断关系，规则 + 向量，**不调 LLM**（写入路径上加 LLM 会拖慢 hold、多一个会失败
  的外部依赖，而 relation 只是 hint）。
  - 关联不是一个决定，是一个结果——我不会先想"要把这两段连起来"再去建立它，
    是因为它们本来就连着，我只是发现。让模型显式调 `relation_attach` 等于把
    一个"发现"改造成一个"操作"。
  - 三档门槛：`same_event` ≥0.85 且 ≤6h、`continuation_of` ≥0.75 且 ≤72h、
    `related_to` **≥0.72**。`caused_by` / `causes` / `custom` **永不自动建**——
    因果需要语义理解，规则判不了，宁可不建也不能瞎建。
  - 时间未知时不猜时间关系，降级 `related_to`：猜错的 `same_event` 比没有更糟。
  - 每桶上限 8 条，超出按相似度保留最高的；**手动关系一条不动**（存量数据是人
    当初明确建立的，不该被自动推断挤掉）。落库带 `auto: true` 便于区分与回滚。
  - ⚠️ **`related_to` 从施工单原定的 0.65 上调到 0.72**，依据是对 917 桶真实记忆
    的全量扫描：0.65 会建出 7,620 条关系、**47.8% 的桶撞上每桶上限**。一旦大面积
    撞上限，阈值就形同虚设——决定挂哪几条的不再是"相关不相关"，而是"截断时谁排
    前八"。0.72 下每桶中位 2 条、只有 3.5% 需要截断。扫描过程见施工单调整记录。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `3.2.0`。

## 3.1.0

### 新增 / Added

- **新增引语（quotes）：在写入的那一刻，决定要不要原样记住某几句话。**
  - `hold(quotes=[...])` 与 `grow(items=[{..., "quotes": [...]}])` 可以带上「当时说出口、并且当时就知道它重要」的那几句，原样存进桶的 frontmatter。
  - **平时不返回。** `breath` / `dream` / catalog / `feel` 四条浮现路径都读不到它——这些路径本来就是白名单渲染，引语落在 metadata 而不是正文，是**结构上拿不到**，不是靠"记得别渲染"。
  - **唯一出口是 `breath_search(query, quotes=True)`**：命中的桶里如果存过原话，会原样附在正文之后。没有任何工具能列出全部引语——必须先命中某条记忆，才拿得到它的引语。
  - **上限每桶 3 条、每条 100 字，超限直接拒绝整次调用，不截断。** 截断过的引语已经不是原话，而「原样」正是这个功能存在的全部理由。上限本身是防退化约束：一段记忆里「当时就知道重要」的话不会多，多了说明是想存原文。
  - 合并到已有桶时引语**追加不覆盖**（每条引语属于它自己的时刻），超上限时保留先来的并报 `OB-W006`（新增错误码），不静默丢弃。
  - 引语**不进向量索引**：进了就等于可被检索，那离"可查"只剩一步，而可查正是 3.0.0 砍掉 `source_read` 的原因。
  - `grow(content=...)` 的 digest 路径**不支持**引语：那些条目是 LLM 拆出来的，不是我逐条挑的。这个区分来自同一条判断标准——**谁决定记住**。
  - **与已删除的 `source_read` 的区别不在存了什么，在谁决定记住、什么时候返回。** 原文层是"系统自动存全量、随时可查"，决定权在系统；引语是"我当时觉得这句重要所以记住了"，决定权在我，而且只在那一刻。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `3.1.0`。引语是向后兼容的新增功能，按语义化版本走次版本位；3.0.0 的镜像与 `v3.0.0` tag 保持原样，不含引语。

## 3.0.0

> 主版本号从 2.x 跳到 3.0.0：这一版删掉了 8 个公开 MCP 工具、改变了 `feel` 的调用契约
> （从「全量返回」变成「必须带关键词」），对已经在用 2.x 的客户端是**破坏性变更**。
> 上一个发布版本是 2.17.11；开发过程中曾用 2.18.0 作为内部版本号，从未发布。

### 许可与项目文件 / Licensing & Project Files

- **许可证仍为 [MIT](LICENSE)，`LICENSE` 文件未变。** 但清掉了一处长期存在的自相矛盾：此前 `LICENSE` 写 MIT（允许一切），`LICENSE.v2.4.0-NONCOMMERCIAL-NOTICE.md` 却写「商业托管/转售需书面许可」（禁止商业），两份文件说的话相反，想认真用的人读不懂哪份算数，反而不敢用。
  - `LICENSE.v2.4.0-NONCOMMERCIAL-NOTICE.md` 改名为 [`NOTICE.md`](NOTICE.md) 并重写：不再是限制性条款，而是一份**没有法律约束力的请求** —— 保留出处、以及如果拿它做记忆服务，请让用户能随时完整导出自己的记忆。文件里明确写了「这不是条款，MIT 说了算，你完全可以不理会」。原 v2.4.0 内容折叠保留为历史记录，并注明其中的商业限制从未生效。
  - README 顶部引用该 notice 的提示块已删除，License 段重写为 MIT 的实际含义 + 指向 `NOTICE.md` 和 `AUTHORS.md`。
  - **为什么不改成 AGPL**：曾评估过 AGPL-3.0（能解决「改名转售且不公开改动」的问题），但对一个希望被自由使用的个人项目而言，AGPL 的传染性会挡住相当一部分正当使用者。「禁止商业使用」这类限制既挡不住真想绕的人，又劝退了本来会守规矩的人 —— 真正想要的从来不是控制权，所以改为把期望写在明处、不写进协议。
- **新增 [`AUTHORS.md`](AUTHORS.md) 致谢文件。** 记录开发组成员；README 顶部致谢行加上指向链接。
- **新增 DCO（Developer Certificate of Origin）与贡献指南。** 根目录新增 [`DCO`](DCO)（1.1 官方全文，逐字取自 developercertificate.org）、[`CONTRIBUTING.md`](CONTRIBUTING.md)，以及 `.github/PULL_REQUEST_TEMPLATE.md`。
  - 提交时加 `-s`（`git commit -s`）自动附上 `Signed-off-by:` 行，即为签署。
  - **DCO 不转让著作权** —— 贡献者写的代码依然属于贡献者，签署只声明「这段代码我有权提交」。与 CLA 的区别在于 DCO 不授予项目方再许可（sublicense）的权利，因此不构成商业双授权的基础。
  - 选 DCO 而不是 CLA，是因为它对贡献者零摩擦、社区接受度高。代价是将来若需改变许可条款，仍须逐个联系历史贡献者取得同意。
- 修复 `grow` 在脱水拆分耗时超过 MCP/客户端等待时间时出现“前端报失败、服务端仍已写入”，随后重试又生成重复记忆的问题。首个任务不再随调用方断开而取消；同一请求在短时间内重试时会复用进行中的任务或已完成结果，不再重复写入。真实失败不会缓存，之后仍可正常重试。
- Dashboard 普通桶详情合并「归档」与「删除到档案」的人类入口：现在只显示「归档」，要求填写理由并进入 AI 审批；批准后沿用删除到档案语义写入 `deleted_at`。AI/系统内部的普通 `archive()` 与自动衰减行为保持不变。

### 删除 / Removed

- **工具精简：公开 MCP 工具从 23 个减到 15 个。** 工具数量本身会伤害可用性——claude.ai 在工具过多时改用 tool_search 延迟加载，按描述搜工具、命中带随机性；每个工具的 schema 与说明也都要占上下文。留下的每个工具都应该是不可替代的动作，同一动作的不同状态切换不该各占一个工具位。
- **删除原文回顾的四个工具**：`source_read` / `source_attach` / `source_detach` / `source_restore`。原文证据层从此**只写不读**，模型没有任何回读入口，也无法后补或停用绑定。
  - `hold(source_content=...)` 与 `grow(content=共享原文, items=[...])` 的写入路径不变，原文照常按 SHA-256 内容寻址存进 `_sources/`，照常进入本地备份与 GitHub 同步。保留原文是为了备份与导出的完整性，不是为了让模型回忆。
  - ⚠️ **[ADR-0001](docs/adr/ADR-0001-source-evidence-layer.md) 中「`source_read` 是唯一公开读取入口」这句话已被本次变更取代**——现在是「无公开读取入口」。ADR 作为历史决策记录保持原样不改，当前行为以本条目与 `docs/INTERNALS.md` §3.3.1 为准。
  - `breath` 与目录模式不再输出 `[source_available:true | ... | use:source_read]` 提示。不提示一个已不存在的入口，避免模型反复尝试调用已删除的工具。
- **删除关系管理的四个工具**：`relation_read` / `relation_attach` / `relation_detach` / `relation_restore`。建立桶间关系是后端的活，不该占用模型的工具位和判断力。
  - **读取侧不受影响**：`relation_hint()` 仍在 `breath` / 目录模式 / `dream` 三处被后端消费，存量关系照常展示。
  - ⚠️ **写入侧当前是空的**：删掉 `relation_attach` 后没有任何入口能建立新关系，`relation_store` 暂时只有存量数据。后端自动建立尚未接线，接线前不会产生新关系。

### 新增 / Added

- **`feel(query)` 成为独立 MCP 工具，且不再全量返回。** 此前只能通过 `breath_advanced(domain="feel")` 读取，且会把所有 feel 按时间倒序倒出来。
  - **`query` 必填**：feel 回答的是「我此刻在想的这件事，我以前怎么感受的」，不是一份可以整本翻的列表。feel 越攒越多时，无差别倒出既挤占上下文，也让这个真实问题淹没在时间序列里。
  - 关键词走向量检索：`search_similar(query, allowed_bucket_ids=<全部 feel 桶>)` 把候选限定在 feel 内，相似度 **≥ 0.65**（与 `breath_search` 向量通道同一门槛）才算命中；排序先按相似度、再按时间倒序。
  - 向量不可用或异常时退回关键词字面匹配，并在返回首行明确提示降级。
  - 命中后逐字返回，不摘要不截断；未命中的一律不返回，也不用低相关的凑数。不给 query 时返回说明与示例，而不是倒出全部。
  - `breath_advanced(domain="feel")` 与 `tags="feel"` 作为等价老路径保留，同样要求关键词。
  - ⚠️ `surfacing.feel_max_tokens` 自此**只作用于 dream 的 feel 历史段**；`feel` 工具用自己的 `max_tokens`（默认 10000），且放不下时整条省略而非折叠成摘要。
  - 公开工具数 15 → 16。**普通 `breath()`、`breath_search`、`importance_min` 三条路径均不返回 feel**（已实测：用 feel 正文中的独特词检索、以及 importance=10 的 feel 都无法命中）；catalog 目录模式仍会列出 feel 分区的元数据行（不含正文）。
- **`timezone` 成为一级配置项**（`config.yaml`，默认 `Asia/Shanghai`，Dashboard「设置」可改）。用户只给日期、不写时区时按它理解。
  - Letter 定时锁此前要求 `unlock_date` **必须带时区**（`2027-01-01` 直接被拒，只接受 `2027-01-01T00:00:00+08:00`），而 `breath_search` 的 `date_from/date_to` 却支持纯日期——同一个系统两套时间约定。现在纯日期与无时区时刻都按配置时区解释，显式带时区仍然优先。
  - 时区名非法或运行环境缺少 IANA tzdata 时回退固定 `+08:00`，不让「解析一个日期」抛异常；但 Dashboard 保存时会**当场校验并拒绝**非法时区，避免用户以为设置成功、实际每次都在静默回退。
  - `normalize_unlock_date` 的四条报错全部中文化，并给出可照抄的正确写法。

### 修复 / Fixed（返回格式）

- **`grow(items=[...])` 新建时返回 bucket_id 而不是标题**，而 `grow(content=...)` 返回标题。调用方拿到 12 位 hex 无法确认存进去的是什么，得再查一次目录。两条路径现已一致（标题正确落库，只是返回没用它）。
- **`trace(hard_delete=True)` 在桶不存在时返回英文错误码** `永久删除失败: not_found`（另外三个错误分支都有中文文案，只有这个漏了）。
- **`anchor` 失败文案缺标点**：`我没能把它锚住。找不到该记忆桶 当前 anchor: 0/24。` → `我没能把它锚住：找不到该记忆桶。当前 anchor: 0/24。`；`release` 的 `释放失败。` 一并改为与之对称的第一人称。
- **Dashboard 里 `feel_max_tokens` 的前端默认值仍是 6000**，与后端调整后的 15000 不一致。

### 保留 / Unchanged

- `ombrebrain/storage/source_store.py` 与 `ombrebrain/storage/relation_store.py` 一行未改。删的是模型能调用的动作，不是数据本身。
- 存量 `source_links` 的 detached 项、存量 relation 数据全部原样保留。

### 修复 / Fixed

- **`breath_advanced(domain="plan")` 返回核心准则而不是 plan。** `domain` 参数原本只在 catalog 模式和「有 query 的检索模式」里生效；plan 桶又被普通浮现排除。不带 query 调用时会直接落到浮现模式，于是返回权重最高的桶 + 置顶核心准则。叠加 dream 末尾 plan 段可能因总预算降级成只报条数，**plan 的正文一度没有任何读取入口**——只能看到「3 条」这个数字，内容一条都读不到。
  - 新增 Plan 通道（`tools/breath/surface.py: surface_plans()`），与既有 Feel 通道同构：`domain="plan"` 直接拉所有 `type==plan && status==active` 桶，按 `created` 倒序逐字返回，不截断不摘要，已 resolved/abandoned 的不返回。**没有新增工具**，只补了一条 domain 分流。
  - 回归测试见 `tests/test_breath_plan_channel.py`，四条断言覆盖：正文可读、不返回核心准则、排除非 active、不调用 LLM。
- **dream 里「没有计划」和「plan 段被预算挤掉」长得一模一样。** 没有 active plan 时 plan 段整段静默消失，无法与降级区分。现在会明确输出「没有计划。」。
- **dream 的 feel 历史段预算从 6000 提到 15000**（`surfacing.feel_max_tokens`）。原值下 feel 很容易被折叠成 40 字摘录。⚠️ 该项若已在 `config.yaml` 中显式设置，以配置值为准，改默认值不生效。
- **Docker 部署下 `AI_NAME` 实际设不上。** `.env.example` 写明可以设，但 `deploy/docker-compose.yml` 的 environment 段从未透传它，而 `get_ai_name()` 只读环境变量——**后果是带锁 Letter 在 Docker 上完全无法创建**（报「未能从现有 ai_name / AI_NAME / author 中取得当前写信人的实际关系名」）。
  - `ai_name` 现在是 `config.yaml` 的一级配置项，**优先级高于环境变量**：config 随 vault 持久化，容器重建/重启都不丢。默认留空表示「没配」，回退环境变量、再回退 `"AI"`，与旧行为一致。
  - `get_ai_name()` 按 config 文件 mtime 缓存（它在一次 letter 请求里会被调用多次），改完配置立即生效、无需重启；配置缺失或损坏时静默回退而不抛异常——署名逻辑遍布 letter/prompt/Dashboard，不能因为配置坏掉就整条链路崩。
  - `deploy/docker-compose.yml` 一并补上 `AI_NAME` 透传，让 `.env` 里的设置也能生效。
- **`entrypoint.sh` 播种日志里的代码目录路径被 shell 吞掉。** 第 242 行 `播种代码到持久卷 $CODE_DIR：$RESEED_REASON` 中，`$CODE_DIR` 后面紧跟的全角「：」是多字节字符，shell 把它的首字节当成变量名的一部分，转而展开一个不存在的变量——**路径整个丢失**，并向 stdout 吐出半个字符（非法 UTF-8）。改用 `${CODE_DIR}` 明确边界。
  - 后果不止是日志难看：运维看不到代码播种到哪个目录；`subprocess(text=True)` 读取启动输出时直接抛 `UnicodeDecodeError`，`tests/test_entrypoint_code_bootstrap.py` 中 4 个用例因此长期失败。修复后这 4 项全部转绿。
- **镜像缺 `docs/adr/`、`tools/`、`kernel/`，三项系统诊断在任何 Docker 部署上恒红**（`adr_requirements` 报 docs/adr not found、`preflight_cli_diagnostics` 报 missing_files、`vnext_preflight` 的 `rust_kernel_scaffold` 契约失败）。
  - `.dockerignore` 放行这三处、`Dockerfile` 补 COPY。
  - `entrypoint.sh` 的播种清单同步扩展：新增可选目录 `SEED_DIRS_OPTIONAL="docs tools kernel"`。此前只播种 `src/`、`frontend/` 与三个 root 文件，而诊断读的是播种目标 `<vault>/_app`，因此光进镜像并不生效。可选目录一律「存在才处理」，老镜像缺失时不阻断启动；换代播种、失败回退与崩溃回滚（`_prev`）三条路径都同步覆盖，回滚后运行时树与回滚点保持一致，不残留新镜像带来的目录。
  - 修复后 `preflight_cli_diagnostics` 与 `vnext_preflight`（28 项全通过）转绿。
  - ⚠️ **`adr_requirements` 仍为 error，但原因已不同**：目录现在能读到了，暴露出 `docs/adr/ADR-0003-unified-human-archive-entry.md` 自身缺 6 个必需边界章节（`Why this is not cognition`、`Why this is not a database feature`、`How forgetting still works`、`How tombstones are preserved`、`How present thinking remains with the LLM`、`Rejected alternatives`）。ADR-0001/0002 均合格。该文档需由其作者补齐，本次未改动。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `3.0.0`。

## 2.17.11

### 改进 / Changed

- Relation MCP 可发现性补强：`relation_attach.relation_type` 直接以 schema enum 暴露六种固定类型与 `custom`，四个 Relation 工具的公开说明补齐 ID-first、方向语义、双向镜像、稳定 slot、detached/title 展开和 legacy 行为，避免调用方靠猜测参数。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.11`。

## 2.17.10

### 改进 / Changed

- Relation 改为 ID-first 的天然双向关系：新建关系在两个普通桶各写一个共享 `relation_id` 的镜像视图，固定六型自动使用 `caused_by↔causes`、`continuation_of↔continues` 及两个对称类型的反向语义；新增 `custom` 类型，仅 custom 使用 `label/reverse_label`，未给 reverse 时默认沿用正向 label。
- `expected_title` 从四个 Relation 工具的必填门槛降为可选校验；`relation_read` 默认只返回 active 的极简 ID ledger，可按需展开目标当前标题或 detached 历史。breath/dream/catalog 的 Relation hint 仍最多展示两条 active 关系，但现在会显式提示剩余条数。
- 新双向 Relation 的 detach/restore 会在有序双桶锁内同步两端镜像；旧 V1 无 `relation_id` 的单向关系不批量迁移，继续保持可读、可原位 detach/restore 的兼容行为。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.10`。

## 2.17.9

### 修复 / Fixed

- 修复 Claude `conversations.json` 被裸结构化记忆 JSON 预检误判的问题；带有
  `chat_messages`、`mapping` 或 `messages` 的会话信封现在交由既有对话格式识别，
  合法裸结构化记忆列表的直接导入兼容行为保持不变。

### 测试 / Tests

- 新增 Claude 官方 `chat_messages` 结构的预检回归测试，并保留裸结构化记忆列表测试。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.9`。

## 2.17.8

### 新增 / Added

- 新增 Relation V1：普通记忆桶之间可建立一跳有向关系，使用稳定 1-based slot 的 `relation_links` ledger，并提供 `relation_attach`、`relation_read`、`relation_detach`、`relation_restore` 四个公开工具；detach/restore 可逆且不压缩 slot。
- 内置六种机器关系 `caused_by` / `causes` / `continuation_of` / `continues` / `related_to` / `same_event`，默认显示为「原因 / 结果 / 前段 / 后续 / 相关 / 同一事件」；可选自定义 label 最多 20 字符，只改变展示语义，不新增 machine type。
- breath 与 catalog 在已选中的普通桶后追加最多两条极简 Relation hint；dream 仅在近期普通记忆块展示 Relation。Relation 不读取目标标题或正文，也不参与候选生成、排序、embedding、activation、decay 或递归图遍历。

### 兼容与安全 / Compatibility & Safety

- Relation V1 仅连接普通记忆桶；归档普通桶仍保留并可管理关系，归档后的 plan / feel / I / letter 等特殊桶仍保持拒绝边界。
- 备份导入的 keep_both 会重写包内 Relation 目标 ID；若包内目标未成功导入，则保留 stable slot 并原位 detached，避免误连到本地同 ID 旧桶。畸形 Relation metadata、非法 type、换行或超长 label 均 fail-closed。

### 测试 / Tests

- 扩展 Relation ledger、稳定 slot、方向性、自环、特殊桶、归档、active 上限、tiny manifest、breath/dream 渲染、公开 MCP schema 与 migration remap 回归；本地工程桥验证继续通过，Python/pytest 由 CI 执行权威验证。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.8`。

## 2.17.7

### 新增 / Added

- 新增可逆的 Source 证据绑定层：持久化有序 `source_links`，并继续维护既有 `source_refs` 作为 active 兼容投影；同一 Source blob 可安全复用到多个桶，detach/restore 只改变绑定状态，不复制或改写原始证据。
- 新增 `source_attach`、`source_detach`、`source_restore` 三个公开工具；slot 采用稳定的 1-based 位置，detach 后不压缩、不重排，restore 回到原 slot。
- `source_read` 支持 `source_slots` 与 `all_sources`。多 Source 默认只返回极简 manifest，显式选择后才读取正文，避免一次拉取把上下文预算读满。

### 兼容与安全 / Compatibility & Safety

- 旧桶仅有 `source_refs` 时继续可读，并可按原顺序解释为 active links；新建/追加时同步维护两套字段。active 绑定上限保持 32，总 ledger 上限 128，超限显式拒绝，不静默丢弃。
- Source attach/detach/restore 不改变桶正文、标签、domain、importance、生命周期、recency 或 embedding；归档桶可管理证据绑定，但不会借 Source restore 复活桶生命周期。锁定 Letter 继续拒绝 AI 修改。
- 备份/迁移闭包同时收集 active 与 detached Source 引用，且在 `source_refs` / `source_links` 并存时取并集，防止证据遗漏。

### 测试 / Tests

- 扩展 Source 层、公开工具 schema、MCP 集成与服务器工具列表回归，覆盖共享不可变 Source、legacy 投影、稳定 slot、detach/restore 幂等、容量预检、单 detached manifest、备份闭包与不触发派生索引等边界。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.7`。

## 2.17.6

### 修复 / Fixed

- 修复 `breath()` 无参浮现时的 pin 预算优先级：只要有任一核心准则因 token 预算不足无法整桶返回，本轮普通浮现、久未浮现与偶然想起全部跳过，避免普通记忆挤占核心准则预算。
- 核心准则继续保持“整桶返回或整桶省略”，不截断、不摘要；当所有 pin 均能装入预算时，普通浮现原有排序、采样与预算行为保持不变。
- 保持 breath 默认预算不变，仅将显式 `max_tokens` / `surfacing.breath_max_tokens` 的安全上限提高到 40000，为重度使用者提供 opt-in headroom。

### 测试 / Tests

- 新增回归覆盖 pin 被预算省略时普通记忆必须全部跳过，以及全部 pin 装入后普通记忆仍可正常返回；同时清理 `tests/test_dream_prompt_boundary.py` 中已知的 Ruff F841 无用变量。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.6`。

## 2.17.5

### 修复 / Fixed

- 修复 I 候选在 `dream` 结果中已经浮现、见证数却仍停在 `0/3`：此前只统计
  末尾专用候选段，候选若先在近期记忆段出现、而专用段被总 token 预算挤掉，
  就会漏计。现在统一追踪最终输出中实际渲染的候选，无论它出现在近期记忆、
  候选主块还是另一候选的碰撞材料，当天都记一次见证；完全未渲染仍不计次。
- 候选状态或见证落盘返回失败时不再误报成功；历史 `i_dream_dates` 会按日期
  去重后再判断 `promote` 的 3 次门槛，确保必须来自 3 个不同日期。

### 测试 / Tests

- 新增预算截断精确回归、碰撞材料见证、真实 Markdown 重载持久化、写入失败与
  重复日期门槛测试，并补 MCP 端到端 `I → dream → I(read)` 见证链路。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.5`。

## 2.17.4

### 修复 / Fixed

- 修正 2.17.3 引入的反向误导：`llm_step_failed_error()` 在 `api_available=True`
  分支里写了「key 配置正常」。但 `api_available` 只回答「配没配」，不回答「配得
  对不对」——key 填错、过期或余额耗尽时它仍是 True，调用会以 401/402 失败，这时
  那句话等于把原来的误导换了个方向。真机用无效 key 跑 `grow` 复现后改成并列列出
  可能原因（供应商故障、模型返回为空、key 失效或余额不足），把判断交回给日志。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.4`。

## 2.17.3

### 修复 / Fixed

- `grow` 的两条路径（长文 digest、短内容打标）此前把所有失败都报成
  「API key 未配置或调用失败，请检查 OMBRE_COMPRESS_API_KEY」。实际上这条路上
  绝大多数失败与 key 无关——供应商 5xx、超时，或 dehydrator 抛的「API 日记整理
  返回空结果」（模型返回解析后 0 条有效条目）都会撞上同一句话，把排查方向带偏：
  key 明明是好的，失败前一秒调用还是 200。现在按 `dehydrator.api_available`
  分岔，只有 API 确实没配好才提 `OMBRE_COMPRESS_API_KEY`，其余情况说明是调用
  失败或返回为空，并引导去看 `server.log` 里的 `err_type`。
- 工具层 9 处 `except Exception` 后直接把裸异常正文拼进返回值的位置，改走统一的
  `errors.safe_error_detail()`：正文照给（保留排查线索），但先抹掉
  `Bearer <token>`、`sk-` 开头的 key、`api_key=` / `token:` 这类键值对，并限长
  200 字符。涉及 `i`、`plan`、`breath`、`anchor`、`grow` 五组工具。捕获自家校验器
  `ValueError` 的那几处（`plan` 的 Letter 锁参数、`_common` 的 grow items 校验）
  维持原样，那些是精心写给调用方的提示，不该被脱敏改写。

### 变更 / Changed

- 导入侧的 `_safe_import_error_detail()` 实现上移到 `errors.safe_error_detail()`，
  原函数保留为薄封装以兼容既有调用与回归测试；脱敏正则只维护一份，避免两处漂移。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.3`。

## 2.17.2

### 修复 / Fixed

- 完整落实 Issue #85：`grow(content=...)` 的长文 digest 与短内容快速路径会在
  首次新建时保存合法的逐条 `why_remembered`；后续合并仍只补旧空值，绝不覆盖
  人工或历史理由，空值和非法模型输出也不会阻断正文入库。
- 按 Issue #89 的冷参考定位，为 `pulse()` 与 `breath_advanced(catalog=True)`
  中的 anchor 桶增加独立 `⚓ [anchor]` 显示标记；不新增读取工具，不改变默认
  `breath` / `dream` 的排除规则，也不改变显式检索、衰减或存储行为。
- 补充 Issue #84 的历史格式回归：直接验证 `letters/history/` 中的 v2.4.12
  Letter 在通用扫描、无参数 `letter_read()` 与 Dashboard Letter API 三个入口
  一致可见，并覆盖活跃缓存预热后的外部文件变更检测。
- 加固热更新清单生成：`VERSION`、`src/` 或 `frontend/` 仍有未暂存改动时
  直接拒绝生成，并让清单版本与文件哈希统一读取同一 Git index/HEAD 快照，
  防止再次产生 `src/VERSION` SHA-256 与源码归档不一致的发布包。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.2`。

## 2.17.1

### 修复 / Fixed

- `letter_lock_update` 成功路径原样返回 JSON，与其余 15 个 MCP 工具的中文短句风格不一致；
  `letter_write` 创建带锁 letter 时同样返回 JSON。两处改为统一的标签式中文文案，
  失败路径的 "Letter not found" 也一并中文化。
- `bucket_manager.set_anchor()` 的 `"bucket not found"`/`"update failed"` 是仅有的两处
  英文字面量，被原样拼进 `anchor`/`release` 的中文提示句里，改为中文。
- `grow` 一直没有 `test_data` 参数（`hold` 有），导致 `grow` 创建的桶无法被
  `trace(hard_delete=True)` 清理，无法用于可回收的自动化测试数据。补齐参数并透传到
  `merge_or_create`。
- `dream` 输出结尾追加固定收束语。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.17.1`。

## 2.16.9

### 修复 / Fixed

- 修复默认 stdio 传输未启动持久化向量队列，导致 `grow` 已完成 Markdown 原子写入后，
  仍同步等待外部 embedding provider；客户端可能先报“server isn't responding”，
  随后误判保存失败并重复写入的问题。
- stdio 现在与 HTTP 托管模式复用同一 `EmbeddingOutbox` 生命周期。默认后台索引开启时，
  写入请求在正文和索引期望状态持久化后即可回包，慢 provider 由队列继续处理；
  独立运行或显式关闭后台索引时仍保留原有同步索引兼容行为。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.9`。

## 2.16.8

### 修复 / Fixed

- 修复 Issue #84 中历史 Letter 异常进入归档区后，Dashboard 仍能看到记录、
  但 `letter_read` 无法读取的问题。新增登录后维护接口：GET 只读扫描，POST
  仅恢复明确提交的候选 ID；不会在启动或日常读取时自动扫描、恢复归档数据。
- 历史 Letter 恢复会在同一桶租约内重新确认唯一物理真源、强 Letter 标记及
  非删除终态，再原子改回 `letter` 并移入 `letters/history/`。正文、作者、
  原时间与锁字段保持不变；弱线索、墓碑、保护态和路径碰撞均拒绝处理。
- 修复 Issue #85 中 `grow(items=[...])` 无法保存 `why_remembered` 的问题。
  人工逐条理由会先做字符串、500 字符和批量元数据预算校验，首次新建即可保存；
  合并时只补旧空值，不清除也不覆盖已有理由。
- `grow(content=...)` 的长文 digest 与短内容专用打标都会生成候选理由，但首次
  新建不盲目写入；仅后续 grow 再次确认命中同一具体事件时原子补入旧空值。
  自动理由严格依据原文，且原文中的 system、ignore、tool 等文字只按数据处理。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.8`。

## 2.16.7

### 修复 / Fixed

- 修复 Issue #76 中 pinned 记忆归档后，显式恢复会静默重新占用 pinned 配额的
  问题。归档文件仍保留历史标记；恢复时会在同一原子提交中清除 pinned、刷新
  活跃时间并校验普通高重要度配额，不会产生幽灵钉选或中间状态。
- 完成 Issue #82 的 `trace(protected=1|0)` 能力。protected 现在使用独立配额
  （默认 20），与 pinned/anchor 互斥，锁定 importance=10；解除最后一层保护
  必须在同次调用重新指定 importance，配额检查与写盘保持并发原子性。
- protected 被明确为「防衰减但不主动浮现」：无参 breath、Dream 的全部候选与
  提示、SessionStart hook 以及 Dashboard 默认 Breath 均不会注入受保护记忆；
  显式 search/catalog 仍可读取，并统一显示受保护标记。
- 历史 `protected+anchor` 冲突档案不会恢复成非法活跃状态；可用
  `trace(id, restore=True, protected=0, importance=1..10)` 原子解除保护并恢复。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.7`。

## 2.16.6

### 修复 / Fixed

- 修复 PC 浏览器缩窄或放大到中间宽度时，Dashboard 页头仍强制保持单行，导致
  标题和统计信息逐字竖排、搜索与操作按钮重叠以及页面出现横向滚动的问题。
- 769–1500px 视口改用稳定的两行页头；导航在较窄 PC 下按 6+5 自适应排列，
  移动端按三列排列并将页头操作收为图标按钮，宽屏原布局保持不变。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.6`。

## 2.16.5

### 修复 / Fixed

- 修复 Issue #83 所述 breath 与 dream 返回值中安全元数据逐桶重复、挤占大量
  上下文预算的问题。Dream 与 SessionStart breath-hook 现在共用紧凑的 OBM2
  数据信封，普通 breath 也改用同一短标记；固定安全语义每次响应只声明一次。
- 紧凑协议继续为每块保留独立内容边界、字符数、完整 SHA-256、来源、展示角色、
  原文/截断状态和命令式文本风险标记；记忆正文仍逐字返回，块内伪造的 system、
  tool 或边界文本仍只能作为历史数据处理。
- 完整 SHA-256 改用等价的无填充 base64url 表达。270 字样例的单块信封开销
  在 Dream 中约减少 50%，在 breath-hook 中约减少四成，普通 breath 的逐桶
  标记约减少一半，同时继续计入原有 token 硬预算。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.5`。

## 2.16.4

### 修复 / Fixed

- 修复上锁 Letter 在 Dashboard「所有桶」、`letter`/归档筛选和通用详情中泄露
  标题或正文的问题。通用列表与详情现在复用 Letter 的 human 侧锁判定，只返回
  中性占位和安全锁元数据；搜索、关系图、重复检测及调试浮现也不会纳入当前用户
  无权读取的锁信。
- 导入复核列表对锁信使用同一安全占位，并禁止复核动作或通用编辑绕过活动锁；
  所有 Letter 均不能通过通用桶接口改变类型或钉选状态。
- 历史上已被改成 `permanent`/pinned、`plan`、`feel` 或 `i` 的 Letter 仍按
  `source_tool`/`__letter__` 识别，不会进入普通 breath、专用类型读取、检索、
  hook、dream、导入模式分析或 hold/grow 合并；
  非锁拥有者也不能借通用 trace 或原文证据入口读取、改写。通用编辑使用桶锁内的
  锁版本前置校验，避免检查后并发上锁仍覆盖正文。
- Letter 专用读取与锁管理同样识别历史类型迁移数据；锁拥有者、已解锁信件和已到期
  定时锁仍按原契约正常可读。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.4`。

## 2.16.3

### 修复 / Fixed

- 修复 Issue #82 中显式恢复归档记忆时保留旧 `last_active` 的问题。
  `trace(..., restore=True)` 现在会在同一次原子恢复中刷新活跃时间，
  避免低分桶在下一轮衰减中立即二次归档。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.3`。

## 2.16.2

### 新增 / Added

- Dashboard「热更新」面板新增「热更新遇到依赖变化时自动安装」开关，等效于设置
  `OMBRE_UPDATE_ALLOW_PIP=1`（写 `config.yaml` 的 `update.allow_pip_install`，
  立即生效不需要重启）。此前这个能力只存在于代码和环境变量里，只能 SSH 改
  `.env` 重启才能碰到；这次改动只是把已有开关暴露成能点的 UI，**默认值没有
  变**——安全加固 #2（自动 pip 会把"谁能点热更新"放大成任意 PyPI 包的执行面）
  仍然默认关闭，需要部署者自己清醒地打开。

### 修复 / Fixed

- 热更新遇到「依赖清单变化 + 自动 pip 关闭」时，检查提前到下载/解析完更新包
  之后、真正备份 `_prev` 和覆盖 `src/`、`frontend/` 之前。此前会先建回滚点、
  写完文件才发现装不了依赖，再整体回滚——多做一轮磁盘 I/O，报错文案也不准确
  （"已回滚"，其实这次没有任何文件被改动过）。现在直接在写文件前拒绝，报错
  也改成「未改动任何文件」，并在文案里指向新加的 Dashboard 开关。

### 测试 / Tests

- 新增 `test_update_settings_endpoint_persists_and_takes_effect_immediately`、
  `test_update_settings_endpoint_rejects_missing_field`。
- `test_changed_release_lock_with_pip_disabled_rolls_back_everything` 更名为
  `..._rejects_before_touching_disk`，补充断言确认 `_prev` 回滚点全程没有被
  创建过。

### 文档 / Docs

- `docs/ENVIRONMENT_VARIABLES.md`、`docs/OPERATIONS.md` 说明 `OMBRE_UPDATE_ALLOW_PIP`
  与新 Dashboard 开关是同一件事的两种配置方式，以及"提前拒绝"这个行为变化。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.2`。

## 2.16.1

### 修复 / Fixed

- 云服务器/远程部署首次打开 Dashboard 设置密码时，`/auth/setup` 一直是"仅本机可
  设置"（防止部署窗口期被人抢先设成他人的密码），但页面不会提前说明，用户填完
  密码点提交才被拒绝，报错还是英文原文，体验上跟 `rule.md` 第 6 条"安全复杂性
  应由系统承担，不能转嫁给用户"正好相反。
- 首次设置表单现在会在检测到当前不是从 `localhost`/`127.0.0.1` 访问时提前显示
  中文提示，说明要去服务器上设 `OMBRE_DASHBOARD_PASSWORD` 或 `OMBRE_SETUP_TOKEN`；
  即便这个前端启发式判断漏判，提交失败后的报错也会换成同样的中文说明（真正生效
  的判定仍在服务端 `web/auth.py` 的 `_setup_request_allowed`，前端只是提前预警）。
- 新增环境变量 `OMBRE_SETUP_TOKEN` 的文档（此前只存在于代码里，`docs/ENVIRONMENT_VARIABLES.md`
  和 README 都没提，普通用户无从得知这条远程补救路径）。

### 测试 / Tests

- 新增 `test_setup_form_warns_only_when_not_on_a_loopback_host`、
  `test_setup_failure_explains_the_local_only_restriction`。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.1`。

## 2.16.0

### 新增 / Added

- 转换历史 Letter（`convert_to_lockable`）在 `AI_NAME` 未配置、拿不到"实际关系名"
  时不再只是报错终止：Dashboard 弹窗当场填一个名字即可重试完成这一次转换，仅
  作用于这一次请求，不写入全局配置。后端 `/api/letter/{id}` PATCH 新增可选
  `ai_name` 字段，取值仍要经过 `_is_actual_relation_name` 校验——"ai" / "assistant"
  等通用占位依旧会被拒绝，这道准入门槛本身没有削弱。

### 测试 / Tests

- 新增 `test_historical_conversion_accepts_request_scoped_ai_name_override`：
  验证 `AI_NAME` 未配置时请求体传入实际关系名可以完成转换，且传通用占位名
  仍会被拒绝。
- 更新 `test_dashboard_offers_one_way_legacy_letter_conversion_to_ai_ownership`
  匹配新的请求体构造方式与弹窗文案。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.16.0`。

## 2.15.0

### 安全 / Security

- 修复 `cryptography==49.0.0` 的已知漏洞 PYSEC-2026-3552（无同版本修复，只能升级到
  `50.0.0`）。CI 的 `pip-audit` 检查自 2026-08-04 起持续报红：`.github/workflows/tests.yml`
  锁文件校验固定的包索引快照日期（`UV_EXCLUDE_NEWER`）早于 `cryptography 50.0.0`
  发布日（2026-07-31），锁文件永远重新解析回 `49.0.0`。快照推进到 `2026-08-01`，
  重新生成 `requirements.lock.txt` / `requirements-dev.lock.txt`。

### 破坏性变更 / Breaking

- 移除 legacy SSE MCP 传输（`transport: sse`，`/sse` `/messages` 路由）。现在只支持
  `stdio` 与 `streamable-http`；已废弃客户端请改连 `/mcp`。Dashboard「传输模式」
  与首次部署向导的 `sse` 选项一并移除。未识别的 `transport` 取值（含 `sse`）现在
  会在启动期显式报错退出，不会再落到 FastMCP 自带、不受本项目鉴权/CORS/CSRF/
  限流中间件保护的 `mcp.run(transport="sse")`。
- 上面的快照推进连带把 `mcp` 1.28.1→1.29.0、`openai` 2.45.0→2.52.0、`uvicorn`
  0.51.0→0.52.0 等包一起升级，`requirements.lock.txt` 内容随之改变。仍在运行
  v2.8.4 之前旧逻辑、且这次之前从未升级过的部署实例，热更新时可能无法再走旧的
  legacy 依赖回退路径，需要手动升级一次；这是经过评估后接受的破坏性变更，不再
  为其设计兼容迁移。

### 测试 / Tests

- `tests/test_server_app.py`：移除依赖 legacy SSE 官方客户端连接的回归，改为验证
  `build_http_app` 对 `"sse"` 显式抛错。
- `tests/test_secure_onboarding.py`：网络传输安全矩阵不再覆盖 `sse`；用 `stdio`
  替换测试里原本用 `sse` 占位的「已保存但未生效」示例值。
- `tests/test_update_source_gate.py`：`requirements.lock.txt` 基线哈希推进到新内容，
  说明这次是经评估后接受的破坏性变更。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.15.0`。

## 2.14.2

### 修复 / Fixed

- 热更新重启等待提示改为原地刷新省略号（1～3 个循环），不再每 2 秒追加一行「等待
  服务恢复…」。原逻辑在服务端 `os.execv` 自重启期间轮询 `/api/version`，最长 60 秒
  内会连续刷出 30 行日志、日志框不停向下滚动，服务其实已正常恢复，只是这段等待
  体验容易让人误以为卡死。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.14.2`。

## 2.14.0

### 新增 / Added

- Letter 支持 `none`、`timed`、`permanent` 三种锁状态；新增只修改锁元数据的
  `letter_lock_update`，锁所有权只由 Dashboard/MCP/stdio 等可信入口确定，署名不参与权限。
- Dashboard 可创建、查看和管理自己的锁信；对方尚未开放的信只显示实际关系名、时间与锁状态，标题和正文不返回。
- SessionStart 按 hook Token、Dashboard session 或公开未认证入口采用对应可见性；锁提示只使用已有实际关系名。
- Letter 语义检索在向量反序列化和相似度排序前排除当前不可见候选，避免查询命中本身泄露内容。

### 兼容与边界 / Compatibility & Boundary

- 历史 Letter 缺少锁字段时按无锁处理，不迁移数据；旧调用不传锁参数时行为不变。
- 不新增必填环境变量，不修改 OAuth、Token、Docker Compose 或 multi-owner 配置。
- 时间锁是应用层关系边界，不是磁盘加密；拥有 vault 或宿主机文件权限的人仍能读取 Markdown 原文。
- 保留 Dashboard 原有 Letter 原稿编辑：历史/无锁 Letter 和锁拥有者自己的锁信可编辑；原稿编辑与锁管理必须分开请求，来信方未解锁内容不可读写，编辑后正常刷新搜索索引。
- 历史公开 Letter 可在 Dashboard 按需转换为新版格式；转换不改原稿、不根据 `author` 推断身份，锁控制权固定交给当前 AI，并补写现有配置中的实际 AI 关系名。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.14.0`。

## 2.13.1

### 修复 / Fixed

- 修复热更新因完整性清单不符而整包中止：`update_manifest.json` 首次入库时是在
  Windows 工作区生成的，记录的是 CRLF 字节，而 GitHub 源码归档携带的是仓库里的
  内容，206 个文件里有 170 个大小/哈希对不上，更新在 `frontend/onboarding.html`
  处中止（清单记 10819 字节，归档里是 10621）。
- `deploy/gen_update_manifest.py` 改为直接读 Git 仓库存储字节（优先 index，回退
  HEAD）生成清单，不再读工作区，也不用「文本一律归一为 LF」的启发式——本仓库
  index 里有 9 个文件存的就是 CRLF、30 个是混合行尾，归一化会把这 39 个反向算错。
  文件不在 index 也不在 HEAD 时直接报错，不再拿工作区字节顶替。
- 发布顺序固定为：先 `git add` 代码改动，再生成清单——清单描述的是仓库内容，
  不是磁盘内容。
- 修复 STDIO transport 成功启动后 `.boot_fails` 未重置：HTTP/SSE 通过
  `RuntimeLifecycle` 在成功启动后清零，STDIO 原先直接调用 `mcp.run()` 缺少对应
  lifecycle；现改为在 FastMCP public lifespan 成功进入时复用现有 boot marker reset 语义。

### 测试 / Tests

- 新增 `tests/test_update_manifest_repo_bytes.py`：用临时 git 仓库复现
  「index 存 LF / 工作区 CRLF」与「index 存 CRLF」两种会被算错的形态，
  并校验仓库现有清单与 HEAD 字节逐条一致。
- 新增真实 STDIO MCP 子进程回归：完成 `initialize` 与 `tools/list`（15 个工具）后，
  验证 `.boot_fails` 从 1 重置为 0。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.13.1`。

## 2.13.0

### 新增 / Added

- `I` 改成沉淀机制：写下的「我觉得……」不再直接成为自我认知，而是先落成一条普通 `dynamic` 记忆（候选），跟别的记忆一样浮现、衰减、进 dream；站不住的自然沉下去。
- `dream` 新增待沉淀候选段：列出每条候选、已被几次 dream 见证，并附本次语义上撞上的材料——已认下的自我认知、普通记忆、另一个还没沉淀的念头都可能出现。碰撞只摆材料，不判断谁支持谁、谁与谁矛盾。向量索引不可用时照样列候选，并明说这次没有材料对照。
- `I(promote="桶ID")` 升级候选为正式条目，门槛是被 **3 个不同日期**的 dream 见证过；不够时明确回报还差几次。可同时传 `content` 用提炼后的措辞落成正式条目。
- `I(read=True)` 同时列出待沉淀候选，并把早期直接写入的历史条目标注为「未经沉淀」。

### 边界 / Boundaries

- 见证只认真的被渲染进 dream 输出的候选；因 token 预算未展开的候选不计次数。同一天做多次 dream 只算一次见证。
- 升级不删除候选桶（rule.md 第 1 条），候选保留原文并标记 `i_stage: promoted` 与指向正式条目的 `i_promoted_to`。
- 候选带 `__i_candidate__` 标签而非 `__i__`，不会被 SessionStart 注入或 Dashboard `/api/self` 当成已成立的自我认知。
- 待沉淀候选不能被 `hold` / `grow` 当作合并目标（与 pinned / protected 同一道准入）：它是「我对我自己的一个判断」而不是时间里发生的事，正文被追加改写会让「几轮梦之后它还站得住吗」失去判断对象。候选升级或退出候选状态后，合并路径恢复正常。
- 不提供绕过候选阶段直写正式 `I` 的通道；哲学边界见 rule.md 第 13.1 条。

### 测试 / Tests

- 新增 `tests/test_i_sediment.py`：候选形态、见证计数按天去重、未渲染不计次、门槛拒绝与升级后候选留存、碰撞材料呈现、向量不可用降级、早期条目标注。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.13.0`。

## 2.12.1

### 修复 / Fixed

- 解钉现在必须在同一次 LLM `trace` 或 Dashboard 请求中明确给出 `importance=1..10`，并原子落盘为动态桶；不再让解除核心后的记忆沿用 999 分短路或依赖第二次补写。
- 新记忆统一声明 `source_tool`，内部 append-only Footprint 记录创建来源以及钉选/解钉操作者（用户、LLM 或系统）；足迹供召回时的 LLM 判断，不新增 Dashboard 编辑入口，也不替代桶与 ledger 真源。
- 标准记忆 JSON（`name/content/domain/valence/arousal/tags/importance`）改为确定性直导，不再先发 LLM 请求；预检明确显示 0 次 API 调用，逐项校验失败会指出条目与原因，无 LLM 配置时也可导入。

### 测试 / Tests

- 增加 Footprint 来源与操作者、LLM/Dashboard 解钉原子 importance、结构化 JSON 离线直导、无 LLM 预检及逐项错误反馈回归。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.12.1`。

## 2.12.0

### 新增 / Added

- 新增开发侧离线 `HN-F1` 候选工具：以严格枚举元数据执行 PAS10 计数档位归一化，并用纯整数 Pareto-DP 对 PAS12 的 `P0/M1/Rtech` 粗分区给出可复核的精确可行、精确不可行或资源不确定结果。
- 新增候选机器合同、输入成员集合绑定、实现与 schema 哈希绑定、独立 witness 复核，以及 `aggregate-last.v1` 两文件逻辑提交协议。aggregate 仅在最后发布；缺少 aggregate 或两文件结构、共同字段、场景投影、私有运行记录哈希不一致时，不得把结果视为已提交 receipt。

### 安全与边界 / Security & Boundaries

- 除读取自身实现两次以核对运行前后完整性外，工具的业务输入只来自由外部管理员事前建立并证明 ACL／单写者边界的受限目录中的无正文枚举 JSON；它不读取 OB 配置、环境变量、真实 vault、模型输出或网络资源。代码内的路径与文件模式检查只是事故防护，不证明现实 ACL、owner、不可变性或跨文件事务。源码位于 Docker 构建上下文排除的 `tools/`，不新增 MCP、Dashboard 或线上运行入口。
- 当前实现只是未冻结的 PAS10 候选归一化器与 PAS12 候选数学核，不计算 PAS01–PAS14 治理状态，不执行 donor 联系、真实数据实验、PAS13 公开投影或 PAS14 授权。正式 PAS 哈希与批准状态不能由候选输出替代。
- `max_seconds` 是归一化与 LB/UB 共用的协作式单调时钟预算；检查到超时只返回资源错误与数学不确定。它不是硬 wall-clock 隔离，正式运行仍需外部 watchdog、内存/CPU 限制与冻结的恢复规则。

### 测试 / Tests

- 增加计数档位、nonresponse、资格/处置、frame/implementation/schema 绑定、严格 JSON、输出 pair 一致性、CLI 泄漏与受限路径回归。
- 增加独立 `4^N` 小规格穷举对照、固定 44 人边界、M1 cap、零目标、Pareto 剪枝、早停、witness 篡改与资源上限 fail-closed 回归。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.12.0`。

## 2.11.1

### 修复 / Fixed

- 修复云部署明确设置 `mcp_require_auth: false` 后，启动期网络门禁仍在内存中强制开启鉴权，导致 `/mcp` 返回 401、CC 云 session 报 `MCP error 32003` 的问题。网络边界风险仍会进入诊断与告警，但不再覆盖用户明确选择的 MCP 鉴权开关。
- Dashboard 将只读综合计算值由“权重分 / Weight”改称“活跃度分 / Activity score”，明确它由 importance、时间、激活次数、唤醒度和解决状态共同计算；`importance` 仍可编辑，综合分不新增人工或模型覆盖入口。

### 测试 / Tests

- 增加非回环云环境下免鉴权配置保持生效的回归，覆盖运行配置、MCP 中间件和 OAuth 路由使用同一关闭鉴权快照。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.11.1`。

## 2.11.0

### 新增 / Added

- 新增独立的 `bucket_edges` SQLite 表定义，只包含来源桶、目标桶、创建时间、最近激活时间、
  激活次数和派生权重六个字段；不接入采集、衰减、剪枝、检索或关系分类。

### 测试 / Tests

- 增加边表字段白名单、幂等初始化、非破坏性和基础约束回归。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.11.0`。

## 2.10.2

### 修复 / Fixed

- 修复 nginx 等反向代理返回 HTML、空响应或网关错误时，Dashboard 登录页把所有非 JSON 响应误报为“密码错误”的问题。登录页现在保留 OB 返回的明确错误，并分别提示来源校验失败、代理响应异常和网络不可达。
- 密码验证成功后先通过 `/auth/status` 确认浏览器已保存并回传 `ombre_session`，确认成立后才初始化 Dashboard；若 `Secure`、Host、协议或 `Set-Cookie` 转发导致会话未建立，会停留在登录页并给出对应检查项。
- 登录请求显式使用同源凭据与禁缓存语义；不会新增 localhost 免密，也不会自动信任 Docker 私网或放宽 `OMBRE_TRUSTED_PROXY_CIDRS`。

### 测试 / Tests

- 增加 Docker 网关来源、可信转发头与环境变量密码的完整登录成功回归，并覆盖非 JSON nginx 错误不再伪装成密码错误、成功响应缺少有效 Cookie 会话时不进入 Dashboard。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.10.2`。

## 2.10.1

### 修复 / Fixed

- 修复 v2.10.0 原文证据未进入本地完整备份与 GitHub 备份的问题。新备份会携带并校验 `sources/src_<sha256>.source`，GitHub 同步使用 `_sources/src_<sha256>.source`；导出前交叉检查全部引用，恢复时先验证/安装全部证据再写桶。旧备份仍可导入，但若桶引用的原文不在包内会明确警告，不再显示成无损恢复；Dashboard 会分别显示恢复的记忆数与安装的证据数。
- 修复 `source_read(scope="event")` 遇到空 `source_ranges` 时可能退化为整份原文的问题。事件模式现在必须有有效整数行范围，布尔值、小数与数字字符串不会再被误转成行号；确需整份共享原文时必须显式使用 `scope="full_source"`，越界范围会拒绝而不是静默裁剪。
- 修复长原文核对时先拼接全部证据、可能放大内存占用的问题。分页读取只保留当前窗口，输出头也计入 `max_tokens`，过长内容继续通过 `next_cursor` 分页，不静默超预算。
- 加固原文证据的路径、符号链接、大小、UTF-8、内容哈希与并发发布校验；在不支持硬链接的 NAS/SMB/FUSE 文件系统上使用跨进程 sidecar 锁，避免不可变证据被并发覆盖。
- 修复人工元数据仍可能被模型结果覆盖的问题。`hold` 与结构化 `grow` 现在让显式标题、标签、domain、importance 和情感坐标优先；未填写时才采用模型建议。结构化条目的未知字段、错误类型和越界值会在写入前明确拒绝。
- 修复 Dashboard 只能改显示名、不能原子更新显式标题的问题。标题改名会在同一次桶写入中同步兼容显示名，并统一执行单行、120 字符上限且不静默截断。
- 修复 Docker 构建中 `cloudflared` 下载失败可能被后续清理命令覆盖为成功退出码的问题；启用 Tunnel 组件时现在下载或授权失败会让构建明确失败，不再产出缺少二进制的假成功镜像。

### 安全与兼容性 / Security & Compatibility

- 精确桶 ID + 精确标题只是一次显式读取意图门禁，不是身份认证。远程可达的公网或局域网部署必须使用 OAuth/Token；stdio 与经安全门禁确认的本机回环模式继续遵循既有部署边界。`source_read` 会把原文标记为不可信存储数据，不调用模型、搜索或联想其他桶。
- 原文证据从本版起随完整备份迁移；v2.10.0 已生成的旧备份可能不含证据文件，只能恢复事件正文和元数据。证据层仍不参与普通 Markdown 桶扫描、日常浮现或语义索引。
- GitHub 备份与本地导出 ZIP 中的原文都是可读明文而非端到端加密内容；GitHub 应使用可信私有仓库并审计协作者权限，本地 ZIP 应加密保管或放入可信存储。
- 新增原文证据边界 ADR，并补齐备份恢复、并发写入、恶意路径/控制字符、分页预算与人工元数据优先级回归。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.10.1`。

## 2.10.0

### 给合作者的版本说明（人话）

这次主要解决两个体验问题：

- **记忆标题可以由整理者拍板。** 以前即使正文第一行已经写了《wife》，打标模型仍可能概括成“直接确认关系”。现在 `hold` 和结构化 `grow` 都能直接传最终标题；人工给出的标题、标签和重要度优先，模型只补没填的部分。提示词也会优先保留原文里的《标题》、独立首行和关键原话，但没有提高模型温度，避免打标变得飘忽。
- **增加一层平时隐藏、需要时才精确读取的原文。** 日常检索仍返回整理后的事件记忆。只有同时给出记忆桶 ID 和完全一致的标题时，`source_read` 才读取该桶对应的原文片段；它不做语义联想、不带出其他桶、不再次摘要。原文过长时明确分页，不静默截断。

设计上，一条记忆分成三层：

- **事件层**：日常 breath 使用的整理后正文，兼顾信息密度与可读性。
- **元数据层**：标题、中文短标签、importance 和情感坐标，允许整理者最终拍板。
- **证据层**：按内容哈希保存的不可变原文，默认不浮现，也不进入当前 Markdown GitHub 同步；只有精确核对时读取。

这样既不会因为保存全部原文拖垮日常上下文，也不会因为反复压缩而彻底失去当时真正说过的话。

### 新增 / Added

- 新增隐藏的不可变原文证据层：`grow(content=原文, items=[...])` 将共享原文按 SHA-256 内容寻址保存一次，每个事件桶以 `source_ranges` 指向自己的行范围。原文默认不参与普通浮现、Markdown GitHub 同步或当前 Markdown 备份导出。
- 新增第 15 个 MCP 工具 `source_read`。它必须同时精确命中桶 ID 和显式标题，一次只读取一个桶，不做语义搜索、相关桶扩散或模型处理；长原文通过 `next_cursor` 明示分页。
- `hold` 新增显式 `title`；`grow(items)` 对象条目支持标题、标签、importance、domain、valence、arousal 与 source_ranges。显式元数据始终优先于打标模型。

### 改进 / Changed

- 脱水与打标提示词优先保留《标题》、独立首行标题和有辨识度的关键原话，避免“确认关系”“达成共识”等会议纪要式标题；模型温度保持不变。
- 原文证据使用 `.source` 文件、写入时原子去重、读取时校验内容哈希；桶合并会追加来源引用而非覆盖已有证据。

### 版本 / Version

- 根目录 `VERSION` 与 `src/VERSION` 同步更新为 `2.10.0`。

## 2.9.0

### 修复 / Fixed

- 修复旧 Docker 实例仅靠热更新跨版本升级、当前持久代码目录与镜像基线都缺少 `requirements.lock.txt` 时，被误判为“依赖清单变化”并回滚的问题。只有“旧锁完全缺失”才会用离线、无安装的 pip dry-run 核对当前解释器是否已经满足新锁；已有旧锁但内容不同时仍保持严格拒绝。
- 依赖自愈探测只接受固定版本与 SHA-256 hash，禁用索引、网络、依赖解析和缓存；URL、editable、全局 pip 选项、宽松版本、非法 UTF-8、超时或子进程失败均失败关闭，成功后同步正式锁文件供后续更新比较。
- 修复长对话或纯文本作为单个超长 turn 时没有真正分块、随后在提取层静默截断尾部的问题。现在按 token 预算无损拆分，防御层遇到异常超限会显式报错而不会丢正文。
- 修复历史导入按语义相似度合并旧桶，造成生成数量偏少、旧日期沿用和导入结果不可见的问题。对话导入现在只做全局精确内容幂等去重并新建独立桶，不再修改相似旧记忆。
- 修复“已导入记忆”接口混入普通最新桶且固定只显示前 30 条的问题；接口仅返回导入桶并支持分页，Dashboard 可加载更多，导入完成或切回桶列表时会主动刷新。
- 修复 Dashboard 顶栏响应式换行后，系统状态条仍使用固定偏移而遮住折叠入口和设置内容的问题；现在跟随顶栏实际高度动态定位。
- 修复大量短消息因逐行 token 向下取整而生成超预算导入分块、随后被提取层整块拒绝的问题；任务状态会区分完成、部分完成与失败，早期失败也不再显示上一次任务结果。
- 导入精确去重由“每条记忆重新扫描全库”改为“每个任务一次建立正文摘要集合并增量维护”，避免大库长对话产生 `O(导入条数 × 桶数)` 的重复磁盘解析。
- “已导入记忆”加载更多改为只追加新卡片，不再反复销毁并重建已经显示的全部 DOM 节点。
- Dashboard 热配置在模型运行时重建或 `config.yaml` 持久化失败时会恢复配置、压缩客户端和向量引擎的同一份快照，避免接口报错后出现“界面配置、内存配置、实际组件”部分生效的分裂状态。
- 修复超深 JSON、平台无法表示的极端导出时间戳可使导入预检返回 500 的问题；异常输入现在安全降级或返回不含内部细节的 400。LLM 提取结果同时强制最多接收 5 条有效记忆，避免异常模型输出造成批量写入放大。

### 新增 / Added

- 新增 `hybrid` MCP 鉴权模式：OAuth 动态注册、授权码与访问 Token 流程继续可用，同时 `Authorization: Bearer` 也接受预置静态 Token；`Ombre-MCP-Token` 仍只接受静态 Token。Claude.ai 与只支持固定 Bearer 的客户端可安全共存。
- Dashboard、YAML/env 配置、部署向导、系统诊断、Tunnel 提示和官方 Compose 模板同步支持 OAuth / Token / 共存三态。多 owner Compose 为每个实例使用独立静态 Token，避免跨实例凭据复用。
- 对话导入桶持久化 `imported: true` 与 `source_tool: import`，Dashboard 列表、详情和复核卡片显示“被导入”；`created` 与 `last_active` 使用同一个实际导入时刻，不再采用历史对话时间。

### 安全 / Security

- 共存模式鉴权失败仍返回 OAuth resource metadata，静态 Token 比较使用固定长度摘要与恒定时间比较；纯 OAuth 不会意外接受残留静态密钥，OAuth access token 也不能通过静态自定义头。
- 多实例部署不再把同一静态万能 Token 注入所有 owner；示例改用 `OMBRE_MING_MCP_TOKEN` / `OMBRE_HONG_MCP_TOKEN` 独立密钥。
- `grow` 的外部模型调用失败只返回程序内置的安全提示；供应商异常正文不会进入 MCP 响应、持久错误或日志，避免密钥、URL 与请求内容泄露。
- Dashboard 登录、远程初始化和 Hook Token 统一使用 UTF-8 摘要做恒定时间比较；非 ASCII 错误凭据现在正常拒绝，不再触发 `compare_digest` 类型异常和 500。OAuth 授权日志字段会移除控制字符，避免换行伪造审计记录。
- 同内容与合并写入的跨进程锁改为内核文件租约，不再按 `mtime` 删除“过期”锁，避免两次慢模型调用超过阈值时第二进程抢占仍存活的临界区。
- Dashboard 称呼拒绝控制字符，导入提取不再把称呼拼进 system prompt，而是放入明确标记为不可信的 JSON 数据记录。Hook Token 示例同步删除不安全且实现未支持的 URL 查询参数方式。

### 测试 / Tests

- 增加热更新无旧锁迁移、恶意依赖语法、OAuth/静态 Token 共存矩阵、多实例密钥隔离、超长单轮和大量短轮次的预算内无损重组、导入只新建、导入日期/来源标记、失败终态、结果筛选分页及 Dashboard 刷新的回归测试。
- 增加活锁不可抢占、异常 JSON/时间戳、模型输出条数上限、称呼提示词边界和 Hook 凭据文档契约的红蓝对抗回归。
- 完成全项目活代码盘点、静态质量与安全扫描；移除已被事务式会话持久化路径完全替代、且没有生产调用方的旧会话保存包装函数和对应测试残留。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.9.0`，Dashboard、运行时与热更新检查显示一致。

## 2.8.12

### 安全 / Security

- 修复本机模式在裸机默认监听 `0.0.0.0` 时仍关闭 MCP 鉴权、导致局域网设备可匿名调用全部记忆工具的问题。本机模式现在默认保留鉴权；第三方客户端与 NAS/局域网部署推荐使用静态 Token。
- 新增统一网络边界门禁：网络 MCP 免鉴权只允许明确的 IPv4/IPv6 回环地址。旧配置处于非回环或 Docker 宿主绑定未知状态时，启动过程只在内存中强制开启鉴权，不改写配置、不阻止 Dashboard；部署向导与 Dashboard 保存接口同步拒绝危险组合。
- 内置 Cloudflare Tunnel 默认阻止把免鉴权 MCP 暴露公网。已有独立可信鉴权边界的高级部署可用精确值 `OMBRE_ALLOW_INSECURE_MCP=true` 显式承担风险并放行，其他宽松真值不会生效。

### 兼容 / Compatibility

- 三份官方 Compose 模板把宿主端口绑定 `OMBRE_BIND_ADDRESS` 传入容器，默认 `127.0.0.1` 免鉴权部署可被安全识别。仅热更新代码的旧 Docker 实例因无法证明宿主边界会安全回退为鉴权；更新 Compose 并重建容器后恢复明确边界，记忆数据不受影响。
- Dashboard、首次部署向导和系统诊断现在显示门禁、容器宿主边界及高风险豁免状态，避免把“可信局域网”误解为可匿名读写。

### 测试 / Tests

- 增加回环地址、通配/局域网地址、Docker 已知与未知宿主边界、严格逃生阀、启动期收紧、配置保存及 Tunnel 暴露的安全矩阵回归。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.12`。

## 2.8.11

### 修复 / Fixed

- `/auth/status` 响应明确禁止缓存，Dashboard 与安全部署向导请求该状态时也主动绕过 HTTP 缓存，避免浏览器或中间缓存复用过期的首次设置与登录状态。
- Dashboard 登录失败时按现有 `/auth/login` 响应契约读取 `error` 字段并显示具体提示，避免将限流、服务繁忙等错误统一显示为“密码错误”。

### 测试 / Tests

- 补充服务端响应头、Dashboard 与安全部署向导请求缓存策略的回归测试，以及登录失败错误展示的用户可见 DOM 回归测试。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.11`。

## 2.8.10

### 修复 / Fixed

- 系统诊断不再因 Dashboard 查询而在真实记忆库重建 SQLite 投影或创建 vNext WAL；重扫移出事件循环，向量投影改为逐行校验，避免 512 MiB 实例同时保留全库向量 JSON。
- embedding 查询缓存改为服务商实际可见前缀的固定长度摘要，不再保留超长记忆原文；向量库替换成功但配置发布失败时明确进入 `publish_failed`，不再误报完成。
- Dashboard 配置和采样参数统一拒绝越界值、非整数小数、布尔值及 `NaN/Inf`；采样设置与 MCP 静态 Token 只在持久化成功后发布到运行态，并串行提交落盘与运行态更新，避免并发请求造成状态漂移。
- 14 个公开 MCP 工具统一拒绝未知参数，避免拼错字段被静默忽略后仍产生写入。

### 安全 / Security

- MCP 操作日志不再复制查询、标题等私密文本，异常正文与 traceback 不再进入响应、持久错误或日志，失败响应也不再附带其他调用的全局日志；breath、信件与 `I` 读取的存储原文增加内容绑定的数据边界。
- OAuth 动态注册增加未授权客户端独立配额和回调地址总长度限制；媒体路径读取拒绝符号链接、特殊文件、并发路径替换及超限内容。

### 测试 / Tests

- 扩充 14 工具 Docker 集成、数值与 payload 边界、持久化失败、并发冲突、隐私日志、路径竞态及 prompt 注入数据边界回归。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.10`，Dashboard、运行时与热更新检查显示一致。

## 2.8.9

### 修复 / Fixed

- 固定 CI 依赖锁解析所使用的包索引时间点；即使从空输出重建也会得到现有生产锁与开发锁，避免上游索引发布新版本后在没有依赖输入变更时误报漂移，同时保持旧实例热更新认可的生产锁摘要不变。
- 补齐记忆详情对 `meaning` 字段的展示：新桶保存的多条体验锚点现在会在 Dashboard 标题下按原顺序显示为暖金引用块，不再出现“数据已写入但 UI 完全看不到”的情况。
- 兼容早期手写或旧备份中以单个字符串保存的 `meaning`；空值不生成占位块，长文本保留换行并安全折行。
- 所有 meaning 文本在进入详情 DOM 前统一转义，恶意 HTML 只会作为普通文字显示；列表接口仍保持精简，不额外传输最多 50 条的完整 meaning 数据。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.9`，Dashboard、运行时与热更新检查显示一致。

## 2.8.8

### 修复 / Fixed

- 修复热更新器把 `requirements.txt` 文本变化误判成实际依赖变化的问题；2.8.8 起依赖门禁以正式 `requirements.lock.txt` 为准，仅兼容旧更新包时回退源清单。官方 GitHub 热更新归档不再携带宽松源清单，因此 2.8.4 存量实例可直接点击热更新进入新版逻辑，无需重建镜像或临时放行 pip。
- Git 克隆、CI 与常规源码构建仍保留 `requirements.txt`；GitHub Download ZIP 与 Dashboard 热更新归档只携带带 hash 的发布锁。Dockerfile 同步兼容“归档中仅有 lock”的构建方式，避免一键直升修复影响 ZIP 部署用户。
- 依赖清单比较会规范化 CRLF/LF；Docker 持久代码目录缺少根级清单时会回退镜像内置基线。更新成功、镜像重新播种与崩溃回滚均同步两份清单，失败时连同“原本不存在”的状态一起精确还原。
- 发布锁真实变化时仍默认拒绝自动安装；明确开启 `OMBRE_UPDATE_ALLOW_PIP` 后改用 `--require-hashes` 安装锁文件。新代码先通过编译自检才会执行 pip，避免失败代码污染解释器环境，不降低原有供应链安全边界。
- 一键直升兼容规则绑定 2.8.4 的发布锁摘要；兼容规则存续期间若依赖锁发生变化，回归测试会强制失败并要求先提供显式迁移方案。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.8`，Dashboard、运行时与热更新检查显示一致。

## 2.8.7

### 修复 / Fixed

- 修复 `hold` 新建桶时，文件系统不支持锁、资源耗尽或 I/O 异常被一律误判为“锁正在占用”，最终等待 30 秒后才抛 `TimeoutError` 的问题；现在只重试真正的共享/锁冲突，其他系统错误立即保留原始 `errno` 报出，并附带逻辑 key、锁路径与持有者摘要用于诊断。
- 缩短 `hold/grow/trace/历史导入` 合并与编辑路径的租约：Markdown 原子提交后先释放桶锁，再登记 content/meaning 派生状态；同步兼容模式调用外部 embedding 前也已释放 identical-content、merge-target 与 importance/pinned 配额锁，慢 provider 不再阻塞后续写入。
- 同桶派生索引按跨进程顺序执行，并在 provider 返回后回读最新 Markdown；并发更新或较新请求被取消时，会继续把 content/meaning 收敛到最新值，避免迟到的旧向量覆盖新状态及重复生成同一最终向量。软删除并发发生时会在独立派生租约内复核是否已恢复，再清理迟到记录。
- embedding outbox 升级为 content/meaning 分组件调度、退避与哈希 CAS；单个 meaning 长期失败不再饿死正文向量。多个实例共享同一 vault 时，整份队列更新会在稳定 sidecar OS lease 内执行 `reload → merge/CAS → fsync → replace`，避免互相覆盖或旧 worker 复活已完成任务。
- 补齐历史导入合并、`trace(old_str/new_str)` 同时更新 meaning、恢复归档和全库人名替换等私有更新路径的锁外索引刷新，避免正文已更新但向量仍停留在旧内容。
- 文件租约初始化、上下文异常、任务取消及显式解锁失败路径统一保证关闭 descriptor；锁文件残留本身不会再被误诊为仍有活动内核租约。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.7`，Dashboard、运行时与热更新检查显示一致。

## 2.8.6

### 修复 / Fixed

- 修复首次完成设置、密码登录或安全问题恢复后，Dashboard 仍停留在空白/未初始化状态、必须手动刷新页面才显示数据的问题；三条认证成功路径现在统一进入同一个完整初始化流程。
- 受保护的数据请求、心跳与错误轮询延后到认证成功后启动，退出登录时同步停止；并用单飞与认证代次校验处理重复初始化、退出后快速重登和旧 `/auth/status` 响应回写等竞态，避免重复定时器或旧会话覆盖新界面。
- 当前活动页签（包括 `#letters` 旧书签）会在认证完成后主动刷新，个人条目入口也随本次会话重新加载，无需整页重载。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.6`，Dashboard、运行时与热更新检查显示一致。

## 2.8.5

### 修复 / Fixed

- 增强 Streamable HTTP 对无法正确处理有状态会话或 SSE 响应的客户端的兼容性，覆盖反馈环境中 Kelivo “能读到 `Ombre Brain` 服务名、但显示 0 工具”的表象：`/mcp` 现在使用无状态、直接 JSON 响应，初始化后无需保存/回传 `Mcp-Session-Id` 也能稳定列出全部 14 个工具。
- 删除 14 工具“主/副 FastMCP 实例 + 启动时操作私有注册表合并”的历史机制；全部工具直接注册到唯一公开实例，避免导入式 ASGI 启动或 SDK 私有结构变化时静默退化为 7 工具。
- CORS 响应显式暴露 MCP/OAuth 排障头，legacy SSE 启动日志改为输出真实 `/sse` 地址，避免客户端被误导到 `/mcp`。
- `/mcp` 对省略 `Accept` 或仅发送通配媒体类型的简化客户端自动选择 JSON；显式只接受 SSE 时仍返回协议错误，避免发送客户端无法解析的响应。
- MCP SDK 声明收紧为 `mcp>=1.27,<2`（生产锁定仍为 1.28.1），防止未来 v2 破坏性变更被非锁定安装静默带入。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.5`。

## 2.8.4

### 修复 / Fixed

- 修复 `digested=1` 只改变字段却仍会出现在无参 `breath()` 的问题：spontaneous/dream 策略现在硬过滤已消化桶，不再受 importance、pinned 或 3% 偶遇影响；带 query 的真实命中、importance 审计和 catalog 目录仍可显式找回。查询结果不足时追加的“非检索命中”随机漂浮也改用 spontaneous 策略，不再旁带 digested、dont_surface 或 anchor 桶。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.4`，Dashboard、运行时与热更新检查显示一致。

## 2.8.3

### 修复 / Fixed

- 修复"我 / Self"面板（dashboard.html 左下角 `I` 按钮）在窄屏设备上宽度固定溢出屏幕、显示不全的问题；连带把详情侧滑面板与自我面板的宽度都改成 `min(定宽, calc(100vw - 边距))` 自适应公式，替换掉原来"基础规则写死宽度 + 单独一条 `@media` 补丁"的模式，避免同类遗漏再次出现。
- 修复"我 / Self"面板关闭按钮未贴靠面板右边缘的问题。
- 修复手机端「写一封信」日期选择器点不开：原实现把真实 `<input type="date">` 藏成 1px 透明元素，靠 JS 调 `showPicker()`/`click()` 唤起原生选择器，但 iOS Safari 等移动浏览器只认落在控件本体上的真实点击，程序模拟点击不生效；现在改为直接展示原生日期输入框，任何设备直接点选。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.3`。

## 2.8.2

### 修复 / Fixed

- 修复 Zeabur 等跨域部署在 Streamable HTTP + 静态 Token 鉴权下无法连接 `/mcp`：浏览器的 `OPTIONS /mcp` 预检现在显式跳过 MCP 鉴权，CORS 中间件调整到鉴权外层，预检不再返回无 CORS 响应头的 401；鉴权失败响应也会携带正确的 CORS 响应头，Polaris 网页版和桌面版可正常发起后续带 Token 请求。

### 维护 / Maintenance

- 完成 2.7.8 启动、跨越 2.7.8—2.7.10 三个正式版本的首批 `src/` 扁平模块兼容观察期：经生产引用、测试、活动文档与部署入口审计后，移除 memory/plan/provider/public-origin/scoring、storage/deployment、ledger/projection 共 16 个顶层兼容壳；仓库测试全部切换到 `ombrebrain.*` canonical package，避免内部代码继续延长旧路径生命周期。
- 修正内部资料忽略边界：`docs/superpowers/`、代码健康审计、内部 TODO 与旧版发布草稿不再受 Git 跟踪，并补入 `.gitignore`；运行时覆盖矩阵不再发布内部计划文件路径。

### 测试 / Tests

- 新增鉴权中间件预检放行与完整 Streamable HTTP 中间件栈回归，覆盖静态 Token 模式下 `OPTIONS /mcp` 返回 200、允许 `POST` 及 `Authorization`/`Content-Type` 请求头。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.2`。

## 2.8.0

### 修复 / Fixed

- 保留 `hold` 与 `grow(items=...)` 的自动合并能力，同时在相似候选之后增加保守的“同一具体事件”判定；仅主题、人物或情绪相似，日期、场景或关键动作不同的独立事件不再串入旧桶，完全相同正文继续保持幂等。
- 修复 `breath_advanced(catalog=True)` 忽略 `tags` 与 `max_results`：目录模式现在执行 tags AND 过滤并遵守返回上限，仍保持 0 LLM、只读元数据。
- `breath_search` 与 `breath_advanced` 新增 `date_from/date_to` 创建日期过滤，支持 `YYYY-MM-DD` 与 ISO 8601；自由联想也受同一日期范围约束，避免按日期检索时漂出范围外旧桶。
- 为语义检索补充不记录查询原文的诊断日志，包含查询哈希、向量候选与得分、embedding 引擎和耐久 outbox 状态，便于区分索引未更新、服务不可用和排序结果问题。
- OAuth 授权页增加提交中状态、重复提交保护、30 秒超时提示与诊断编号；服务端按同一编号记录提交、密码失败和跳转阶段，便于定位授权页面卡住。

### 行为说明 / Behavior

- 保留检索命中不足时浮现 3–5 条低权重旧记忆的自由联想设计，并以“非检索命中”独立分区明确标记。
- 保留核心准则无条件注入设计；传入 tags 时会明确说明 tags 只过滤普通浮现记忆。

### 部署 / Deployment

- 新建并验证 Zeabur 一键部署模板 `WB5ZKE`，README 部署按钮已指向新模板，同时保留 Deploy from GitHub 备用流程。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.8.0`。

## 2.7.9

### 修复 / Fixed

- 修复 `grow` 长内容拆分所用的 digest prompt 未注入第一人称视角铁律：现在与 dehydrate/merge 共用同一规则，AI 自身保持“我”，人类一方保持配置名称，并禁止动作或情绪主语翻转（#62）。

### 文档 / Documentation

- 补清 Zeabur/Render 反代后的 OAuth 公网来源配置：标准 `X-Forwarded-Proto` / `X-Forwarded-Host` 已受支持，但只采信可信最后一跳；托管平台应在安全部署向导填写 HTTPS 公网地址，避免 OAuth 元数据回落为容器内部 `http://`，同时禁止用 `0.0.0.0/0` 放宽代理信任（#63）。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.9`。

## 2.7.8

### 维护 / Maintenance

- 渐进整理 `src/` 根目录：将领域消息、计划历史、服务商识别、公开来源校验、部署模式、检索评分、媒体与备份存储、embedding outbox、ledger 及 projection 实现迁入 `ombrebrain/` 对应领域包；仓库内生产代码改用新的 canonical package 路径。
- 旧的顶层 Python 导入路径暂时保留为轻量兼容壳。本版本开始计算三个正式版本的弃用观察期：`2.7.8`、`2.7.9`、`2.7.10` 保持兼容；最早在 `2.7.11` 经引用、文档、部署和完整回归审计后删除，不能仅按版本号自动移除。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.8`。

## 2.7.7

### 修复 / Fixed

- `trace` 正式支持 `old_str/new_str` 原文片段局部替换：在单桶跨进程锁内读取完整正文并仅替换唯一的逐字命中，长 pinned 桶尾部同样有效；零命中、重叠或普通多命中、替换后正文为空都会明确拒绝且不写盘，`new_str=""` 可删除不会清空整桶的局部片段。替换后的正文继续受 50KB 上限约束并正常重建 embedding，plan 并发编辑也会在锁内追加 change log。`content` 与局部替换互斥，未知或拼错的 trace 参数也不再被 FastMCP 静默吞掉后误报“没有任何字段需要修改”。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.7`，Dashboard、运行时和热更新检查显示一致。

## 2.7.6

### 修复 / Fixed

- 修复脱水视角规则的反向遗漏：禁止把人类一方的动作或情绪误归给“我”；原文省略主语时优先依据紧邻上下文，无法判断则保留省略结构，不再擅自补成第一人称，并通过 prompt v4 使旧脱水缓存自然失效。
- 记忆桶批量选择的“全选”改为只作用于当前页；翻页后复选框按当前页重新计算全选/半选状态，取消全选不会清掉其他页已手动选择的桶。
- 修复记忆桶时间顺序下拉控件被全局表单 padding 挤压、文字下半部遭裁剪的问题；控件现在使用独立的垂直内边距和安全行高自适应内容。

### 维护 / Maintenance

- 清理历史对话导入合并路径中未使用的局部变量，保持全库静态检查通过。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.6`，Dashboard、运行时和热更新检查显示一致。

## 2.7.5

### 新增 / Added

- “已导入记忆”卡片新增直接编辑入口：点击后读取完整桶正文并自动展开现有编辑器，保存后刷新卡片且保持列表滚动位置。

### 修复 / Fixed

- 修复云端与本地 embedding 配置串台：服务商预设现在会把 format、Base URL、model 与可选 Key 作为完整配置一次保存，迁移接口能正确持久化显式空 Base URL；本地 Ollama 会忽略残留的 Gemini/SiliconFlow 云端地址并使用独立本地地址，SiliconFlow 的 `bge-m3` 会安全规范为官方模型名 `BAAI/bge-m3`，错误面板及当前后端状态也会正确区分 `ollama` 与云 API。
- 修复普通 `hold` 偶发误报“向量化失败”的竞态：新 Markdown 发布后会在任何 meaning/网络等待前立即对 ID 查询可见，后台 worker 不再把刚创建的桶误判为已删除并丢弃正文向量任务。
- embedding outbox 对账改为单调补任务：衰减自愈或手动补齐拿到的旧快照不能再删除新任务、覆盖新 content hash 或重复入队；meaning-only 行会补建正文向量，而已有正文向量但旧版 hash 为空的历史行不会触发全库重算；记忆只移动到归档时保留待处理任务和已有向量，只有真正物理删除才清理。
- Dashboard 向量补齐在清理孤儿索引前会回查 Markdown 真源，不再因扫描快照过时而误删并发 `hold` 刚生成的向量。
- 若写入后的 outbox 任务异常缺失但向量仍未生成，`hold/grow` 会从 Markdown 真源自动重新入队；只有无法入队时才提示当前降级，不再把限流、超时或内部竞态一律误导为 API Key 错误。
- Dashboard 详情与“已导入记忆”列表只接受最后一次请求结果，快速切换记忆或并发刷新时旧响应不再覆盖当前编辑对象和新卡片。

### 安全 / Security

- 本地 Ollama 运行时固定使用无秘密占位 token，保留在配置中供切回云端的 Gemini/SiliconFlow API Key 不再作为 Bearer 发往本地或自定义 Ollama 地址。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.5`，Dashboard、运行时和热更新检查显示一致。

## 2.7.4

### 修复 / Fixed

- 修复取消钉选时 importance≥9 配额再次虚高的问题：计数现在与 `breath_advanced(importance_min=9)` 的可审计普通记忆范围一致，排除 pinned/protected、主动遗忘、feel/plan/letter、归档/删除终态，并按逻辑 bucket ID 去重；不再把 18 条普通高重要度误报为 89 条物理/特殊记录。
- 同步修正 pinned 配额的旧数据计数：文本 `"false"` 不再被当成已钉选，同一 bucket ID 的物理副本只计一次，归档/删除终态不占名额。
- 统一高重要度占位判定到 trace、Dashboard 快速解钉/完整编辑、导入复核、历史对话导入和单条/批量恢复浮现；所有从特殊/隐藏状态重新进入普通高重要度池的转换都会在同一配额锁内检查并落盘。新建或合并把低重要度桶提升到 9+ 时也不再绕过硬上限。
- 修复配额/同内容写入队列中等待请求被取消后可能阻塞后续请求或提前打开串行屏障的问题；取消现在不会传播到前序 Future，交接回调也不会在非重入锁内自死锁。
- 归档和软删除现在是存储层终态：快速钉选、Dashboard 编辑、导入复核或并发普通更新都不能再把 archived/deleted/tombstone 桶意外迁回活跃目录。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.4`，Dashboard、运行时和热更新检查显示一致。

## 2.7.3

### 新增 / Added

- 记忆桶分页新增首页、末页和输入具体页码跳转；非法、空白、小数或越界页码会被安全归一化，删除末页内容或刷新后页数减少时自动收敛到有效末页。
- 记忆桶新增“综合分优先 / 最新创建优先 / 最早创建优先”排序并记住用户选择；时间顺序按桶的首次 `created` 记录解析真实时区，缺失或无效时间稳定排在末尾，时间视图直接展示创建日期。

### 修复 / Fixed

- 修复三个官方 Docker Compose 模板未透传 `OMBRE_TRUSTED_PROXY_CIDRS` 的部署缺陷；外置 nginx/Caddy 位于 Docker 网桥时，可信代理配置现在会真正进入容器，避免合法 Dashboard 写操作、热更新和重启因内部 Host/协议被误判为跨来源。
- 热更新失败时读取并显示服务端错误；若命中 `Cross-origin request rejected`，Dashboard 会明确提示这是 CSRF 来源校验而非 CORS，并指向 Host、HTTPS 转发头和最后一跳代理 CIDR。
- 补充 nginx 安全反代示例、非默认端口规则及 v2.7.0 无法通过 Dashboard 自更新时的宿主机脱困步骤；不放宽热更新或重启接口的 CSRF 保护。
- 修复空记忆筛选残留旧页码/全选状态、非法时间显示 `NaNmo前`，并让同分桶及同时间桶在刷新后的顺序保持确定；切换排序时不会因当前 domain 掉出前 10 而误退回“全部”，容器与浏览器时区不同时也使用服务端规范化时间保持排序和显示一致。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.3`，Dashboard、运行时和热更新检查显示一致。

## 2.7.2

### 修复 / Fixed

- 澄清并锁定 `trace` 删除契约：普通记忆与 plan 的 `hard_delete` 会明确拒绝且不再被误解为顺带归档；测试桶清理必须提供非空、最多 500 字符的 `delete_reason`，并拒绝与 `delete=True` 同时使用。形式化不变量现在只依据创建事件中的不可变 `test_data` provenance 豁免合法测试清理，删除事件无法自行伪造该资格。

### 版本 / Version

- 根目录 `VERSION` 与热更新优先读取的 `src/VERSION` 同步更新为 `2.7.2`，Dashboard、运行时和热更新检查显示一致。

## 2.7.1

### 修复 / Fixed

- 修复 `breath` 默认 token 预算、精准查询、`max_results`、`catalog` 与 pinned/core 渲染回归；目录模式不再注入全文，普通命中不会再被无关核心准则挤出预算。
- 修复 Dashboard 记忆编辑、pinned/type/importance 联动，以及取消钉选或保存后丢失当前 tab/页码的问题。
- 修复 grow/API Key 热更新与 GitHub 备份配置持久化；配置先原子落盘并回读成功后才更新运行时，不再出现界面报成功、重启后被旧值清空。
- 修复 Dashboard 公网 MCP 地址与安全部署向导无法可靠保存/回读，以及 OAuth 换取 token 成功后 `/mcp` 循环 401：公网 origin 统一规范化并绑定 discovery、授权、刷新与 MCP 校验；旧地址授权要求重新认证，不再签发必然失效的 token。
- 修复托管平台/反向代理改写 Host 或 HTTPS 协议时，Dashboard 同源的记忆编辑与两组 API Key 保存被 CSRF 防护误判为 `Cross-origin request rejected`；浏览器同源信号及已保存公网 origin 可安全放行，真实 `same-site`/`cross-site` 请求仍拒绝；无初始化 token 的首启设密同时校验回环 peer 与唯一回环 Host，阻断 DNS rebinding 抢占。
- 完整备份迁移改为 disk-backed upload/extract/apply：请求先占位再流入 spool，桶与 SQLite 只保留路径，冲突在 bucket 锁内重验，`overwrite` 采用 staged commit + 历史副本 + 失败回滚；导入、解析、应用均以 generation 防并发串包。
- 修复 Render 512 MB 场景下迁移、全量导出与日志读取的额外 OOM 风险；导出改为有上限的磁盘流式 ZIP/FileResponse，正常、Range 提前返回和断连均清临时文件，日志只倒序扫描有界尾部。
- 强化登录、恢复与 OAuth 密码验证：PBKDF2 移出事件循环，加入跨 event-loop 并发上限、全局/来源双限流、有界来源状态及 IPv6 `/64` 聚合。
- 强化 auth/OAuth rotation 原子性：密码/session/grant 使用 generation/CAS，code 与 refresh 单次消费，换密/revoke 阻断在途授权复活，持久化失败不会消费旧 grant 或发布半状态；DCR 增加双层限流、未激活 TTL 与安全驱逐。
- 修复热更新跨 event-loop 双任务、同步 I/O/子进程阻塞和 SSE 断连竞态：进程级单飞占位，阻塞阶段移出事件循环，取消等待 worker 后回滚并清理，根目录与 `src/VERSION` 同步更新。
- 强化持久化 prompt 注入数据边界与 dream/hook 最终预算：存储/派生内容显式标为不可执行数据，provenance 有界；dream 把完整渲染计入硬上限，hook 限制 provider 调用、并发与超时，token 只接受 header/Bearer。
- 强化公开健康/引导接口、备份/下载边界、embedding 迁移单所有者与供应链完整性校验。
- 完成全项目代码健康度、14 个 MCP 工具 Docker 集成、边界/异常路径及红蓝对抗测试；完整报告见 `docs/CODE_HEALTH_AUDIT_2026-07-15.md`。

### 版本 / Version

- 根目录 `VERSION` 与热更新必读的 `src/VERSION` 同步更新为 `2.7.1`，Dashboard 与热更新检查均可见。

## 2.7.0

一次系统性找茬（对抗式代码审查）后的批量修复，覆盖安全、数据丢失、竞态、检索质量、历史对话导入四类问题。

### 安全 / Security

- 修复 CORS 通配符（`allow_origins=["*"]`，覆盖除 `/mcp` 外的整个 HTTP app）叠加 `/auth/setup` 无限流，可被恶意网页跨站劫持首次设置的 Dashboard 密码：新增 `OriginCSRFGuardMiddleware`，非安全方法（POST/PUT/DELETE）且 `Origin` 与自身 `Host` 不符即拒绝，豁免 `/mcp`/`/oauth/*`/`.well-known`（这些走 Bearer token / PKCE，不依赖 cookie）；`/auth/setup` 补上与 `/auth/login` 一致的限流。

### 修复 / Fixed

- `trace(bucket_id, importance=9)` 完全绕过 importance≥9 硬上限（配额检查此前只在 `hold` 的创建路径生效）；`letter_write` 固定 `importance=10` 却未排除在配额计数外，会永久占位挤占正常记忆的配额；pinned/anchor/importance 三处配额都是「先数后写」两步走，并发请求可冲破硬上限——三处统一接入跨进程文件锁（`_quota_turn` / `_bucket_turn`）序列化。
- 导入别的 OB 实例导出的备份包时，`overwrite` 冲突处理是「先删旧桶、再写新内容」，写新内容失败会导致旧桶已删、新内容未写，净丢失一条记忆；改成新内容先完整落盘到暂存文件，确认成功后才处理旧桶。
- `bucket_manager` 的 `archive()`/`update()`/`delete()`/`touch()` 各自独立读改写、互不知会，衰减引擎后台归档撞上并发的 `trace`/`hold` 写入时可能把已归档的桶在原路径复活成一份带旧内容的重复桶；四个方法统一接入同一把跨 loop/进程文件锁。
- embedding 后端切换（本地 ↔ API）的迁移引擎文档写了「先写 `.migrating` 暂存、全部成功后原子替换主库」，实际代码从始至终直接原地改 `embeddings.db`，中途失败会让主库永久混入新旧模型/维度不一致的向量；现在真正实现了暂存+原子替换，并给断点续传的 checkpoint 加上目标签名（backend:model:dim），换目标后不会把不兼容的旧向量当成「已完成」。
- 衰减引擎的自动结案（重要度≤4 且超期未激活 → 强制 `resolved=True`）漏排除 `plan`/`letter` 类型，违反「plan 生命周期只由 status 驱动、letter 永久原样保留」的设计承诺。
- embedding 后台重试队列的熔断器把「单条内容本身有毒（比如触发 provider 内容过滤）反复失败」和「供应商真的挂了」算成同一件事，一条坏内容能把熔断顶到 600 秒上限、连累所有新写入的合法记忆一起卡住；改成只有失败发生在不同的桶身上才计入熔断计数。
- `breath` 无参浮现排序的情感强度 tiebreak 用 `meta.get("arousal") or 0.3`，Python 里 `0.0` 是合法存储值却被 `or` 当缺失值静默换成默认值，误伤效价/唤醒度恰好为极端值的记忆。
- Dashboard `/api/search` 语义索引不可用时完全静默降级，响应体和「语义检索正常」时长得一模一样；改成显式跑一次向量查询，降级状态通过 `X-Semantic-Search` 响应头暴露（响应体形状不变，不破坏现有前端）。
- 历史对话导入（`import_memory.py`）四处修复：① `preserve_raw` 特殊内容断点续传时会重复导入（进度只在整个 chunk 处理完才落盘，崩溃重启后同一 chunk 重新提取一遍，原文逐字保留场景原来完全没有去重）；② `source_hash` 只按原文算，没算进 `human` 称呼字段，暂停期间改了称呼会导致续传时分块边界错位；③ 单次提取正文固定按 12000 字符截断，对英文/中英混合内容而言远小于块本身 ~10000 token 的目标预算，且不留任何痕迹地丢内容；④ 单条存储失败只打日志不计入 `state.errors`，`/api/import/status` 看不出为什么创建数比调用数少；顺带把 `ImportState.save()` 换成 `utils.atomic_write_text`（补上 fsync 与 Windows 长路径前缀）。
- `github_sync.py` 从 GitHub 恢复备份时写文件用裸 `open()`/`os.makedirs()`，没有 Windows 长路径前缀，深层目录结构的备份在超过 260 字符 MAX_PATH 时会静默跳过该文件。

## 2.6.13

### 修复 / Fixed

- 修复 GitHub 备份配置（token/仓库/分支/路径前缀）保存后过一两个小时又被清空的问题：根因是 `config.yaml` 的保存一直用「读现有内容 → 改一个 key → 整份覆盖写」，写失败时只记日志、接口仍返回"已保存"，用户看到成功提示，但磁盘其实没落地，下次进程重启（崩溃/热更新/手动重启）就会读到没写成功的旧文件，把内存里的新配置盖掉。
- 同一读改写模式在 `buckets.py`（采样权重、显示称呼两处设置）里还会在写失败时**静默保留"保存成功"提示**，一并修复为如实报错；`config_api.py`（主配置持久化、MCP token 重新生成、env 字段热更新、传输模式切换）与 `embedding.py`（向量化迁移后落盘，此前是 OB-W005 维度错乱复发的成因之一）四处虽然本来就会如实报错，但缺少加锁与原子写，此次一并纳入同一套机制。
- 新增 `utils.atomic_update_config_yaml()` 作为所有 `config.yaml` 写入的唯一入口：全局锁避免多个保存接口并发写互相覆盖、临时文件 + `os.replace` 原子替换、写后回读校验，任何一步失败都如实抛出。

## 2.6.12

- `hold` / `trace` 的媒体输入现在会复制到 OB 持久媒体目录，支持服务器可读路径与 `data_base64`，不再把客户端临时路径直接写进记忆。
- 新增 `OMBRE_MEDIA_DIR`、`OMBRE_MEDIA_MAX_BYTES`，并补齐完整环境变量清单及禁止静默改名的工程规范。
- 永久兼容 `OMBRE_API_KEY`、`OMBRE_BASE_URL`、`PASSWORD` 等旧部署变量名，正式变量名存在时优先使用正式名称。

## 2.6.11

- 修复 `breath` 工具因参数过多（9 个）导致 claude.ai 按需加载工具时常年跳过它、记忆无法自动浮现的问题：拆成 `breath()`（0 参数，日常浮现）/ `breath_search(query, domain, max_results)`（3 参数，检索）/ `breath_advanced(...)`（完整 9 参数，供 catalog/tags/importance_min/valence/arousal/max_tokens 等高级模式使用）三个 MCP 工具，共用同一套内部实现，检索/浮现逻辑本身不变。工具总数由 12 个变为 14 个。
- 新增 `NgrokHeaderMiddleware`：给所有 HTTP 响应（含鉴权拒绝/出错响应）加 `ngrok-skip-browser-warning: true` 头，避免 ngrok 隧道部署时免费版浏览器警告拦截页挡住 claude.ai 的 MCP 请求。

## 2.6.10

### 安全 / Security

- 修复 2.6.9 引入的回归：给「永久删除测试桶」按钮统一加图标时，行内 `style="display:inline-flex"` 覆盖了 `.developer-only { display:none; }` 这条控制显隐的 class 规则（行内样式优先级恒高于 class 选择器），导致该危险操作无论是否开启开发者模式都会显示给所有用户。现在去掉了这个按钮自身 style 里的 `display`，显隐重新完全交给 `.developer-only` / `body.developer-mode .developer-only` 两条 class 规则控制；其余三个动作按钮不受影响，靠右对齐和图标不变。补了一条回归测试直接检查该按钮的行内 style 属性不得含 `display`。

## 2.6.9

- 记忆桶列表工具栏视觉细节修正：`主动遗忘`/`沉底`/`归档`/`永久删除测试桶` 四个动作按钮从整条左对齐堆放改为整体靠右（与左侧 `全选当前筛选`/`已选` 分开两组），并补上与站内其他位置一致的图标（`eye-off`/`moon`/`archive`/`trash-2`）。外框改成和其他卡片一致的圆角+浮起阴影，去掉此前突兀的方角细边框。

## 2.6.8

- 修复「实际生效配置」诊断项无论怎么设置都持续显示「需处理」的问题：该项此前只认走完 `/onboarding` 向导写入的 `deployment.profile`，在 Dashboard「MCP 连接」面板直接保存鉴权设置不会触发。现在只要 `config.yaml` 里出现过 `mcp_require_auth` 或 `mcp_auth_mode`（即手动保存过一次），即视为主动配置，诊断项转为正常；从未配置过的全新安装仍会照常提示。
- 修复记忆桶列表工具栏（全选当前筛选 / 已选 / 主动遗忘 / 沉底 / 归档）字号与周围按钮不一致的问题，统一为与站内其他按钮一致的 12px + 32px 高度。
- 「开发者模式」开关从记忆桶工具栏移到设置 → 高级区域最底部，单独成一个明确标注风险的区块，并换成站内统一的胶囊开关组件；受它控制的「永久删除测试桶」按钮仍保留在原处。

## 2.6.7

- 新增 `/mcp` 静态 Token 鉴权模式（`mcp_auth_mode: token`），与 OAuth 互斥、三选一：默认 `oauth` 不变、`token` 供支持自定义请求头但走不通浏览器 OAuth 授权流程的第三方 MCP 客户端使用、`off` 保持原有免鉴权语义。Token 走 `Authorization: Bearer` 或 `Ombre-MCP-Token` 请求头，不支持 URL 查询参数；选了 `token` 后 OAuth 的 discovery/register/authorize/token 路由全部 404。
- Dashboard「MCP 鉴权」区支持一键切换三种模式、生成/轮换静态 Token（生成即时生效、切换模式仍需重启），并对隧道 + Token 模式给出针对性的公网暴露风险提示。
- 修复 `src/VERSION` 落后于根目录 `VERSION` 的问题（2.6.6 发布时只 bump 了根目录，Dashboard 版本号一度显示 2.6.5）。

## 2.6.6

- 新增三模式安全部署向导：普通用户只需选择本机、公网安全或高级模式；公网安全模式强制 OAuth，并在保存前校验 HTTPS 边界。
- 系统体检新增“实际生效配置”，并列展示 `config.yaml` 已保存值、当前进程值、环境来源、真正覆盖项和持久卷状态，解决托管平台环境变量覆盖 Dashboard 后难以排查的问题。
- Docker/VPS 默认只绑定宿主机回环地址；Docker、Render 与 Zeabur 文档统一把配置和记忆落在持久目录，托管平台不再推荐用 OAuth 环境变量覆盖面板设置。

- Dashboard 像素小鸡现在支持鼠标与触屏拖动，抓起时会随机吐槽并记住停放位置；窗口尺寸变化时自动限制在可视区域内，既不挡翻页按钮，也不会被拖丢。新增位置感知对白、深夜提醒、左右摇晕、反复搬运装死、闲置打盹、记忆写入/遗忘/测试清理反馈、真实记忆保护和搜索暗号等低打扰彩蛋。
- 点击小鸡身体左右侧可以挠痒，连续互动会逐步升级吐槽；429、无效 API Key、向量重建、空记忆和连接失败等真实状态也会触发对应彩蛋。
- 记忆列表新增多选与“全选当前筛选”，支持批量主动遗忘、沉底和归档；开发者模式新增受保护的测试数据永久删除，但只接受创建时明确标记 `test_data=True` 的桶。真实记忆仍只能被遗忘、沉底或归档，AI 与 Dashboard 共用同一后端边界。

### 安全 / Security

- OAuth 鉴权开关现在明确区分“已保存配置”与“当前进程实际生效值”；鉴权中间件与 OAuth 路由可见性仍坚持启动时快照，不再将仅写入配置的变更误报为热切换成功。
- Dashboard 通用重启接口必须通过 Dashboard 会话鉴权，且请求体必须显式携带 `confirm=true`，避免公开页面或误触发直接终止服务。
- OAuth DCR 客户端注册表使用私有权限原子 JSON 落盘；启动时会重新校验回调 URI、过期时间和数量上限，防止损坏或手工篡改的注册表绕过安全边界。

### 改进 / Changed

- Dashboard 右上角新增“重启”按钮；OAuth 等启动时配置存在待生效变更时显示红点和明确提醒，重启请求返回后自动重连页面。
- 相似度检索改为 NumPy 批量矩阵计算，同时支持 content/meaning 双向量取最高分；保留维度不匹配记 `0.0` 和同分结果的稳定顺序，避免旧 PR 的行为漂移。
- DCR 客户端有效期与 refresh token 对齐为 365 天，容器或裸机服务重启后无需客户端清缓存重新注册。

### 测试 / Tests

- 新增 DCR 重启恢复与恶意落盘数据过滤、OAuth 待重启状态、受保护重启 API、双向量批量相似度与稳定排序回归；完整测试集通过。

## 2.6.4

### 修复 / Fixed

- 修复 Dashboard“信”页面及桶详情的编辑、删除按钮偶发完全无响应：装饰性 Lucide 图标不再截获指针事件，鼠标按下与抬起稳定落在按钮本体。
- 修复全局 `MutationObserver` 与 `lucide.createIcons()` 互相触发的无限 SVG 重绘循环；图标渲染期间暂时断开观察器，避免 Console 警告/计数持续暴涨、CPU 占用和页面卡顿。
- 信件删除接口支持半删除状态的幂等自愈：Markdown 已不存在时，仍清理残留向量、embedding 待处理项和运行时桶缓存；正常存在的信件继续保留移入 archive 的软删除语义。

### 测试 / Tests

- 新增 Dashboard 图标点击与观察器防自噬、幽灵信派生状态清理回归；完整测试 `1049 passed, 45 skipped`。

## 2.6.3

### 安全 / Security

- OAuth 受保护资源元数据现在严格服从 `mcp_require_auth`：关闭鉴权时相关元数据端点返回 404；仅真实存在的 `/mcp` 路径可被发现，废弃或不存在的 MCP 路径不再误报 200；根元数据明确指向实际 `/mcp` 资源。
- 当隧道已配置而 MCP OAuth 关闭时，Dashboard 持续显示醒目的匿名公网读写风险提示，系统诊断将该组合升级为错误并给出修复建议。

### 修复 / Fixed

- 修复 Dashboard“启动时自动连接”开关可能被并发状态轮询弹回、保存失败却无明确反馈的问题。隧道配置改为加锁、原子写入、落盘回读校验；前端仅接受服务端明确确认的持久化结果，并显示真实保存错误。

### 测试 / Tests

- 新增 OAuth 开关与路径发现、隧道配置持久化失败、Dashboard 保存确认及公网暴露诊断回归，并通过 Docker 实例验证自动连接开关可写入和回读。

## 2.6.2

### 修复 / Fixed

- 修复 Dashboard 检查更新读取 `main/VERSION`、实际下载却默认取 GitHub Latest Release 的源不一致：当 Latest 仍停在 v2.4.6 时，第一次更新会降级到 2.4.6，第二次才由旧更新器拉到最新。现在默认与版本检查一致地下载 `main`，并在写盘前拒绝任何低于当前版本的更新包。
- “补齐缺失向量”升级为完整向量对账：除补齐缺失/过期向量外，也会清理“向量存在但 Markdown 桶已不存在”的孤儿向量；Dashboard 分别显示发现孤儿、已清理、清理失败、缺失和入队数量，不再出现诊断告警 114 条但按钮显示待处理 0、加入 0 且无动作的误导。

### 测试 / Tests

- 新增热更新降级保护、默认更新源一致性及孤儿向量清理回归；相关测试 42 项通过。
- Docker 真机插入孤儿向量后运行 Dashboard 对账：孤儿数从 1 降为 0，状态显示发现 1、清理 1、失败 0。

## 2.6.1

### 修复 / Fixed

- `breath(query="<完整 bucket_id>")` 新增按 ID 直读通道：直接返回桶当前存储的完整 raw content，跳过 embedding、BM25 和 LLM 摘要/改写，避免 AI 在 `trace(content=...)` 前只能拿到压缩内容，进而覆盖原始 bullet、缩进或遗漏信息。
- 精确 ID 读取继续遵守归档、软删除、专用桶类型与浮现策略边界；token 预算不足时整桶省略，不截断或压缩正文。

### 测试 / Tests

- 新增单元回归，断言精确 bucket ID 读取不调用 embedding、BM25/search 或 dehydrator，并逐字校验原始正文哈希。
- 本地 Docker `streamable-http` 真机验证全部 12 个 MCP 工具及安全/并发边界；新增 35 条纯 bullet 桶 → 按 ID 原文读取 → `trace` 追加第 36 条 → 再次逐字读取的端到端回归。

## 2.6.0

### 修复 / Fixed

- Docker 代码播种不再只比较 `VERSION`：新增 `src/` + `frontend/` 稳定 SHA-256 镜像指纹，同版本自建镜像内容变化也会重新播种；镜像未变化时保留卷内 Dashboard 热更新。
- 镜像重新播种改为暂存、校验后切换，原健康运行树可作为 `_prev` 崩溃回滚点；回滚不会被同一次启动立即反向覆盖。
- 独立 bind/named/anonymous 代码卷可通过 mountinfo 正确识别为持久热更新，不再只认位于 `buckets_dir` 下的代码目录。
- 启动日志明确输出活动代码目录及 `image-match` / `runtime-override` / `legacy-residue` 状态；旧布局 `<数据目录>/_app` 仅在确认未被使用时告警，不自动删除。

## 2.5.5

### 变更 / Changed —— Dashboard 面板重设计（纯前端，不动数据结构）

信息架构与文案按「面板只回答好不好、婷易的妈妈能看懂」原则整体重做，均集中在 `frontend/dashboard.html`：

- **诊断 `[object Object]` 泄露修复**：`renderDiagnosticCheck` 面板主体只留 状态灯 + 一句人话 + 建议，原始 details（schema 名 / 字段路径 / 嵌套 JSON）一律折叠进「查看详情」；`esc()` 加对象兜底，任何调用点都不再吐 `[object Object]`。
- **顶栏常驻系统状态条**：全绿收成一行「系统正常 · OK」；有问题才展开「需处理」卡片，按钮 `scrollToField()` 直达对应设置字段并高亮。
- **体检项按 用户 / 开发者 分离**：22 项里只把 8 项用户相关项（数据目录 / 记忆桶 / 压缩LLM / 向量化 / 数据完整性 / GitHub备份 / 访问控制 / 运行时）显示给用户；14 项内部契约检查（事件账本 / 红线 / vNext 预检 / ADR / 代码规范…）折叠进「开发者诊断」，不再让用户误收「需处理」告警。英文术语全部中文化（surface context→浮现上下文 等）。
- **「危险区」正名「数据备份与迁移」**：去掉红色危险样式（原区实为导出/查重/zip迁移，零破坏性操作），改鼓励使用的语气。
- **设置结构重组**：加「常规 / 高级 / 备份与迁移」三个子 tab（`data-sgroup` + CSS 显隐，不物理挪 DOM），GitHub 同步归入备份组；去掉段标题的 ⓪①② 圆圈编号。
- **工具箱 tab 合并进设置**：其开关/动作在设置里本已存在（采样→桶行为、外网→我、备份→GitHub），删除重复 tab，消除「一件事劈两半」。
- **记忆桶列表翻页**：每页 10 个 + 底部翻页；搜索栏支持桶名 / 子串 / 模糊（子序列）匹配，向量化未开启时退化为纯本地匹配。
- **中英双语与字体统一**：段标题 / 子标题 / 按钮 / 空态统一「中文 + 英文小字」；`button/input/select/textarea` 强制继承 body 字体，消除表单控件用系统字体造成的割裂。
- **文案分级**：平台环境变量告警等长注释重排为「结论一句 + 框住的变量清单 + 分点说明」；删除对用户无意义的解释性文字；`一键本地化` 按钮回归短标签，操作说明下沉为小字。

### 测试 / Tests

- 全量内联 JS `node --check` 通过；本地 Docker（`deploy/docker-compose.yml`）部署验证：登录、`/api/buckets`、`/api/system/diagnostics` 正常；造 25 桶验证分页（10/10/5、首末禁用、无重叠）与搜索（桶名 / 子串 / 模糊子序列 / 多命中）；22 项体检的 用户/开发者 分离用真实返回核对（8 用户 + 14 开发者）。
- 记录并根治部署坑：数据卷 `<vault>/_app` 影子副本 + VERSION 门控 reseed 导致「改前端不生效」，本次 VERSION 抬升会触发运行时自动重新播种。

## 2.5.4

### 修复 / Fixed

- `atomic_write_text`（`src/utils.py`）在 Windows 上未做长路径处理：domain/tag 合法长度可到 80~128 字符，落在较深的数据目录下时，拼出的桶文件全路径（含原子写入用的 `.tmp` 后缀）会超过 Windows 默认 260 字符 `MAX_PATH`，导致 `hold`/`grow` 等写入直接抛 `FileNotFoundError`（`tests/test_red_team_regressions.py::test_bucket_boundary_bounds_tags_and_domains` 在 Windows 上复现）。现在在 Windows 上一律用 `\\?\` 扩展路径前缀绕过该限制，Linux/macOS 行为不变。
- `grow(items=...)`（预拆分逐字入库分支，`src/tools/grow/core.py`）在打标 API 不可用（未配置 `OMBRE_COMPRESS_API_KEY` 或调用失败）时会直接吞掉整条内容、不创建任何桶——与文档承诺的「一字不动只补元数据」矛盾，也和同类工具 `hold` 的降级行为不一致（`hold` 会在打标失败时落回本地中性元数据并原样保存正文）。真机验证 12 个工具时发现：无 API Key 场景下 `grow(items=[...])` 返回「新0合0」，正文全部丢失。现已改为与 `hold` 一致的降级路径：打标失败时使用本地中性元数据继续建桶，正文不丢，并在返回里追加提示。
- 修复 `.gitignore` 里一条整目录忽略 `/tools/`，导致新脚本 `git add` 被静默吞掉、从未进仓库：pytest 12 个用例因此依赖的 `tools/vnext_preflight.py` 缺失而失败，系统诊断的 vnext_preflight 检查也永久报错。新建该 CLI（照 `tools/v3_health_report.py` 模板）并放开 `.gitignore`。
- 补建 README「检索质量评测」一节引用、但从未存在过的 `tools/evaluate_retrieval.py`（离线关键词通道 + `--with-embedding` 混合检索，输出 Hit@K/Recall@K/MRR）。
- 移除 `letter_write` 的过时校验实现 `src/tools/letters.py`（全仓零引用死代码，实际生效实现在 `tools/plan/core.py`，本就允许任意署名字符串），并修正 README 对 `author` 字段的过时描述。
- 移除 `/dream-hook` 端点与 SessionStart hook 里的调用：`dream`（做梦消化）按设计哲学不是义务，不该在每次会话开始被自动触发，只应由模型在需要消化时主动调用 `dream` 工具。
- SessionStart hook 脚本（`.claude/hooks/session_breath.py`）此前调用 `/breath-hook` 不带任何 token、遇 401/网络错误静默吞掉，看起来"运行正常"实则没有 breath；现已支持 `OMBRE_HOOK_TOKEN`（Authorization Bearer）、出错时打印可诊断信息到 stderr（不阻断会话启动）、默认 URL 改为 `http://localhost:18001`（此前误写 `:8000`，与 Docker 对外默认端口不符）。
- Docker 快速开始路线此前存在 onboarding 断点：README 引导把 `docs/CLAUDE_PROMPT.md` 放进 system prompt，但预构建镜像的 `.dockerignore` 排除了整个 `docs/` 和所有 Markdown，Docker 用户本地无源码也拿不到该文件。现将面向用户的 `docs/CLAUDE_PROMPT.md`、`docs/INTERNALS.md`、`docs/MULTI_OWNER.md`、`docs/OPERATIONS.md`、`README.md`、`CHANGELOG.md` 放行进镜像；内部设计稿（`docs/superpowers/`、`docs/secrets/` 等）仍不进镜像。
- 同时把 `.claude/hooks/session_breath.py`（原被整个 `.claude/` 目录忽略规则挡住、用户无处获取）放行为官方产物。
- 删除仓库根目录的 `dashboard.html`：它只是 `frontend/dashboard.html` 的字节级镜像，运行时代码（`src/web/dashboard.py`）从不读取它，纯粹为满足一条"两份必须一致"的测试断言而人工维护，是「同一事实存两处」的反模式。改为单一真源，`tests/test_release_audit_regressions.py` 及另外 5 个内容契约测试同步只校验 `frontend/dashboard.html`。
- Dashboard 前端（`frontend/dashboard.html`）补齐登录/急救屏与设置区（⓪~⑦）title 属性的中英文对照，统一采用「中文 / English」内联格式，与项目既有 Tab/标题规范对齐；技术字段名（Model/API Key/Base URL/Timeout 等）按约定保持纯英文。

### 测试 / Tests

- 全量 `pytest tests/`：961 passed，38 skipped。
- 本地裸机 Windows 真实起服务（`streamable-http`）验证：Dashboard 首页 200 可打开、真实浏览器走完首次设密/登录流程、主界面正常渲染；12 个 MCP 工具通过 `/mcp` 逐一列出并**全部**真机调用一遍（`hold`/`breath`/`grow`/`trace`/`anchor`/`release`/`pulse`/`plan`/`letter_write`/`letter_read`/`I`/`dream`），核对返回内容与文档描述一致（新记忆可被检索回读；`trace(delete=True)` 仅移入 `archive/`，未物理抹除；`grow(items=...)` 修复后正确逐字建桶）；`tools/evaluate_retrieval.py`、`tools/vnext_preflight.py`、`tools/v3_health_report.py` 均运行通过。
- 验证 Dashboard ③ 引擎的热更新：通过 `/api/env-config` 写入压缩模型 API Key 后，同进程内下一次 `hold` 调用立即使用新 Key 发起请求（服务端日志可见对应出站请求），无需重启进程。
- 本地 Docker 从零 `--no-cache` 构建 + 部署验证；12 个 MCP 工具逐一真机调用核对文档描述；红蓝队核查物理删除红线（`trace(delete=True)` 确认只移入 `archive/`，未物理抹除）、鉴权边界、路径穿越注入，均符合预期。
- 验证镜像内 `docs/` 只含 4 个白名单文件（`docs/secrets`、`docs/superpowers` 确认未进镜像）；`/dream-hook` 端点已移除（404），`/breath-hook` 鉴权正常（401）。

## 2.5.3

### 修复 / Fixed

- 统一解析带 `Z` / UTC offset 的时间字段，避免新导入记忆被误判为旧记忆并异常衰减。
- 修正字符串 `"false"` 在 OAuth、embedding、记忆状态和 LLM 结构化结果中被误当作开启的问题。
- 移除普通写入、导入和编辑路径的重复 embedding 请求，统一由 `BucketManager` 维护向量。
- embedding 热重载会同步更新 Web、MCP、桶管理、导入和完整迁移运行时，避免新旧模型并存。
- 同步两份 Dashboard，并修正 Docker 宿主机挂载提示和动态调试 ID 的安全传递。

### 测试 / Tests

- 新增时间、布尔边界、embedding 单次写入、热重载引用和 Dashboard 一致性回归测试。
- 使用隔离的真实本地服务验证 Dashboard、12 个 MCP 工具、`hold` 落盘、`breath` 读回及 `pulse`。
- 完整测试通过：623 passed，7 skipped。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.5.3。

## 2.5.2

### 修复 / Fixed

- MCP OAuth 补齐 resource binding、反向代理公网地址规范化、PKCE 与 token 续期边界，避免授权页已弹出却无法完成连接。
- `hold` 在打标或 embedding API 不可用时仍原样保存正文；合并只追加原文，绝不调用 LLM 压缩。
- 脱水缓存键加入 API 格式、端点和模型；切换到 Haiku 等新模型后，长桶下次首次浮现会真正调用新模型，不复用旧模型摘要。
- 移除 Dashboard 物理删除入口；旧 `/api/buckets/purge` 改为只读拒绝端点，保留 API 兼容但不会抹除记忆。

### 优化 / Improved

- 收紧 `hold` / `grow` / `trace` 工具描述，要求客户端只在有明确记忆意图时发起写操作，降低模型过度调用。

### 测试 / Tests

- 新增 OAuth 授权码 + PKCE + resource + refresh token 端到端回归，并以真实本地 HTTP 服务验证 401 discovery 链。
- 新增 `hold` 打标/向量降级、原文合并、模型级脱水缓存、OAuth 开关持久化和 purge 禁用回归。
- 完整测试通过：613 passed，7 skipped。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.5.2。

## 2.5.1

### 修复 / Fixed

- Cloudflare Tunnel 在 Compose 部署下默认使用双 `v2` region edge 和 HTTP/2，绕过部分 VPN DNS
  无法解析 `_v2-origintunneld._tcp.argotunnel.com` SRV 记录导致的启动失败。
- 单实例 Compose 统一通过 `OMBRE_HOST_VAULT_DIR` 将宿主机目录 bind mount 到
  `/app/buckets`，并改用兼容 Windows 盘符的长语法；记忆、`config.yaml` 和 Tunnel token
  在 `--force-recreate` 后继续保留。
- 多实例 Compose 支持为每个 owner 单独设置宿主机持久目录，同时保留数据隔离。
- Dashboard 在 Docker 内不再把容器自己的 `.env` 误报为宿主机挂载配置；宿主机目录改为
  Compose 只读状态，并明确提示修改 compose 同目录 `.env` 后重建容器。
- 修正文档和环境变量示例中遗留的 `/data` 路径，统一为 `/app/buckets`。

### 测试 / Tests

- 新增 Compose Tunnel/DNS、Windows bind mount、owner 隔离和 Tunnel token 持久化回归测试。
- 完整测试通过：602 passed，7 skipped。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.5.1。

## 2.4.13

### 修复 / Fixed

- 修复向量 API 被反复重复调用的问题：`trace(content=...)` / `plan()` / `letter_write()` 在
  `bucket_mgr.update()` / `create()` 已经内部同步生成并存好向量之后，又各自显式调用了一次
  `embedding_engine.generate_and_store()`，导致每次写操作都对同一段内容打两次向量 API。
  现在移除了这些多余的显式调用。
- `EmbeddingEngine` 新增进程内小容量 LRU 查询缓存：`breath(query=...)` 内部会对同一个查询串
  各自调用一次向量检索（`bucket_mgr.search()` 内部一次、`surface_search()` 直接又一次），
  `hold()`/`grow()` 的 `merge_or_create` → `check_duplicate_for` → `check_plan_resolution`
  三条 fire-and-forget 链路也会对同一段新内容各嵌入一次。同一段文本对同一模型的向量结果恒定，
  缓存后这些短时间内的重复请求不再重新打向量 API。

### 测试 / Tests

- 现有 `tests/test_embedding_api_regression.py` 等回归测试全部通过，确认门面缓存不影响
  既有向量化行为。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.13。

## 2.4.12

### 优化 / Improved

- `pulse()` 顶部统计现在单独显示 feel / plan / letter 数量，避免列表数量和头部统计看起来对不上。
- `grow()` 短内容走 hold 风格单条保存时会明确提示“没有拆分”，减少短日记归档时的误解。
- Dashboard 保存 `OMBRE_HOST_VAULT_DIR` 后直接提示需要重启容器/服务；API 也返回 `restart_required` 和 `message`。
- Dashboard 将单桶、信件和导入审核删除文案改为“删除到档案”，与清理模式里的物理永久删除明确区分。
- `trace(resolved=1)` 与 REST resolve 共用同一套中文提示，Dashboard 会展示“已沉底/已重新激活”的一致说明。
- `config.example.yaml` 移除已废弃的 active `wikilink:` 配置段，只保留 deprecated 说明。

### 测试 / Tests

- 新增 `tests/test_priority4_confusion_cleanup.py` 覆盖上述高频困惑点的回归。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.12。

## 2.4.11

### 修复 / Fixed

- MCP OAuth 支持 `refresh_token` grant：授权码换 token 时会同时返回 refresh token，headless 服务器环境下 access token 失效后可直接刷新，不再必须重新打开浏览器授权页。
- OAuth discovery 与动态客户端注册现在声明 `refresh_token`，并兼容旧版 `.dashboard_mcp_tokens.json` access token 存储格式。
- 修复 v3 legacy 桥接层缺失的 runtime/web/bucket side-channel API，恢复工具调用、Web 路由注册、更新策略评估和 bucket 生命周期事件的只读旁路记录。

### 测试 / Tests

- 新增 `tests/test_oauth_refresh_token.py` 覆盖 refresh token 元数据声明、授权码换 refresh token、刷新 access token、未知 refresh token 拒绝。
- 修复并恢复 `tests/test_v3_legacy_*` 桥接回归，测试用例显式注入 fake embedding，避免绕开当前“写入必须有向量化”的生产约束。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.11。

## 2.4.10

### 新增 / Added

- GitHub 同步现在会在同一次 commit 中写入 `_ombre_backup_manifest.json`，记录备份生成时间、文件数、总字节数、每个 bucket markdown 的大小和 sha256。
- 从 GitHub 导入/恢复时会读取 manifest 摘要并返回给调用方，后续可用于恢复前校验和备份选择。

### 测试 / Tests

- 新增 `tests/test_github_backup_manifest.py` 覆盖 manifest 生成、同步写入和恢复读回。
- 更新 zero-commit 空仓库同步测试，确认首次提交也包含 manifest。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.10。

## 2.4.9

### 新增 / Added

- Dashboard 历史对话导入新增上传前预检：选中文件后先显示识别格式、轮次、分块数、预计 API 调用、文件大小、首个分块预览和警告，再由用户确认开始导入。
- 新增 `POST /api/import/preflight`，复用导入解析/分块逻辑做只读预检，不写 bucket、不启动后台任务。
- 新增 `preview_import()` 纯函数，便于后续把导入体验继续拆成更明确的预检查项。

### 测试 / Tests

- 新增 `tests/test_import_preflight.py` 覆盖导入预检纯函数和 API 路由。
- 新增 `tests/test_dashboard_import_preflight.py` 覆盖 Dashboard 预检入口。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.9。

## 2.4.8

### 新增 / Added

- Dashboard 设置页新增“系统体检”面板，可一键查看数据目录、记忆桶统计、脱水/打标 LLM、向量化、GitHub 备份、访问控制和运行时状态。
- 新增 `GET /api/system/diagnostics` 只读接口，返回结构化 `ok` / `warning` / `error` 检查项；体检不主动请求外部 API，避免设置页被慢网络卡住。

### 测试 / Tests

- 新增 `tests/test_system_diagnostics.py` 覆盖诊断接口和缺配置告警。
- 新增 `tests/test_dashboard_diagnostics_panel.py` 覆盖 Dashboard 体检入口。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.8。

## 2.4.7

### 修复 / Fixed

- 修复 GitHub 新建空仓库（Zero Commit，首页仍是 Quick setup）首次同步时报 `409 Conflict` 的问题。现在 Ombre 会在空仓库中创建初始 tree/commit，并创建 `refs/heads/<branch>`，无需用户先手动添加 README。
- 从空 GitHub 仓库导入时返回“暂无可导入文件”，不再把空仓库 409 当作异常。

### 测试 / Tests

- 新增 `tests/test_github_sync_zero_commit.py` 覆盖 zero-commit 仓库首次存档 bootstrap 流程。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.7。

## 2.4.6

### 优化 / Improved

- Dashboard 批量导入的 LLM 抽取结果解析改为宽松 JSON 清洗：支持 DeepSeek 等模型在 JSON 数组/对象前后附带说明文字，减少 `Import extraction JSON parse failed`。
- 抽出通用 `clean_llm_json()`，让导入解析与 grow/dehydrator 的 JSON 解析共用同一套 code fence/JSON 片段提取逻辑。

### 测试 / Tests

- 新增 `tests/test_import_extraction_json.py` 覆盖模型回复包含说明文字时的导入解析回归。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.6。

## 2.4.5

### 优化 / Improved

- 新增 LLM / embedding 请求超时配置：`dehydration.timeout_seconds`、`embedding.timeout_seconds`，以及环境变量 `OMBRE_COMPRESS_TIMEOUT_SECONDS`、`OMBRE_EMBED_TIMEOUT_SECONDS`。
- 写记忆时的脱水/打标、原生 Gemini、OpenAI 兼容 embedding 请求都会使用配置的超时时长，方便国内自托管服务器连接云端 API 较慢时调大等待时间。

### 测试 / Tests

- 新增 `tests/test_api_timeout_config.py` 覆盖 config/env 覆盖和运行时对象 timeout 传递。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.5。

## 2.4.4

### 修复 / Fixed

- 允许在 Dashboard 清空或修改 `AI_NAME`，避免关闭 OAuth 后仍显示旧的 AI 显示名；清空后回退为默认 `AI`。
- 统一桶元数据读取层的日期时间序列化，将 `created` / `last_active` 中的 `datetime` / `date` 归一化为 ISO 字符串，避免 `dream()`、Dashboard 首页和导入页面 JSON 序列化报错。
- 版本检查优先通过 GitHub Contents API 读取 `VERSION`，避免 raw CDN 在 push 后继续返回旧版本导致热更新检测不到新版本。

### 测试 / Tests

- 新增 `tests/test_env_config_identity.py` 覆盖 AI 显示名清空回归。
- 新增 `tests/test_datetime_metadata_normalization.py` 覆盖 YAML/frontmatter 时间戳被解析为 `datetime` 后的序列化回归。
- 新增 `tests/test_dashboard_update_source.py` 覆盖 Dashboard 版本检查的 GitHub API 优先顺序。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.4。

## 2.4.0

### 架构 / Architecture

- 将当前高级架构线统一作为对外发布版本 `2.4.0`。
- 保留内部 `src/ombrebrain/` 架构层命名：acceptance、eventsourcing、retrieval、microkernel、plugins、distributed 等模块继续作为内部深内核层存在。
- 保持 MCP tool names、bucket markdown、Dashboard existing routes、config/env 语义不变。

### 修复 / Fixed

- 修复 `tests/test_permanent_breath_regression.py` 中写死 Windows 路径分隔符的断言，改为 `os.sep`，避免 Linux / Docker / CI 下出现跨平台假失败。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.0。
- capability catalog 的 manifest version 改为读取项目版本，避免对外元数据继续暴露旧的架构草案版本号。

## 2.3.22

### 前端 / Frontend

- 写信表单「身份」下拉固定为 `user` / `AI`（对面是 AI 这点不必纠结具体模型名）；
  具体署名由用户在旁边的「署名」框自行填写。
- 写信表单的日期选择改造成拟态化「按钮」：点击主动唤起原生日期选择器（`showPicker()`
  + `focus/click` 兜底），选定后按钮显示所选日期；解决了原生小日历图标与提示文字重叠、
  以及透明输入框点击无响应的问题。
- 「服务日志」页右上角的日志文件路径只显示文件名（如 `server.log`），完整路径移到鼠标
  悬停提示，界面更干净、也不在页面上暴露本机绝对路径。

### 维护 / Chores

- VERSION + `src/VERSION` → 2.3.22。

## 2.3.21

### 新增 / Added

- **letter 署名支持自定义 AI 名称。** `letter_write` 的 `author` 不再限定
  `"user"`/`"claude"`，改为接受任意字符串署名：
  - `"user"` → 用户侧（`user_name` 逻辑不变）；
  - `"ai"`、等于 `ai_name` 的值、或历史遗留的 `"claude"` → 统一存为 `ai_name` 的值；
  - 其它任意字符串 → 原样作为署名。
  新增可选参数 `ai_name`（显式传入优先），默认取环境变量 `AI_NAME`，回退 `"AI"`。
  `letter_read` 原样返回存储的署名、不做转换；按 `author` 过滤时 `"ai"` 会同时
  命中新署名与历史 `"claude"` 信件。Dashboard 写信/筛选、SessionStart 钩子的「最近的信」
  同步适配。（`src/tools/plan/core.py`、`src/web/letters.py`、`src/web/hooks.py`、
  `src/server.py`、`frontend/dashboard.html`；回归测试 `tests/test_letter_author_regression.py`）
- 新增共享 helper `utils.get_ai_name()`：统一从环境变量 `AI_NAME` 读取 AI 显示名（回退 `"AI"`）。
- `.env.example` 新增 `AI_NAME=` 条目及说明。

### 变更 / Changed

- **全局去除面向用户文本与注释中的 "Claude" 硬编码。** 面向用户的文案（OAuth 授权页、
  Dashboard 删除确认/提示、配置项说明）改为中性的 "AI"；代码注释中的 "Claude" 统一改为
  "AI"/"LLM"。保留第三方服务/格式/文件的固有名（如 `Claude Desktop`、`claude.ai`、
  `claude_desktop_config.json`、Claude/ChatGPT 导出格式、Anthropic 模型 ID），以及 letter
  存储层对历史 `"claude"` 署名的向后兼容判断。

### 维护 / Chores

- 同步 bump `src/VERSION`（热更新读取的副本）与根 `VERSION` 至 2.3.21。

## 2.3.20

### 修复 / Fixed

- **`breath(importance_min=N)` 在高重要度桶塞满上限时，刚被 `trace` 降级的桶看似「未刷新」**
  之前 `breath(importance_min=N)` 把所有符合阈值的桶按 importance 降序排，直接截取前 20 条。当 `importance=10` 的桶超过 20 个时，一个刚用 `trace` 从 10 降到 9 的桶会被高分桶挤出列表，看起来像「trace 改了 importance 但 breath 没刷新」。
  现在改为先给每个符合阈值的 importance 档位（10、9…）各预留一条最近更新的桶，再按正常排序填满剩余名额，确保降级后的桶在其档位仍可见。
  （`src/tools/breath/importance.py` `_select_importance_buckets`；回归测试见 `tests/test_trace_importance_regression.py`）

  > 说明：`trace` 写入 importance 后，`breath` 是每次从磁盘实时重读、无缓存，本身不存在「需要额外操作触发刷新」。若 `trace` 降级看似无效，请先确认目标桶不是 `pinned`/`protected`——这类核心桶 importance 被锁定为 10，`trace` 会拒绝降级并返回提示，需先 `trace(bucket_id, pinned=0)` 再调整 importance。

### 维护 / Chores

- 修正 `.gitignore`：`docs/secrets/`（复数）此前未被忽略，补上规则，避免本地密钥/设计稿目录被纳入版本控制。
