# Claude Code 自动召回钩子

让 Claude Code 自动接上 Ombre Brain：开新窗口时接上上一段对话，说到「记得 / 上次」时自动带出相关记忆，
聊久了提醒模型存档。只对 Claude Code 生效，其他客户端不受影响。

## 它会做什么

| 时机 | 做什么 |
|---|---|
| 开新窗口 / `/clear` / 压缩后 | 注入上一段对话最后 10 轮原文；当天第一次开窗再附一次 dream |
| 你每发一句话 | 存下这句原文；每满 30 轮提醒模型考虑用 hold 存档；说到回忆语气词、Them 名册里的人名或面板上加的词时，注入最相关的一条记忆（字面和向量两路都认同才注入） |
| 每轮回答结束 | 存下这一轮的回复（本地实测一次约 60ms） |
| 关闭窗口 | 尽力补存没存上的原文 |

原文存在记忆库的 `.autorecall/` 里，会跟着 Dashboard 的 ZIP 备份一起导出。

## 安装

1. **服务端**：在 Ombre Brain 设置一个 hook token（环境变量 `OMBRE_HOOK_TOKEN`，或 config.yaml 的 `hooks.token`），
   然后在 Dashboard「设置 → 自动召回 Claude Code」里打开开关。
2. **一键安装**：卡片里有现成的命令（地址已经替你填好），复制到装了 Claude Code 的电脑终端里运行，按提示输入 token：

   ```bash
   curl -fsSL <OB地址>/autorecall/client/install.py | python3 - --url <OB地址>
   ```

   Windows 先下载再运行：`curl -o install.py <OB地址>/autorecall/client/install.py`，然后 `python install.py --url <OB地址>`。

   安装脚本会：把 `ob_hook.py` 放进 `~/.claude/hooks/`；备份 `~/.claude/settings.json` 后只增改自己的四个钩子
   （你原有的钩子和设置不动，`settings.json` 不是合法 JSON 时一个字都不改）；把地址和 token 写进 `env`
   （加 `--no-env` 则不写，改在 shell 里 `export OB_URL=… OB_HOOK_TOKEN=…`）；最后连一下 OB，告诉你 token 对不对、开关开没开。
   重复运行是安全的。卸载：`python3 install.py --uninstall`。
3. 重开一个 Claude Code 窗口即可。

**手动安装**：脚本在 `src/ombrebrain/autorecall/client/ob_hook.py`，复制到 `~/.claude/hooks/`，
再把本目录（deploy/claude-code-hooks/）的 `settings.example.json` 合并进 `~/.claude/settings.json`。

## 排查

- 什么都没注入：先看 Dashboard 的开关和「已存 N 条原文」有没有在涨；涨了说明钩子通了，只是这句话没触发召回。
- 想看钩子报错：设 `OB_HOOK_DEBUG=1`，错误会打到 stderr（`claude --debug` 里能看到）。
- 钩子出任何错都会静默退出，不会卡住对话。

## 说明

- 不解析对话记录文件取当轮内容：Claude Code 官方说明那是内部格式、会随版本变化。
  用户原文取 UserPromptSubmit 的 `prompt`，回复取 Stop 的 `last_assistant_message`。
- SessionEnd 默认只有 1.5 秒预算，示例里设了 `timeout: 10`。
- Stop 没有设成后台（`async`）：后台钩子在会话退出时会被 Claude Code 直接杀掉，实测 `claude -p` 下回复一条都存不上。
  改成同步挂，但钩子自己不等：把请求交给本地中继（或一个脱离的子进程）就返回。
- **本地中继** `ob_relay.py`：钩子按需在后台拉起，只听 127.0.0.1、带随机密钥，和 OB 保持一条长连接，闲置 30 分钟自动退出。
  远端 OB 时省掉每次重新握手：实测跨境（RTT≈0.3s）发问钩子 657ms → 275ms，每轮存回复 ≈650ms → 不到 0.1s。
  不想要它就设 `OB_RELAY=0`，每次直连。
- 注入内容单次不超过 10000 字符（Claude Code 的上限），超了会被截断而不是存成文件。
