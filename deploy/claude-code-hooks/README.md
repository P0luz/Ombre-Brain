# Claude Code 自动召回钩子

让 Claude Code 自动接上 Ombre Brain：开新窗口时接上上一段对话，说到「记得 / 上次」时自动带出相关记忆，
聊久了提醒模型存档。只对 Claude Code 生效，其他客户端不受影响。

## 它会做什么

| 时机 | 做什么 |
|---|---|
| 开新窗口 / `/clear` / 压缩后 | 注入上一段对话最后 10 轮原文；当天第一次开窗再附一次 dream |
| 你每发一句话 | 存下这句原文；每满 30 轮提醒模型考虑用 hold 存档；说到回忆语气词或库里的 `[[名字]]` 时注入最相关的一条记忆（字面和向量两路都认同才注入） |
| 每轮回答结束（后台） | 存下这一轮的回复 |
| 关闭窗口 | 尽力补存没存上的原文 |

原文存在记忆库的 `.autorecall/` 里，会跟着 Dashboard 的 ZIP 备份一起导出。

## 安装

1. **服务端**：在 Ombre Brain 设置一个 hook token（环境变量 `OMBRE_HOOK_TOKEN`，或 config.yaml 的 `hooks.token`），
   然后在 Dashboard「设置 → 自动召回 Claude Code」里打开开关。
2. **复制脚本**：

   ```bash
   mkdir -p ~/.claude/hooks && cp deploy/claude-code-hooks/ob_hook.py ~/.claude/hooks/
   ```

3. **挂钩子**：把 `settings.example.json` 的内容合并进 `~/.claude/settings.json`，
   把 `OB_URL` 改成你的 Ombre Brain 地址、`OB_HOOK_TOKEN` 改成第 1 步设的 token。
   不想把 token 写进 settings.json 的话，也可以删掉 `env` 段，改在 shell 里 `export` 这两个变量。
4. 重开一个 Claude Code 窗口即可。

## 排查

- 什么都没注入：先看 Dashboard 的开关和「已存 N 条原文」有没有在涨；涨了说明钩子通了，只是这句话没触发召回。
- 想看钩子报错：设 `OB_HOOK_DEBUG=1`，错误会打到 stderr（`claude --debug` 里能看到）。
- 钩子出任何错都会静默退出，不会卡住对话。

## 说明

- 不解析对话记录文件取当轮内容：Claude Code 官方说明那是内部格式、会随版本变化。
  用户原文取 UserPromptSubmit 的 `prompt`，回复取 Stop 的 `last_assistant_message`。
- SessionEnd 默认只有 1.5 秒预算，示例里设了 `timeout: 10`。
- 注入内容单次不超过 10000 字符（Claude Code 的上限），超了会被截断而不是存成文件。
