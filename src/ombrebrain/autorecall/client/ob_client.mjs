// Ombre Brain 自动召回的最小 JavaScript 客户端（浏览器 / Node 18+ 都能用，只依赖 fetch）。
//
//   import { OBClient } from "./ob_client.mjs";
//   const ob = new OBClient("http://127.0.0.1:18001", { token: "你的 hook token", scope: "我的前端" });
//
//   const start = await ob.sessionStart(sessionId);           // 开新对话：拼进 system prompt
//   const ctx = await ob.prompt(sessionId, userText);         // 每句用户输入：要注入的记忆（可能为空）
//   const reply = await callYourModel({ system: start + ctx, user: userText });
//   await ob.stop(sessionId, reply);                          // 流式就等流结束、拿到全文后再调
//   await ob.sessionEnd(sessionId);                           // 关闭对话（可选）
//
// 协议见 docs/AUTORECALL_PROTOCOL.md。网络错误一律返回空，不抛异常：记忆出问题不能卡住对话。
// 浏览器里直接调用时，hook token 会暴露给页面——只在你自己的、不对外的前端里这么做，
// 对外的前端请让自己的后端去调 OB。

export class OBClient {
  constructor(baseUrl, { token, scope = "", timeoutMs = 12000, client = "sdk" } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.token = token;
    this.scope = scope; // 对应协议里的 cwd：「上一段对话」只在同一个 scope 里找
    this.timeoutMs = timeoutMs;
    this.client = client; // 面板上按客户端分开关；自制前端都算 sdk
    this.lastError = "";
  }

  async #post(path, payload, timeoutMs = this.timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${this.baseUrl}/autorecall/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Ombre-Hook-Token": this.token },
        body: JSON.stringify({ ...payload, cwd: this.scope, client: this.client }),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`OB 返回 ${res.status}`);
      this.lastError = "";
      return (await res.json()) || {};
    } catch (err) {
      this.lastError = String(err && err.message ? err.message : err);
      return {};
    } finally {
      clearTimeout(timer);
    }
  }

  async sessionStart(sessionId, source = "startup") {
    return (await this.#post("session-start", { session_id: sessionId, source }, 45000)).context || "";
  }

  async prompt(sessionId, text, promptId = crypto.randomUUID()) {
    return (await this.#post("prompt", { session_id: sessionId, prompt: text, prompt_id: promptId })).context || "";
  }

  async stop(sessionId, reply, promptId = crypto.randomUUID()) {
    return this.#post("stop", {
      session_id: sessionId,
      messages: [{ key: `a:${promptId}`, role: "assistant", text: reply }],
    }, 25000);
  }

  async sessionEnd(sessionId, messages = []) {
    return this.#post("session-end", { session_id: sessionId, messages }, 30000);
  }
}
