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
- 建议带 `client`：你是哪家客户端，取 `claude` / `codex` / `gemini` / `antigravity` / `gateway` / `openwebui` / `sdk`
  （不认识的名字按 `sdk` 算，不带按 `claude` 算）。Dashboard 上每家有单独的开关和「最后到达」；
  关掉的那家发来的请求返回 `200 {"disabled": true, "client": …}`，只记到达，不存不注入。
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

- `POST /autorecall/status`（同样的 token）→ `{"enabled", "seen": {事件: 最后到达时间}, "injection_protocol": 2}`。
- `session_id` 以 `ob-check-` 开头的请求只记「到过」，不存任何东西，可以拿来做连通性自检。
- Dashboard 卡片显示每个事件最后到达的时间，断了会标红。

## 4.1：注入快照与上下文去重

此扩展借鉴巴别塔的历史快照和窗口覆盖机制，不改变 OB 的记忆真源、检索排序、MCP 工具或遗忘规则。
不带 `injection` 的旧调用仍按上面的四接口工作；新版网关先查询 status，旧服务端仍走旧模式。

### 网关模式

`prompt` 额外带 `injection: {version: 2, mode: "gateway", history, roles, policy}`：

- `history`：每条非 system/developer 消息的 SHA-256（规范 JSON：键排序、UTF-8、无额外空白）；包括工具调用和结果，最多 2000 条。正文和图片不随指纹发送。
- `roles`：与 history 等长，取 user/assistant/tool。最后是 user 时携带当前 `prompt` 和 `prompt_id`；否则只重放，不新增召回或原文。
- `policy`：所有 system/developer 消息及位置的同算法摘要。
- 返回 `protocol: 2, context, receipt_id?, placements: [{index, context, receipt_id}]`。index 指向 history 的位置；网关只在该 user 消息前加参考块，不改 system，不裁剪客户端历史。

完整前缀一致时重放原快照；客户端裁掉开头时，只允许唯一匹配的历史后缀重放。最新 user 不靠后缀猜重试，多分支重复文本有歧义时不恢复该历史位置。
窗口内已重放的同版本记忆不再新增；退出窗口可重新召回。正文更新形成新版本，旧历史不改写。重试、服务端和网关重启均复用保留范围内的快照。
请用 `X-OB-Session` 提供稳定且各会话独立的 ID；没有它只能按 system 和第一句推断，相同开头、裁剪首句或修改 system 时不能保证会话识别。该头不转发给模型商。

首轮接续、dream、提醒和召回共用 `autorecall.max_chars`，沿用 UTF-16 计数（默认 9000，上限 10000），不是精确 token 预算。预算只限制本轮新增内容，已在历史里的快照不重新截断。首轮服务端沿用 session-start 超时，其他轮沿用 prompt 超时。

### MCP + hook 模式

Claude Code、Codex CLI、旧 Gemini CLI 的新版脚本在 session-start/prompt 带：
`injection: {version: 2, mode: "hook", epoch, request_id, delivered: [receipt_id]}`。

- epoch 是上下文阶段；startup/clear/compact 更换它，resume 保留。request_id 使用本轮 ID；没有 ID 的客户端仍用随机键，不能保证无 ID 事件的重投幂等。
- 只有成功输出到 stdout 后，脚本才保存回执；下一次请求带回它。服务端准备结果不等于已交付，未带回执的快照不会压制另一轮召回。
- 本机回执仅包含阶段与 ID，放在现有 hook 状态目录下的 `injections/`，0600、原子替换，最多 1000 份，每份最多 128 个 ID；不保存正文。按服务地址、凭据摘要、客户端、目录和会话隔离。
- 4.1.2 起，目录内的 `.delivery.lock` 保护回执读改写和阶段重置，最多等待 1 秒，不跨网络调用持锁；进程退出由操作系统释放。锁文件保留以避免并发进程锁住不同文件；普通 prompt 不再重写整份回执。锁超时会记录错误并按现有失败路径降级，不能保证该次回执入账。
- 回执只能证明脚本输出过；当前适配器不能确认宿主仍保留哪些消息。没有收到压缩事件、宿主另行裁剪或回滚历史时，不能提供精确窗口去重保证。
- MCP 的 breath/breath_search 仍可主动重新读取，不受自动覆盖抑制。本轮不根据 token、工作目录或 MCP 连接猜测跨入口会话绑定。
- Antigravity、Open WebUI Filter、通用 SDK 暂保留原协议，不宣称具备 v2 快照能力。

同一段对话只指定一个自动入口：若走网关，在 Dashboard 关闭对应客户端的自动召回开关；若走 hook，则不再经过记忆网关。关闭客户端自动召回不关闭 MCP。

### 持久化、失败与保留范围

快照在现有 `.autorecall/transcript.sqlite3` 的 module_state 表保存，含实际注入正文、版本引用和历史指纹；旧备份可读，新备份沿用原模块校验。按客户端、作用域、会话、模式及阶段隔离。
4.1.2 起，每份分别保留最多 128 条有正文的快照和 128 条空结果重试记录，共用约 2 MiB 上限；容量不足先淘汰空重试，再淘汰最早的正文。空重试只存请求身份、历史位置和策略指纹，不重复保存空正文与空引用。超过空重试保留范围的旧请求不保证仍返回原空结果。
落盘历史采用共享前缀的节点表，读取时还原原有历史路径，分支和裁剪匹配规则不变。旧编码直接可读，下次保存时转换；已被旧版本淘汰的快照无法恢复。新编码的派生状态需由 4.1.2 或更新版本读取，降级前须保留兼容备份。
整个注入状态仍最多 1000 份或 64 MiB（逻辑内容大小）；只淘汰派生状态，不删除原文和记忆。超出正文保留范围不保证旧快照重放，缺失覆盖时允许按相关性再次召回。
快照写盘成功后才返回；并发提交比较版本，冲突返回失败，客户端可重试。OB 失联时网关继续转发原请求，这一轮可能缺少历史注入；不把这种降级称为缓存稳定。

4.1.1 起，进程内按上述注入作用域串行准备快照，不同作用域的慢召回互不占用同一把锁；闲置锁自动回收。
网关首轮及 hook 开窗请求记录自己领取的 dream 额度：快照写盘失败或请求取消且未保存时归还，成功保存后保留，重试读取原快照。取消协程不会停止 SQLite 线程，因此取消返回前须等待已经启动的额度或快照写入完成，再决定是否归还；不会释放其他请求领取的额度。
此恢复保证覆盖可处理的异常和协程取消，不覆盖进程被强制终止或额度归还本身持续写盘失败；数据库格式保持不变。

验证入口：`tests/test_autorecall_service.py`、`tests/test_autorecall_gateway.py`、`tests/test_autorecall_endpoints.py`、`tests/test_autorecall_hook_script.py`、`tests/test_autorecall_client_contracts.py`。
本机可执行 `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/bin/pytest tests/test_autorecall*.py -q --tb=short`。测试使用临时库和模型/向量替身；真实供应商缓存命中率与客户端压缩行为须另行实测。

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
