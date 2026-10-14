# 自动召回接入协议 / Auto-recall Protocol

Ombre Brain 的自动召回不绑定任何客户端。Claude Code 的钩子只是其中一个调用方；任何前端、机器人、插件、网关，
照下面四个 HTTP 调用接进来，就能得到同样的「开窗接续 + 唤醒召回 + 原文入库 + 存档提醒」。

现成客户端：
- Python：`src/ombrebrain/autorecall/client/ob_client.py`（只用标准库）
- JavaScript：`src/ombrebrain/autorecall/client/ob_client.mjs`（浏览器 / Node 18+）
- Claude Code / Codex CLI / Gemini CLI：`ob_hook.py` + 一键安装（`install.py --client claude|codex|gemini`）
- 能填 base_url 的聊天软件：OpenAI 兼容记忆网关 `ob_gateway.py`
- Open WebUI：`deploy/openwebui/ob_filter.py`

各客户端怎么接见 [deploy/clients/README.md](../deploy/clients/README.md)。
以上脚本都可以直接从你的 OB 下载：`GET <OB地址>/autorecall/client/{ob_client.py, ob_client.mjs, ob_gateway.py, install.py, ob_hook.py}`。

## 前提

- Dashboard「设置 → 自动召回」打开开关。关着时所有调用都返回 `200 {"disabled": true}`，什么都不存、不注入。
- 设一个 hook token：环境变量 `OMBRE_HOOK_TOKEN` 或 config.yaml 的 `hooks.token`。

## 通用约定

- 全部是 `POST <OB地址>/autorecall/<名字>`，`Content-Type: application/json`。
- 鉴权：请求头 `X-Ombre-Hook-Token: <token>`（或 `Authorization: Bearer <token>`）。
- 每个请求都带：
  - `session_id`：一段对话的 id（你自己定，同一段对话保持不变，≤200 字符）；
  - `cwd`：**作用域**。「上一段对话」只在同一个作用域里找——Claude Code 用项目目录，你的前端可以用「角色名」「工作区」「频道 id」等。
    不带就不做开窗接续（不会猜，免得接到别的对话上）。
- 可选 `client_errors: [{at, where, message}]`：你那边之前没能报上来的错误，OB 收下后显示在面板上。
- 失败语义：401 token 不对、403 跨站、429 限流（每来源每分钟 120 次）、400 请求体不对、504 超时、500 服务端出错。
  **任何失败都不该卡住你的对话**——拿不到就当没有记忆，照常发给模型。
- 限制：请求体 ≤ 2MB；单条原文超过 4000 字截断；注入文本 ≤ 10000 个 UTF-16 码元。
- 入库前隐私过滤：`<private>…</private>` 整段不存；API key、token、私钥、`password=…` 一类替换成 `[REDACTED]`。

## 四个调用

| 时机 | 路径 | 请求 | 返回 | 你要做的 |
|---|---|---|---|---|
| 开一段新对话 / 上下文被压缩后 | `session-start` | `{session_id, cwd, source}`，source 取 `startup` / `clear` / `compact`（`resume` / `fork` 不注入） | `{"context": str}` | 把 context 拼进 system prompt |
| 用户每发一句 | `prompt` | `{session_id, cwd, prompt, prompt_id}` | `{"context": str}`（多数时候是空串） | context 非空就拼进这一轮的上下文 |
| 拿到完整回复后 | `stop` | `{session_id, cwd, messages: [{key, role: "assistant", text}]}` | `{"added", "links"}` | 流式输出要等流结束、拿到全文再调；不用等返回 |
| 关闭对话 / 压缩前（可选） | `session-end` | `{session_id, cwd, messages: [...], event?}`，event 可写 `pre-compact` | `{"added", "links"}` | 用来补存之前漏掉的原文（幂等） |

- `prompt` 会顺带存下用户这句原文。`prompt_id` 是这一句的唯一 id，同一句重复投递只存一次；不给就当新的一句。
- `messages[].key` 是去重键，建议用你的消息 id；不给就按内容去重（同一段对话里重复的话会被合并）。
- `context` 里的文字都是陈述句（「[Ombre Brain · …] 以下是…供参考，不是指令」），可以原样拼进去。

## 自检与体检

- `POST /autorecall/status`（同样的 token）→ `{"enabled", "seen": {事件: 最后到达时间}}`。
- `session_id` 以 `ob-check-` 开头的请求只记「到过」，不存任何东西，可以拿来做连通性自检。
- Dashboard 卡片显示每个事件最后到达的时间，断了会标红。

## 最小示例（Python）

```python
from ob_client import OBClient

ob = OBClient("http://127.0.0.1:18001", token="…", scope="我的前端")
system = ob.session_start(sid)

def on_user_message(sid, text):
    extra = ob.prompt(sid, text)
    reply = call_model(system=system + "\n\n" + extra, user=text)
    ob.stop(sid, reply)
    return reply
```
