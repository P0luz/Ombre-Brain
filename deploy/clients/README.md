# 各客户端接入自动召回

先在 Dashboard「设置 → 自动召回」打开开关，并设好 hook token（`OMBRE_HOOK_TOKEN` 或 config.yaml 的 `hooks.token`）。
下面所有接法调用的都是同一套 OB 接口（[docs/AUTORECALL_PROTOCOL.md](../../docs/AUTORECALL_PROTOCOL.md)），存下来的原文、提醒、接续、唤醒召回规则完全一样。

挑哪种：

- 用 Claude Code / Codex CLI / Gemini CLI → 装钩子（下面第 1 节）。
- 用能填「API 地址 / base_url」的聊天软件或自己写的前端（DeepSeek、GPT、Gemini 的 API，Cherry Studio、ChatBox、LobeChat、RikkaHub、Cline……）→ 记忆网关（第 2 节）。
- 用 Open WebUI → Filter（第 3 节），或者网关。
- 自己写代码 → SDK（第 4 节）。

## 支持矩阵：每家验证到什么程度

「验证」分四档，从强到弱：**真机**（在这个客户端上实际跑通存取）> **官方 schema / 源码**（按它仓库里的定义逐字段核对）>
**官方文档**（按文档示例或字段说明）> **开源实现**（照别的能用的项目）。每家的真实输入样例都存在
`tests/fixtures/autorecall_clients/<客户端>/`，`tests/test_autorecall_client_contracts.py` 每次测试都拿它们把钩子真跑一遍；
客户端改版后把新样例放进去，哪里对不上测试会直接红。

- **Claude Code**：真机（2.1.295，四个钩子到达、存取正常）＋官方文档示例。
- **记忆网关（OpenAI 兼容）**：真机（官方 openai Python SDK 流式 / 非流式 ＋ 真 OB）。具体聊天软件（Cherry Studio 等）没有逐个测。
- **Open WebUI**：真机（v0.12.0，Docker，界面与 API 两条路径）。
- **Codex CLI**：官方 schema（openai/codex @ 95273b84fbff，输入和输出都按 schema 校验）＋开源实现（claude-mem）。没有上真机。
- **旧版 Gemini CLI**：官方文档字段说明（gemini-cli @ 42c51822084a）＋开源实现（claude-mem 早期版本）。没有上真机。
- **Antigravity CLI（agy）**：官方内置文档（agy 1.3.5）＋两个开源实现（cli-jaw、not-fade-away）读记录文件的格式。
  **没有上真机**——开发者账号过不了 agy 的资格检查。
- **自制前端（SDK）**：单元测试；协议见 docs/AUTORECALL_PROTOCOL.md。

