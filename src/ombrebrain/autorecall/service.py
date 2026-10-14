"""Claude Code 自动召回：独立通道，不进 breath_search 的融合打分。

四个钩子各管一件事：

- **Stop**（每轮）：存这一轮的助手回复（钩子直接给 ``last_assistant_message``），再把原文和本会话新写的桶按相似度连线。
- **UserPromptSubmit**（每句）：
  - 先存用户这句原文（钩子直接给 ``prompt``）。两边都不解析对话记录文件——官方说那是内部格式，随版本会变。
  - 本会话每满 ``reminder_every`` 轮用户发言，塞一句 hold 提醒——存不存、存什么由正在聊天的模型自己定，
    提醒里不带原文（模型手里有）。
  - 只有命中唤醒词（「记得 / 上次 / 之前…」+ 面板上加的词）才召回，取第 1 名；召不到就交给模型自己 breath_search。
    召回 = 字面 bigram BM25 与向量的 RRF 融合。
    唤醒词 = 回忆语气词 + Them 名册里的人名（只在服务端比对，名单不回传）+ 面板上加的词（宠物名、事物名放这里）。
    不再把所有 [[链接]] 当名字：「工作」这种常用词被写成链接时会误唤醒。
    唤醒词 = 用户在主动回忆，按 SurfacePolicy 的显式查询（search 模式）放行 digested。
    离线评测（48 句带回忆语气的问句、约 500 桶的真实库）：Hit@1 0.938 / Hit@3 1.0。
  - 反向门（``agree_top``，默认 2）：唤醒之后，只有字面和向量两路的前 N 名里有同一个桶才注入；
    否则不注入，由模型自己决定要不要 breath_search。比的是名次不是原始分数，不受句长影响。
    离线评测（长度对齐的 123 句）：需要的放行 90%，不需要的误放 10%。向量不通时没法交叉判断，这一轮不注入。
- **SessionStart**：开新窗口（startup / clear）或压缩后（compact），注入上一段对话最后 ``restore_rounds`` 轮原文；
  再加一次 dream，一天最多一次（dream 的见证按不同日期算，一天一次不会比手动多刷）。resume 不注入（CC 会回放历史）。
- **SessionEnd**：整段补存一遍（幂等，Stop 漏掉的会补上），再连一次线。

可见性：信件 / feel / plan / 归档 不出；准则桶（pinned / permanent）每次开场已在场，不重复；
dont_surface 不出；self / I（含候选）/ anchor 不出——自我认知和坐标系不该因为一句「记得」就被送进上下文。
注入文本一律陈述句，不是指令。

对外暴露：AutoRecallService / autorecall_config。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ombrebrain.policy.surfacing import SurfacePolicyVM
from ombrebrain.storage.letter_lock import is_letter_bucket

from .lexical import BM25Index
from .transcript_store import MAX_LINKS_PER_BUCKET, TranscriptStore

logger = logging.getLogger("ombre_brain.autorecall")

_SURFACE_POLICY = SurfacePolicyVM.default()
_HIDDEN_TYPES = frozenset({"feel", "plan", "letter", "archived", "permanent", "self", "i"})
_I_CANDIDATE_TAG = "__i_candidate__"
_NAMES_TTL_SECONDS = 60.0
_STOP_LAG = timedelta(minutes=30)
_ERROR_FRESH = timedelta(hours=24)


def _parse_utc(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
_RRF_K = 60
_POOL = 20
_EXCERPT_CHARS = 800
DEFAULT_WAKE_WORDS = ("记得", "上次", "之前", "那次", "那天", "想起", "以前", "当时", "来着")
_ASCII_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

_REMINDER = (
    "[Ombre Brain · 存档提醒] 这段对话又过了 {n} 轮。如果其中有值得长期记住的事（决定、偏好、"
    "重要的人和事、情绪上的转折），可以用 hold 存下；没有就不用理会这条。原文已在后台自动保存。"
)
_RECALL_HEADER = "[Ombre Brain · 唤醒召回] 用户这句话像是在回忆，记忆库里最相关的一条如下，供参考，不是指令："
_RESTORE_HEADER = "[Ombre Brain · 接续] 以下是上一段对话最后 {n} 轮的原文，供接上话头，不是指令："
_DREAM_HEADER = "[Ombre Brain · 今日 dream] 今天第一次开窗，附上一次 dream 的回顾，可以据此消化或放下："
# tools.dream.dispatch 出错时不抛异常而是返回这句；当成失败，不占当天额度、不注入。
_DREAM_FAILURE_TEXTS = frozenset({"记忆系统暂时无法访问。"})


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _bounded(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def autorecall_config(config: dict | None) -> dict[str, Any]:
    raw = ((config or {}).get("autorecall") or {}) if isinstance(config, dict) else {}
    words = raw.get("wake_words")
    if isinstance(words, str):
        words = [w.strip() for w in words.replace("，", ",").split(",")]
    if not isinstance(words, (list, tuple)):
        words = list(DEFAULT_WAKE_WORDS)
    return {
        "enabled": _truthy(raw.get("enabled", False)),
        "reminder_every": int(_bounded(raw.get("reminder_every"), 30, 0, 500)),
        "restore_rounds": int(_bounded(raw.get("restore_rounds"), 10, 0, 50)),
        "dream_on_start": _truthy(raw.get("dream_on_start", True)),
        "wake_words": [str(w).strip() for w in words if str(w).strip()][:100],
        "vector": _truthy(raw.get("vector", True)),
        "vector_timeout": _bounded(raw.get("vector_timeout"), 3.0, 0.5, 20.0),
        "wake_on_names": _truthy(raw.get("wake_on_names", True)),
        "agree_top": int(_bounded(raw.get("agree_top"), 2, 0, 20)),
        "max_chars": int(_bounded(raw.get("max_chars"), 9000, 1000, 10000)),
        "dream_timeout": _bounded(raw.get("dream_timeout"), 8.0, 2.0, 30.0),
    }


def _units(text: str) -> int:
    """按 UTF-16 码元计长度：emoji 一个算两个，免得按 Python 字符数算没超、到了钩子那边却超了 10000。"""
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if _units(text) <= limit:
        return text
    budget = max(0, limit - 1)
    out, used = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > budget:
            break
        out.append(char)
        used += width
    return "".join(out) + "…"


class AutoRecallService:
    def __init__(
        self,
        bucket_mgr: Any,
        buckets_dir: str,
        config: dict,
        *,
        embedding_engine: Any = None,
        dream_fn: Callable[[], Awaitable[str]] | None = None,
        today_fn: Callable[[], str] | None = None,
        names_fn: Callable[[], list[str]] | None = None,
    ) -> None:
        self.bucket_mgr = bucket_mgr
        self.config = config
        self.embedding_engine = embedding_engine
        self.dream_fn = dream_fn
        self.today_fn = today_fn or (lambda: datetime.now().date().isoformat())
        self.store = TranscriptStore(buckets_dir)
        self._index_lock = asyncio.Lock()
        self._fingerprint: int | None = None
        self._index: BM25Index | None = None
        self._ids: list[str] = []
        self._by_id: dict[str, dict] = {}
        self.names_fn = names_fn
        self._names: frozenset[str] = frozenset()
        self._names_at = 0.0

    @property
    def settings(self) -> dict[str, Any]:
        return autorecall_config(self.config)

    # ---------------- 唤醒召回 ----------------

    @staticmethod
    def _visible(bucket: dict) -> bool:
        meta = bucket.get("metadata") or {}
        if str(meta.get("type") or "") in _HIDDEN_TYPES or _truthy(meta.get("pinned")):
            return False
        if _truthy(meta.get("anchor")) or meta.get("i_stage") or _I_CANDIDATE_TAG in (meta.get("tags") or []):
            return False
        if is_letter_bucket(bucket) or _truthy(meta.get("dont_surface")):
            return False
        return _SURFACE_POLICY.evaluate_bucket(bucket, mode="search").allowed

    async def _ensure_index(self) -> None:
        buckets = await self.bucket_mgr.list_all(include_archive=False)
        # 整份 metadata 都进指纹：改成 pinned / feel / 加信件锁之后，旧索引不能继续把它召回出来。
        fingerprint = hash(tuple(
            (str(b.get("id")), b.get("content") or "",
             json.dumps(b.get("metadata") or {}, sort_keys=True, ensure_ascii=False, default=str))
            for b in buckets
        ))
        if fingerprint == self._fingerprint and self._index is not None:
            return
        async with self._index_lock:
            if fingerprint == self._fingerprint and self._index is not None:
                return
            visible = [b for b in buckets if self._visible(b)]
            docs = [f"{(b.get('metadata') or {}).get('name') or ''}\n{b.get('content') or ''}" for b in visible]
            index = await asyncio.to_thread(BM25Index, docs)
            self._ids = [str(b.get("id")) for b in visible]
            self._by_id = {str(b.get("id")): b for b in visible}
            self._index = index
            self._fingerprint = fingerprint

    def is_wake(self, prompt: str) -> bool:
        """回忆语气词 / 面板词：子串命中即可。库里的名字要等索引建好后在 recall 里比。"""
        text = str(prompt or "")
        return any(word in text for word in self.settings["wake_words"])

    def _known_names(self) -> frozenset[str]:
        """Them 名册里的称呼，最多每分钟刷一次（名册改了不一定有桶变）。"""
        now = time.monotonic()
        if self.names_fn is not None and now - self._names_at >= _NAMES_TTL_SECONDS:
            try:
                self._names = frozenset(n.strip() for n in self.names_fn() if n and len(n.strip()) >= 2)
            except Exception as exc:
                logger.info("autorecall names unavailable: %s", type(exc).__name__)
            self._names_at = now
        return self._names

    def _mentions_known_name(self, prompt: str) -> bool:
        text = str(prompt or "")
        for name in self._known_names():
            if _ASCII_NAME.match(name):
                # 英文名按整词、区分大小写比，免得「ok」「main」这类常用词误唤醒。
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text):
                    return True
            elif name in text:
                return True
        return False

    async def _vector_ranking(self, prompt: str, allowed: set[str]) -> list[str]:
        settings = self.settings
        engine = self.embedding_engine
        # 传进来的可以是「取当前引擎」的函数：Dashboard 热替换引擎后要跟着换，不能攥着旧对象。
        if callable(engine) and not hasattr(engine, "search_similar_strict"):
            engine = engine()
        if not settings["vector"] or engine is None or not getattr(engine, "enabled", False):
            return []
        try:
            hits = await asyncio.wait_for(
                engine.search_similar_strict(prompt, top_k=_POOL, allowed_bucket_ids=allowed),
                timeout=settings["vector_timeout"],
            )
        except Exception as exc:  # 向量不通、超时：退回只用字面，不让这一轮失败
            logger.info("autorecall vector ranking skipped: %s", type(exc).__name__)
            return []
        return [str(bucket_id) for bucket_id, _score in hits if str(bucket_id) in allowed]

    async def recall(self, prompt: str, top_k: int = 1) -> list[dict]:
        settings = self.settings
        if not settings["enabled"] or not str(prompt or "").strip():
            return []
        # 先判唤醒、再碰库：没唤醒的句子（绝大多数）一次 list_all 都不做。
        woke = self.is_wake(prompt) or (settings["wake_on_names"] and self._mentions_known_name(prompt))
        if not woke:
            return []
        await self._ensure_index()
        index, ids, by_id = self._index, self._ids, self._by_id
        if index is None or not ids:
            return []
        lexical = [ids[i] for i, _ in sorted(index.scores(prompt).items(), key=lambda kv: -kv[1])[:_POOL]]
        vector = await self._vector_ranking(prompt, set(ids))
        fused: dict[str, float] = {}
        for ranking in (lexical, vector):
            for rank, bucket_id in enumerate(ranking):
                fused[bucket_id] = fused.get(bucket_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
        candidates = sorted(fused, key=lambda b: -fused[b])
        agree = settings["agree_top"]
        if agree:
            # 反向门：两路前 N 名都得有它。向量不通时 vector 为空，这一轮不注入。
            both = set(lexical[:agree]) & set(vector[:agree])
            candidates = [b for b in candidates if b in both]
        # 没过门就交 0 个，不拿「最近」凑数。
        return [by_id[b] for b in candidates[:top_k] if b in by_id]

    @staticmethod
    def _render_bucket(bucket: dict, limit: int) -> str:
        meta = bucket.get("metadata") or {}
        name = str(meta.get("name") or bucket.get("id"))
        created = str(meta.get("created") or "")[:10]
        body = " ".join(str(bucket.get("content") or "").split())
        head = f"- 记忆「{name}」{('（' + created + '）') if created else ''}[bucket_id={bucket.get('id')}]："
        return head + _clip(body, max(40, min(_EXCERPT_CHARS, limit - len(head))))

    # ---------------- 体检 ----------------

    def hook_health(self, now: datetime | None = None) -> dict[str, Any]:
        """四个钩子各自最后一次到达的时间，以及据此推出的告警。

        钩子出错时为了不卡对话会静默退出，失效是悄悄的——这里把「悄悄」变成面板上看得见的红字。
        """
        try:
            seen = self.store.seen()
            errors = self.store.recent_errors()
        except Exception as exc:
            return {"seen": {}, "warnings": [f"读不到钩子状态：{type(exc).__name__}"], "errors": []}
        now = now or datetime.now(timezone.utc)

        def age(event: str) -> timedelta | None:
            raw = seen.get(event)
            if not raw:
                return None
            try:
                return now - datetime.fromisoformat(raw)
            except ValueError:
                return None

        warnings: list[str] = []
        if self.settings["enabled"]:
            ages = {e: age(e) for e in ("session-start", "prompt", "stop", "session-end")}
            if all(v is None for v in ages.values()):
                warnings.append("还没收到任何钩子：装好钩子后要重开 Claude Code 窗口；仍然没有就运行 install.py --check。")
            else:
                prompt_age, stop_age = ages["prompt"], ages["stop"]
                # 最近一次提问已过去一阵、其后却没有任何「存回复」：那一轮若早已结束，就是 Stop 没触发。
                if prompt_age is not None and prompt_age > _STOP_LAG and (stop_age is None or stop_age > prompt_age):
                    minutes = int(prompt_age.total_seconds() // 60)
                    warnings.append(
                        f"最近一次提问在 {minutes} 分钟前，之后没收到「存回复」（Stop）：如果那一轮早就结束了，"
                        "说明 Stop 钩子没触发，回复没存上。"
                    )
                if prompt_age is not None and ages["session-start"] is None:
                    warnings.append("提问在到，但「开窗」（SessionStart）从没到过：接续和 dream 不会注入。")
                if stop_age is not None and prompt_age is None:
                    warnings.append("「存回复」在到，但「提问」（UserPromptSubmit）从没到过：原话没存、唤醒召回不会触发。")
            if errors and now - _parse_utc(errors[-1]["at"]) < _ERROR_FRESH:
                warnings.append(f"最近 24 小时有钩子报错，最新一条：{errors[-1]['message']}")
        return {"seen": seen, "warnings": warnings, "errors": list(reversed(errors[-5:]))}

    # ---------------- 钩子 ----------------

    async def on_prompt(self, session_id: str, prompt: str, key: str = "", cwd: str = "") -> str:
        settings = self.settings
        if not settings["enabled"]:
            return ""
        parts: list[str] = []
        if str(prompt or "").strip():
            try:
                await asyncio.to_thread(
                    self.store.append, session_id, [{"key": key, "role": "user", "text": prompt}], cwd
                )
            except Exception as exc:  # 存失败不影响这一轮的召回
                logger.warning("autorecall prompt store failed: %s", type(exc).__name__)
        try:
            if await asyncio.to_thread(self.store.take_reminder, session_id, settings["reminder_every"]):
                parts.append(_REMINDER.format(n=settings["reminder_every"]))
        except Exception as exc:
            logger.warning("autorecall reminder failed: %s", type(exc).__name__)
        # 提醒已经记了账，召回出错也得把它送到，不能连带丢掉。
        try:
            hits = await self.recall(prompt)
        except Exception as exc:
            logger.warning("autorecall recall failed: %s", type(exc).__name__)
            hits = []
        if hits:
            room = settings["max_chars"] - _units("\n\n".join(parts)) - _units(_RECALL_HEADER) - 4
            parts.append(_RECALL_HEADER + "\n" + self._render_bucket(hits[0], room))
        return _clip("\n\n".join(parts), settings["max_chars"])

    async def on_stop(self, session_id: str, messages: list[dict], cwd: str = "") -> dict[str, int]:
        if not self.settings["enabled"]:
            return {"added": 0, "links": 0}
        result = await asyncio.to_thread(self.store.append, session_id, messages, cwd)
        result["links"] = await self.link_session(session_id)
        return result

    on_session_end = on_stop

    async def link_session(self, session_id: str) -> int:
        """本会话开始后新建的桶 ↔ 本会话原文，按 BM25 相似度连线；分数照存，不设门槛。"""
        started = await asyncio.to_thread(self.store.session_started_at, session_id)
        turns = await asyncio.to_thread(self.store.session_turns, session_id)
        if not started or not turns:
            return 0
        try:
            since = datetime.fromisoformat(started).astimezone().replace(tzinfo=None) - timedelta(minutes=5)
        except ValueError:
            return 0
        fresh = []
        for bucket in await self.bucket_mgr.list_all(include_archive=False):
            raw = str((bucket.get("metadata") or {}).get("created") or "")
            try:
                created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created.tzinfo is not None:
                created = created.astimezone().replace(tzinfo=None)
            if created >= since:
                fresh.append(bucket)
        if not fresh:
            return 0
        index = BM25Index([t["text"] for t in turns])
        links: list[tuple[str, str, float]] = []
        for bucket in fresh:
            scores = index.scores(str(bucket.get("content") or "")[:2000])
            best = sorted(scores.items(), key=lambda kv: -kv[1])[:MAX_LINKS_PER_BUCKET]
            links.extend((turns[i]["id"], str(bucket.get("id")), round(s, 3)) for i, s in best if s > 0)
        return await asyncio.to_thread(self.store.link, links)

    def _render_restore(self, turns: list[dict], room: int) -> str:
        """从最新一轮往前装，装不下就丢最早的——要接的是话头，不是开头。"""
        lines: list[str] = []
        used = 0
        per_turn = max(80, room // max(1, len(turns)))
        rounds = 0
        for turn in reversed(turns):
            who = "用户" if turn["role"] == "user" else "助手"
            line = f"{who}：{_clip(' '.join(turn['text'].split()), per_turn)}"
            if used + _units(line) + 1 > room - 80:
                break
            lines.append(line)
            used += _units(line) + 1
            rounds += turn["role"] == "user"
        if not lines:
            return ""
        lines.reverse()
        return _RESTORE_HEADER.format(n=max(1, rounds)) + "\n" + "\n".join(lines)

    async def _todays_dream(self, today: str) -> str:
        """当天第一次开窗才跑；单独限时，跑失败 / 超时 / 返回失败文案都把额度还回去。"""
        if not await asyncio.to_thread(self.store.claim_daily, "dream_injected", today):
            return ""
        text = ""
        try:
            text = await asyncio.wait_for(self.dream_fn(), timeout=self.settings["dream_timeout"])
            text = str(text or "").strip()
            if text in _DREAM_FAILURE_TEXTS:
                text = ""
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("autorecall dream skipped: %s", type(exc).__name__)
            text = ""
        finally:
            if not text:
                await asyncio.to_thread(self.store.release_daily, "dream_injected", today)
        return text

    async def on_session_start(self, session_id: str, source: str, cwd: str = "") -> str:
        settings = self.settings
        if not settings["enabled"] or source not in ("startup", "clear", "compact"):
            return ""
        budget = settings["max_chars"]
        parts: list[str] = []
        # dream 只在开新窗口时领（压缩不算「开窗」）；当天已用过就不给它留预算，原文用满。
        today = self.today_fn()
        dream_due = (
            source in ("startup", "clear")
            and settings["dream_on_start"]
            and self.dream_fn is not None
            and not await asyncio.to_thread(self.store.daily_used, "dream_injected", today)
        )
        previous = session_id if source == "compact" else await asyncio.to_thread(
            self.store.latest_session, session_id, cwd
        )
        rounds = settings["restore_rounds"]
        if previous and rounds:
            turns = await asyncio.to_thread(self.store.last_turns, previous, rounds)
            if turns:
                restore = self._render_restore(turns, budget // 2 if dream_due else budget)
                if restore:
                    parts.append(restore)
        if dream_due:
            dream_text = await self._todays_dream(today)
            if dream_text:
                room = budget - _units("\n\n".join(parts)) - _units(_DREAM_HEADER) - 4
                parts.append(_DREAM_HEADER + "\n" + _clip(dream_text, room))
        return _clip("\n\n".join(parts), budget)
