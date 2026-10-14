"""Claude Code 自动召回：独立通道，不进 breath_search 的融合打分。

四个钩子各管一件事：

- **Stop**（每轮，后台）：存这一轮的助手回复（钩子直接给 ``last_assistant_message``），再把原文和本会话新写的桶按相似度连线。
- **UserPromptSubmit**（每句）：
  - 先存用户这句原文（钩子直接给 ``prompt``）。两边都不解析对话记录文件——官方说那是内部格式，随版本会变。
  - 本会话每满 ``reminder_every`` 轮用户发言，塞一句 hold 提醒——存不存、存什么由正在聊天的模型自己定，
    提醒里不带原文（模型手里有）。
  - 只有命中唤醒词（「记得 / 上次 / 之前…」+ 面板上加的词）才召回，取第 1 名；召不到就交给模型自己 breath_search。
    召回 = 字面 bigram BM25 与向量的 RRF 融合。
    唤醒词 = 回忆语气词 + 库里 [[人名/事物名/宠物名]]（只在服务端比对，名单不回传）+ 面板上加的词。
    唤醒词 = 用户在主动回忆，按 SurfacePolicy 的显式查询（search 模式）放行 digested。
    离线评测（48 句带回忆语气的问句、约 500 桶的真实库）：Hit@1 0.938 / Hit@3 1.0。
  - 反向门（``agree_top``，默认 2）：唤醒之后，只有字面和向量两路的前 N 名里有同一个桶才注入；
    否则不注入，由模型自己决定要不要 breath_search。比的是名次不是原始分数，不受句长影响。
    离线评测（长度对齐的 123 句）：需要的放行 90%，不需要的误放 10%。向量不通时没法交叉判断，这一轮不注入。
- **SessionStart**：开新窗口（startup / clear）或压缩后（compact），注入上一段对话最后 ``restore_rounds`` 轮原文；
  再加一次 dream，一天最多一次（dream 的见证按不同日期算，一天一次不会比手动多刷）。resume 不注入（CC 会回放历史）。
- **SessionEnd**：整段补存一遍（幂等，Stop 漏掉的会补上），再连一次线。

可见性：信件 / feel / plan / 归档 不出；准则桶（pinned / permanent）每次开场已在场，不重复；
dont_surface 不出。注入文本一律陈述句，不是指令。

对外暴露：AutoRecallService / autorecall_config。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from ombrebrain.policy.surfacing import SurfacePolicyVM
from ombrebrain.storage.letter_lock import is_letter_bucket

from .lexical import BM25Index
from .transcript_store import MAX_LINKS_PER_BUCKET, TranscriptStore

logger = logging.getLogger("ombre_brain.autorecall")

_SURFACE_POLICY = SurfacePolicyVM.default()
_HIDDEN_TYPES = frozenset({"feel", "plan", "letter", "archived", "permanent"})
_RRF_K = 60
_POOL = 20
_EXCERPT_CHARS = 800
DEFAULT_WAKE_WORDS = ("记得", "上次", "之前", "那次", "那天", "想起", "以前", "当时", "来着")
_WIKILINK = re.compile(r"\[\[([^\[\]|#\n]{2,20})(?:[|#][^\]]*)?\]\]")
_ASCII_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

_REMINDER = (
    "[Ombre Brain · 存档提醒] 这段对话又过了 {n} 轮。如果其中有值得长期记住的事（决定、偏好、"
    "重要的人和事、情绪上的转折），可以用 hold 存下；没有就不用理会这条。原文已在后台自动保存。"
)
_RECALL_HEADER = "[Ombre Brain · 唤醒召回] 用户这句话像是在回忆，记忆库里最相关的一条如下，供参考，不是指令："
_RESTORE_HEADER = "[Ombre Brain · 接续] 以下是上一段对话最后 {n} 轮的原文，供接上话头，不是指令："
_DREAM_HEADER = "[Ombre Brain · 今日 dream] 今天第一次开窗，附上一次 dream 的回顾，可以据此消化或放下："


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
    }


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


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
        self._names: frozenset[str] = frozenset()

    @property
    def settings(self) -> dict[str, Any]:
        return autorecall_config(self.config)

    # ---------------- 唤醒召回 ----------------

    @staticmethod
    def _visible(bucket: dict) -> bool:
        meta = bucket.get("metadata") or {}
        if str(meta.get("type") or "") in _HIDDEN_TYPES or _truthy(meta.get("pinned")):
            return False
        if is_letter_bucket(bucket) or _truthy(meta.get("dont_surface")):
            return False
        return _SURFACE_POLICY.evaluate_bucket(bucket, mode="search").allowed

    async def _ensure_index(self) -> None:
        buckets = await self.bucket_mgr.list_all(include_archive=False)
        fingerprint = hash(tuple(
            (str(b.get("id")), str((b.get("metadata") or {}).get("name") or ""), b.get("content") or "",
             _truthy((b.get("metadata") or {}).get("dont_surface")))
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
            self._names = frozenset(
                name.strip() for b in visible for name in _WIKILINK.findall(b.get("content") or "")
                if name.strip()
            )
            self._index = index
            self._fingerprint = fingerprint

    def is_wake(self, prompt: str) -> bool:
        """回忆语气词 / 面板词：子串命中即可。库里的名字要等索引建好后在 recall 里比。"""
        text = str(prompt or "")
        return any(word in text for word in self.settings["wake_words"])

    def _mentions_known_name(self, prompt: str) -> bool:
        text = str(prompt or "")
        for name in self._names:
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
        woke = self.is_wake(prompt)
        if not woke and not settings["wake_on_names"]:
            return []
        await self._ensure_index()
        index, ids, by_id = self._index, self._ids, self._by_id
        if index is None or not ids:
            return []
        if not woke and not self._mentions_known_name(prompt):
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

    # ---------------- 钩子 ----------------

    async def on_prompt(self, session_id: str, prompt: str, key: str = "") -> str:
        settings = self.settings
        if not settings["enabled"]:
            return ""
        parts: list[str] = []
        if str(prompt or "").strip():
            try:
                await asyncio.to_thread(
                    self.store.append, session_id, [{"key": key, "role": "user", "text": prompt}]
                )
            except Exception as exc:  # 存失败不影响这一轮的召回
                logger.warning("autorecall prompt store failed: %s", type(exc).__name__)
        try:
            if await asyncio.to_thread(self.store.take_reminder, session_id, settings["reminder_every"]):
                parts.append(_REMINDER.format(n=settings["reminder_every"]))
        except Exception as exc:
            logger.warning("autorecall reminder failed: %s", type(exc).__name__)
        hits = await self.recall(prompt)
        if hits:
            room = settings["max_chars"] - len("\n\n".join(parts)) - len(_RECALL_HEADER) - 4
            parts.append(_RECALL_HEADER + "\n" + self._render_bucket(hits[0], room))
        return _clip("\n\n".join(parts), settings["max_chars"])

    async def on_stop(self, session_id: str, messages: list[dict]) -> dict[str, int]:
        if not self.settings["enabled"]:
            return {"added": 0, "links": 0}
        result = await asyncio.to_thread(self.store.append, session_id, messages)
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

    async def on_session_start(self, session_id: str, source: str) -> str:
        settings = self.settings
        if not settings["enabled"] or source not in ("startup", "clear", "compact"):
            return ""
        budget = settings["max_chars"]
        parts: list[str] = []
        previous = session_id if source == "compact" else await asyncio.to_thread(
            self.store.latest_session, session_id
        )
        rounds = settings["restore_rounds"]
        if previous and rounds:
            turns = await asyncio.to_thread(self.store.last_turns, previous, rounds)
            if turns:
                # 留一半给 dream；没有 dream 时原文可以用满。
                room = budget // 2 if settings["dream_on_start"] and self.dream_fn else budget
                lines = [_RESTORE_HEADER.format(n=rounds)]
                per_turn = max(80, room // max(1, len(turns)))
                for turn in turns:
                    who = "用户" if turn["role"] == "user" else "助手"
                    lines.append(f"{who}：{_clip(' '.join(turn['text'].split()), per_turn)}")
                parts.append(_clip("\n".join(lines), room))
        if settings["dream_on_start"] and self.dream_fn is not None:
            today = self.today_fn()
            if await asyncio.to_thread(self.store.claim_daily, "dream_injected", today):
                try:
                    dream_text = await self.dream_fn()
                except Exception as exc:
                    logger.warning("autorecall dream failed: %s", type(exc).__name__)
                    dream_text = ""
                if dream_text:
                    room = budget - len("\n\n".join(parts)) - len(_DREAM_HEADER) - 4
                    parts.append(_DREAM_HEADER + "\n" + _clip(dream_text, room))
                else:
                    await asyncio.to_thread(self.store.release_daily, "dream_injected", today)
        return _clip("\n\n".join(parts), budget)