你在某个客户端上用通了（或者没通），欢迎到 [GitHub issue](https://github.com/P0luz/Ombre-Brain/issues) 说一声，附上客户端版本，我们把它升到「真机」。

不想被记的项目：在面板「设置 → 自动召回 → 排除目录」里填上它的目录，里面的对话不存原文、不接续、不召回。

## 1. 命令行客户端：钩子

| 客户端 | 一键安装 | 挂了哪些事件 |
|---|---|---|
| Claude Code | `curl -fsSL <OB地址>/autorecall/client/install.py \| python3 - --url <OB地址>` | 开窗、提问、存回复、关窗、压缩前 |
| Codex CLI | 同上，末尾加 `--client codex` | SessionStart、UserPromptSubmit、Stop |
| Gemini CLI（旧版 `gemini` 命令） | 同上，末尾加 `--client gemini` | SessionStart、BeforeAgent、AfterAgent |
| Antigravity CLI（`agy`，**实验**） | 同上，末尾加 `--client antigravity` | SessionStart（接续）；PreInvocation、Stop 只跑结构探针 |

- Codex：装完要打开 codex 输入 `/hooks`，把 ob_hook.py 的几条逐个**信任**一次，否则不会执行（Codex 的安全设计，改过的钩子也要重新信任）。
- Codex / Gemini 的配置里没有 env 段，OB 地址和 token 写在钩子旁边的 `ob_hook.env`（只有自己可读）。
- Codex / Gemini 没挂「关窗 / 压缩前补存」：那两个钩子要读对话记录文件，目前只认得 Claude Code 的格式。每一轮的原文照常存。
- **Antigravity 是实验性的，我们没能实测**（开发者的账号过不了 agy 的资格检查）：
  - agy 的钩子输入里没有正文，只有记录文件路径。原文从记录文件
    `~/.gemini/antigravity-cli/brain/<会话>/.system_generated/logs/transcript.jsonl` 里读：
    `USER_INPUT` 是用户原话（取 `<USER_REQUEST>` 里的部分），`PLANNER_RESPONSE` 里正文非空、没有工具调用的那条是最终回复。
    这个格式参照两个能用的开源项目（lidge-ai/cli-jaw、heyxiaoc/not-fade-away），agy 改版后可能失效。
  - SessionStart 注入接续 / dream；PreInvocation 只在一轮的第一次调用时存原话、唤醒召回；Stop（正常结束）存回复并补存原话。
  - 用过请到 [GitHub issue](https://github.com/P0luz/Ombre-Brain/issues) 说说行不行。**没存上时的排查**：在
    `~/.gemini/config/hooks.json` 的 `ombre-brain` → `Stop` 里加一条
    `{"type": "command", "command": "python3 ~/.gemini/config/ombre-brain/ob_hook.py --client antigravity probe Stop"}`，
    再聊一句，把 `~/.gemini/config/ombre-brain/ob_agy_probe.json` 贴进 issue——它只记字段名、类型和 type 这类枚举值，**不记聊天内容**。
- 自检：`python3 ~/.codex/hooks/install.py --client codex --check`（Gemini 把 codex 换成 gemini、目录换成 `~/.gemini/hooks`）。
- 卸载：安装命令加 `--uninstall`。原配置先备份，只删自己的条目。

## 2. 记忆网关（OpenAI 兼容）

一个本地小代理，夹在聊天软件和模型商中间：

```bash
curl -fsSLO <OB地址>/autorecall/client/ob_gateway.py
OB_HOOK_TOKEN=你的token python3 ob_gateway.py --upstream https://api.deepseek.com/v1 --ob <OB地址>
```

然后在聊天软件里把 API 地址改成 `http://127.0.0.1:18080/v1`，API key 照填模型商的（网关原样转交，不落盘）。

- `--upstream` 写到版本段为止：OpenAI `https://api.openai.com/v1`、DeepSeek `https://api.deepseek.com/v1`、
  Gemini `https://generativelanguage.googleapis.com/v1beta/openai`。其他兼容 OpenAI 的服务同理。
- 流式回复一边转发一边拼全文，结束后存进 OB；中途点了停止的那一轮不存。工具调用轮不存，图片等非文本部分不存。
- 会话怎么认：网关按「作用域 + 第一条 system + 第一句用户发言」认同一段对话，只有一句用户发言的请求视为新对话（取接续）。
  前端能传请求头的话，用 `X-OB-Session: <你的对话 id>` 更准。
- 不同用途用不同作用域：`--scope 工作`、`--scope 角色A`，「上一段对话」只在同一个作用域里找。
- 要放在服务器上给手机用：必须加 `--key 网关密钥`（客户端用它当 API key），模型商的 key 交给 `--upstream-key`。
  不设 key 时网关只肯监听本机——网关会把记忆注入回复，谁能连上它谁就能读到记忆。
- OB 不通或超时，照常转发、不带记忆，对话不受影响。

## 3. Open WebUI：Filter

管理员面板 → Functions → `+`，把 [deploy/openwebui/ob_filter.py](../openwebui/ob_filter.py) 整段贴进去保存，
打开开关、设为 Global，再在它的 Valves 里填：

- `ob_url`：OB 地址。Open WebUI 跑在 Docker 里、OB 在宿主机时填 `http://host.docker.internal:18001`；
- `hook_token`；
- `scope_per_user`（默认开）：多用户时每个人的「上一段对话」分开。

Open WebUI 自己发起的后台任务（生成标题、标签、追问建议）不存也不召回。

## 4. 自己写前端：SDK

- Python：`<OB地址>/autorecall/client/ob_client.py`（只用标准库）
- JavaScript：`<OB地址>/autorecall/client/ob_client.mjs`（浏览器 / Node 18+）

用法见文件开头的注释和协议文档。在浏览器里直接调 OB 会把 hook token 暴露给页面，只适合自己用的前端；对外的前端请让后端去调。

## 5. 其他客户端

- **Cursor**：它的钩子不能在提问时注入上下文，开窗注入也是不等结果的，做不到和上面一样的效果。
  用自己的 API key 时，可以在模型设置里覆盖 OpenAI Base URL 指向网关（第 2 节）；据我们所知 Cursor 的请求从它的服务器发出，
  网关要公网可达，因此必须加 `--key`。（未实测）
- **Cline**：选「OpenAI Compatible」提供商，Base URL 填网关地址。它自己的钩子拿不到回复全文。（未实测）
- **只支持 MCP 的客户端**（Claude 桌面版 / 网页版、Cherry Studio 的 MCP 等）：MCP 协议里没有「每轮自动」这回事，
  只能靠模型自己调用 OB 的 breath / breath_search。客户端能填自定义 API 地址的，改用网关。
- **SillyTavern**：暂不支持。酒馆里的角色和对话是虚构的，不等于同一个人的连续记忆，按对话自动存取容易把不同故事混在一起。
