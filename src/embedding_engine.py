"""
========================================
embedding_engine.py — 向量化引擎，给 breath/search 提供语义召回
========================================

向量化采用「门面 + 后端」两层：
- 后端实现（BaseEmbeddingEngine 子类）只负责把文本算成向量，不碰任何 IO/SQLite。
  后端：OpenAI 兼容 API（默认 Gemini）。
- 门面（EmbeddingEngine）持有一个后端实例，负责 SQLite 存取、余弦搜索、删除、
  孤儿对账、模型/维度元数据校验。对外接口零变化，bucket_manager 不需要动。

关键行为：
- generate_and_store(bucket_id, content)：写入或覆盖某个桶的向量
- search_similar(query, top_k)：返回 [(bucket_id, score)] 按相似度倒序
- search(query, top_k)：新接口，按规范只返回 bucket_id 列表
- delete_embedding(bucket_id)：与 BucketManager.delete 同步调用
- list_all_ids()：给 tools/clean_orphan_embeddings 用，找孤儿向量
- enabled=False 时所有方法 no-op，方便离线/测试
- 启动时若 db 里历史模型/维度与当前后端不一致 → 记 OB-W005 警告，不阻止启动

不做什么（边界）：
- 不读写桶文件
- 不做关键词检索（那是 BucketManager 的事）
- 不做去重 / 合并判断

对外暴露：
- BaseEmbeddingEngine（抽象基类，方便未来扩展）
- APIEmbeddingEngine（OpenAI 兼容 API，默认 Gemini）
- EmbeddingEngine（门面：保持向后兼容的对外类）
========================================
"""

from __future__ import annotations

import abc
import asyncio
import heapq
import hashlib
import json
import logging
import math
import os
import sqlite3
from collections import OrderedDict
from typing import Any

import httpx
import numpy as np
from openai import AsyncOpenAI

try:
    from utils import parse_bool, positive_float
except ImportError:  # pragma: no cover
    from .utils import parse_bool, positive_float  # type: ignore

from ombrebrain.integrations.provider_detect import (
    is_known_cloud_embedding_endpoint,
    normalize_model_for_endpoint,
    strip_native_resource_prefix,
)

logger = logging.getLogger("ombre_brain.embedding")


# ============================================================
# 常量
# ============================================================

_GEMINI_DEFAULT_DIM = 3072
_API_TIMEOUT_SECONDS = 30.0

# 输入截断长度
_MAX_INPUT_CHARS = 2000

# 同一段文本短时间内被多条链路重复请求向量（同一个 breath(query=...) 里
# bucket_mgr.search() 和 surface_search() 各查一次；同一个 hold() 里
# merge_or_create/check_duplicate_for/check_plan_resolution 各嵌入一次同样的
# content）。同一 (text, model) 恒定映射到同一向量，缓存最近 N 条查询结果即可
# 把这些重复请求拦在进程内，不用每次都打真实向量 API。
_QUERY_CACHE_MAXSIZE = 32

# SQLite stores vectors as JSON text.  Reading the whole table and converting
# every row to Python floats before the matrix operation can briefly hold the
# JSON strings, Python float objects and a second NumPy matrix at the same time.
# Keep that peak independent of vault size (important on 512 MiB hosts).
_SEARCH_BATCH_ROWS = 32


def _provider_input_identity(text: str) -> str:
    """为服务商实际可见的输入生成有界缓存标识。"""

    provider_text = text[:_MAX_INPUT_CHARS]
    return hashlib.sha256(provider_text.encode("utf-8")).hexdigest()


def _norm_model(name: str) -> str:
    """归一化模型名用于「同一性」比较。

    Gemini OpenAI-compat 端点要求 "models/" 前缀，OpenAI 兼容代理（aihubmix /
    硅基流动等）用裸名——同一模型仅前缀不同。剥掉前缀 + 去空白 + 小写，
    让 model_name 的对账只看真实身份，不被书写约定误伤（修 OB-W005 假阳性）。
    Ollama 的 ":latest" 是默认 tag：bge-m3 与 bge-m3:latest 是同一模型，
    比较前剥掉结尾 ":latest"。仅剥这一个 tag——:q4_0 之类量化 tag 代表
    不同的量化版本，是真实身份差异，不能剥。
    """
    return strip_native_resource_prefix(name).lower().removesuffix(":latest")


def _humanize_api_error(
    e: Exception,
    *,
    api_format: str = "openai_compat",
    base_url: str = "",
) -> str:
    """把 OpenAI 兼容后端的常见异常翻成可读中文提示，附在 OB-E001 detail 末尾。

    目的：让错误面板直接看懂 401/400/404/超时该怎么办，尤其跨境 provider 选错的
    场景（美国 VPS 连国内域名超时、国际站无某模型、key 不匹配 provider）。
    返回空串表示无额外可补充的提示。
    """
    name = type(e).__name__
    code = getattr(e, "status_code", None)
    s = str(e).lower()
    is_local = (api_format or "").strip().lower() in ("ollama", "local")
    if is_local:
        if code in (400, 404) or "badrequest" in name.lower() or "notfound" in name.lower():
            return "→ 本地 Ollama 未找到或不支持该模型：确认 Ollama 已运行并已拉取 bge-m3。"
        if "timeout" in name.lower() or "connect" in name.lower() or "timeout" in s:
            return "→ 本地 Ollama 连接失败：确认服务已启动，且 Base URL 指向 Ollama 而不是云端 API。"
        if code == 401 or "authentication" in name.lower() or "401" in s:
            return "→ 本地 Ollama 返回 401：当前地址可能是需要鉴权的代理，而不是标准 Ollama 服务。"
        return ""
    if code == 401 or "authentication" in name.lower() or "401" in s:
        return "→ 401：API key 无效或无权限，确认 key 正确且属于当前 base_url 的 provider。"
    if code == 404 or "notfound" in name.lower() or "404" in s:
        return "→ 404/model 不存在：确认模型名与 base_url 属同一 provider（如 SiliconFlow 国际站可能没有 BAAI/bge-m3）。"
    if code == 400 or "badrequest" in name.lower():
        return "→ 400：请求被拒，多为模型名不存在或参数不被支持，核对 model 名。"
    if "timeout" in name.lower() or "connect" in name.lower() or "timeout" in s:
        return (
            "→ 超时/连接失败：检查网络与 base_url 可达性。美国 VPS 直连国内域名"
            "（api.siliconflow.cn）极易超时，建议改用就近 provider 或本地 ollama。"
        )
    return ""


# ============================================================
# 后端基类 / Backend Abstract Base
# ============================================================

class BaseEmbeddingEngine(abc.ABC):
    """所有 embedding 后端的契约。

    设计原则：
    - generate 是同步接口（协议要求），生产路径应走 generate_async 原生异步。
    - model_name / vector_dim 在初始化后必须能稳定返回；不允许构造完了还说不出维度。
    - 后端不开 SQLite 连接，不读写桶文件，存储/查询交给门面 EmbeddingEngine。
    """

    @abc.abstractmethod
    def generate(self, text: str) -> list[float]:
        """同步算一条向量。失败返回空列表（不抛运行期异常）。"""

    @abc.abstractmethod
    def model_name(self) -> str:
        """返回当前模型名（用于元数据写入与前端显示）。"""

    @abc.abstractmethod
    def vector_dim(self) -> int:
        """返回向量维度（用于 db meta 校验防止混用）。"""

    @abc.abstractmethod
    async def generate_async(self, text: str) -> list[float]:
        """异步算一条向量（生产路径）。失败返回空列表（不抛运行期异常）。"""

    def warmup(self) -> None:
        """子类可选：提前把模型加载到内存，避免首次调用延迟。"""
        return None


# ============================================================
# API 后端：OpenAI 兼容（默认 Gemini）
# ============================================================

class APIEmbeddingEngine(BaseEmbeddingEngine):
    """OpenAI 兼容的远程 embedding API（默认 Gemini）。

    必须有 api_key；空 key 会在门面层抛 OB-F001。本类只负责发请求 + 拿向量。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int = _GEMINI_DEFAULT_DIM,
        timeout_seconds: float = _API_TIMEOUT_SECONDS,
        api_format: str = "openai_compat",
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.api_format = (api_format or "openai_compat").strip().lower()
        self.backend_name = (
            "ollama" if self.api_format in ("ollama", "local") else "api"
        )
        self.timeout_seconds = positive_float(timeout_seconds, _API_TIMEOUT_SECONDS)
        # Google's OpenAI-compatible endpoint wants OpenAI-style bare model IDs.
        # Native REST uses the "models/" resource prefix, so normalize pasted
        # native IDs here before calling embeddings.create().
        self.model = normalize_model_for_endpoint(model, base_url, self.api_format)
        self._dim = dim
        # 本地/容器 ollama 必须绕过系统代理。httpx 默认 trust_env=True 会读
        # 环境变量「以及 Windows 注册表/WinINET 系统代理」，于是 Clash/V2Ray 等
        # 一开，127.0.0.1:11434 也被丢给代理 → 502 空响应，本地向量化整条挂掉
        # （现网 Docker 没代理所以没暴露，但裸机用户极常见）。
        # 判定本地：base_url 指向 localhost / 127.0.0.1 / ollama 容器名 → trust_env=False。
        # 云端（Gemini / 硅基流动等）保持 trust_env=True，国内往往正需要代理才能到。
        _host = base_url or ""
        _is_local_host = any(h in _host for h in ("127.0.0.1", "localhost", "ombre-ollama", "[::1]"))
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=not _is_local_host),
        )

    def model_name(self) -> str:
        return self.model

    def vector_dim(self) -> int:
        return self._dim

    def generate(self, text: str) -> list[float]:
        """同步接口（基类协议要求）。生产路径走 generate_async。"""
        try:
            return asyncio.run(self.generate_async(text))
        except RuntimeError:
            logger.warning("[embedding] sync generate() called inside event loop; use generate_async")
            return []

    async def generate_async(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        try:
            response = await self._client.embeddings.create(
                model=self.model,
                input=text[:_MAX_INPUT_CHARS],
            )
            if response.data and len(response.data) > 0:
                vec = response.data[0].embedding
                # 第一次拿到向量时确认真实维度
                if vec and len(vec) != self._dim:
                    self._dim = len(vec)
                if vec:
                    return list(vec)
            # 拿到了 2xx 响应但没有可用向量 —— 不能静默返回 []，否则向量化「成功
            # 调用却没结果」会无声无息（#3）。记 OB-E001 让错误面板可见。
            self._record_e001(
                f"backend={self.backend_name} model={self.model} 返回空向量"
                f"（base_url={self.base_url}，检查 model 名 / base_url / key 是否匹配该 provider）"
            )
            return []
        except Exception as e:
            _hint = _humanize_api_error(
                e,
                api_format=self.api_format,
                base_url=self.base_url,
            )
            self._record_e001(
                f"backend={self.backend_name} model={self.model} base_url={self.base_url} "
                f"err={type(e).__name__}: {e}" + (f" {_hint}" if _hint else "")
            )
            return []

    @staticmethod
    def _record_e001(detail: str) -> None:
        try:
            from errors import record_error  # type: ignore
        except ImportError:
            from .errors import record_error  # type: ignore
        try:
            record_error("OB-E001", detail)
        except Exception:
            logger.warning(f"[embedding] OB-E001 (record failed): {detail}")


# ============================================================
# API 后端：Gemini 原生 REST
# ============================================================

class GeminiNativeEmbeddingEngine(BaseEmbeddingEngine):
    """Gemini 原生 REST embedding（不走 OpenAI-compat，直接调 embedContent）。

    端点：POST .../v1beta/models/{model}:embedContent?key={api_key}
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        dim: int = _GEMINI_DEFAULT_DIM,
        timeout_seconds: float = _API_TIMEOUT_SECONDS,
    ):
        self.api_key = api_key
        self.model = model
        self._dim = dim
        self.timeout_seconds = positive_float(timeout_seconds, _API_TIMEOUT_SECONDS)

    def model_name(self) -> str:
        return self.model

    def vector_dim(self) -> int:
        return self._dim

    def generate(self, text: str) -> list[float]:
        try:
            return asyncio.run(self.generate_async(text))
        except RuntimeError:
            logger.warning("[embedding] sync generate() called inside event loop; use generate_async")
            return []

    async def generate_async(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        import httpx
        model_id = strip_native_resource_prefix(self.model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:embedContent"
        payload = {"content": {"parts": [{"text": text[:_MAX_INPUT_CHARS]}]}}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as c:
                r = await c.post(
                    url,
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                )
                r.raise_for_status()
            values = r.json().get("embedding", {}).get("values", [])
            if values and len(values) != self._dim:
                self._dim = len(values)
            if values:
                return list(values)
            self._record_e001(
                f"backend=gemini_native model={self.model} 返回空向量（检查模型名是否支持 embedContent）"
            )
            return []
        except Exception as e:
            self._record_e001(
                f"backend=gemini_native model={self.model} err={type(e).__name__}: {e}"
            )
            return []

    @staticmethod
    def _record_e001(detail: str) -> None:
        try:
            from errors import record_error  # type: ignore
        except ImportError:
            from .errors import record_error  # type: ignore
        try:
            record_error("OB-E001", detail)
        except Exception:
            logger.warning(f"[embedding] OB-E001 (record failed): {detail}")


# ============================================================
# 门面：EmbeddingEngine — 对外保持原接口
# ============================================================

class EmbeddingEngine:
    """SQLite 存储 + 搜索 + 元数据校验，持有一颗 BaseEmbeddingEngine。"""

    def __init__(self, config: dict):
        self.v3_runtime = None
        # 进程内小容量 LRU：provider 输入摘要 -> embedding。缓存键不能保留完整
        # 桶正文；后端只会看到前 _MAX_INPUT_CHARS 个字符，identity 也必须遵守
        # 同一个边界，否则长桶会白白滞留在 512 MiB 实例内存中。
        self._query_cache: "OrderedDict[str, list[float]]" = OrderedDict()
        embed_cfg = config.get("embedding", {}) or {}
        timeout_seconds = positive_float(embed_cfg.get("timeout_seconds"), _API_TIMEOUT_SECONDS)

        # 解析 backend：env > config > 默认 api
        self.backend = "api"

        # 2) 解析 enabled。OB-F001：enabled=true 但 api_key 空，且后端是 api → 拒启
        enabled_cfg = parse_bool(embed_cfg.get("enabled", True), default=True)

        # 3) 解析 SQLite 路径（允许测试 fixture 通过 db_path 覆盖）
        custom_db = (embed_cfg.get("db_path") or "").strip()
        if custom_db:
            self.db_path = custom_db
        else:
            self.db_path = os.path.join(config["buckets_dir"], "embeddings.db")

        # 4) 实例化后端
        self._backend: BaseEmbeddingEngine | None = None
        self.enabled = False
        # model 是镜像属性（server.py 里的热重载会直接 setattr，所以保留）
        self.model: str = ""

        if not enabled_cfg:
            # 显式关闭：no-op 模式，仍初始化 db 让 list_all_ids 能跑
            self._init_db()
            return

        # 解析 api_format（提前到 key 检查之前）。本地 ollama/local 后端无需真实 key，
        # 不能因为「key 为空」就被打到待机模式。
        api_format = (
            (embed_cfg.get("api_format") or "").strip()
            or os.environ.get("OMBRE_EMBED_FORMAT", "openai_compat")
        ).lower()
        self.api_format = api_format
        is_local = api_format in ("ollama", "local")

        api_key = (embed_cfg.get("api_key") or "").strip()
        if not api_key:
            api_key = os.environ.get("OMBRE_EMBED_API_KEY", "").strip()
        # 本地模型没有 key 概念，但 OpenAI 客户端库要求 api_key 非空 → 补占位符。
        # 占位符会作为 Bearer 发给 ollama，ollama 不校验、照单全收。
        if is_local:
            # Never forward a retained Gemini/SiliconFlow secret to a local or
            # user-supplied Ollama URL. The real cloud key stays in config for
            # switching back, but the local runtime uses a non-secret token.
            api_key = "ollama"

        if not api_key:
            # 无 key（仅云端后端会走到这）→ 待机模式：enabled=False，DB 仍初始化，key 热更新后激活
            logger.warning("[embedding] enabled=true but no api_key — starting in standby (disabled); set OMBRE_EMBED_API_KEY to activate")
            self._init_db()
            return

        if is_local:
            # 本地 Ollama：OpenAI 兼容 /v1/embeddings。
            # 默认地址按宿主分流：Docker 里连同网络的 ombre-ollama 容器；
            # 裸机/原生连本机 127.0.0.1（否则原生用户切到本地会去连不存在的容器名）。
            _local_default = (
                "http://ombre-ollama:11434/v1"
                if os.path.exists("/.dockerenv")
                else "http://127.0.0.1:11434/v1"
            )
            configured_base = (embed_cfg.get("base_url") or "").strip()
            if configured_base and is_known_cloud_embedding_endpoint(configured_base):
                logger.warning(
                    "[embedding] local mode ignored stale cloud base_url=%s",
                    configured_base,
                )
                configured_base = ""
            base_url = (
                os.environ.get("OMBRE_OLLAMA_URL", "").strip()
                or configured_base
                or _local_default
            )
            model = embed_cfg.get("model") or "bge-m3"
            # bge-m3 = 1024 维；APIEmbeddingEngine 拿到第一颗向量后还会自校正，这里给正确默认值
            try:
                dim = int(embed_cfg.get("dim") or 1024)
            except (TypeError, ValueError):
                dim = 1024
            self._backend = APIEmbeddingEngine(
                api_key=api_key,
                base_url=base_url,
                model=model,
                dim=dim,
                timeout_seconds=timeout_seconds,
                api_format=api_format,
            )
        elif api_format == "gemini":
            model = embed_cfg.get("model") or "gemini-embedding-001"
            self._backend = GeminiNativeEmbeddingEngine(
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        else:
            model = embed_cfg.get("model") or "gemini-embedding-001"
            base_url = (
                (embed_cfg.get("base_url") or "").strip()
                or "https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            # 读 dim 并透传，否则非默认维度的 OpenAI 兼容模型（如硅基流动 BAAI/bge-m3=1024）
            # 会被 APIEmbeddingEngine 的默认 Gemini 维度钉死 → 启动时 db dim vs current dim 不一致。
            # 报 OB-W005、逼用户去 migrate（即便 config.yaml 已写 embedding.dim: 1024）。
            # fallback 用 _GEMINI_DEFAULT_DIM 而非 1024：本分支默认端点/模型就是 Gemini，
            # 没显式配 dim 时必须保持 Gemini 官方默认维度，否则会把默认 Gemini 路径打错。
            try:
                dim = int(embed_cfg.get("dim") or _GEMINI_DEFAULT_DIM)
            except (TypeError, ValueError):
                dim = _GEMINI_DEFAULT_DIM
            self._backend = APIEmbeddingEngine(
                api_key=api_key,
                base_url=base_url,
                model=model,
                dim=dim,
                timeout_seconds=timeout_seconds,
                api_format=api_format,
            )

        self.model = self._backend.model_name()
        self.enabled = True

        # 5) 初始化 SQLite + 校验元数据
        self._init_db()
        self._check_meta_consistency()

    def attach_v3_runtime(self, runtime) -> None:
        self.v3_runtime = runtime

    # -------------------- SQLite 初始化 --------------------

    def _init_db(self) -> None:
        """建表。embeddings 主表 + embeddings_meta 元数据表（2.0.3 新增）。"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    bucket_id TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
            }
            if "content_hash" not in columns:
                conn.execute(
                    "ALTER TABLE embeddings ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"
                )
            if "meaning_embedding" not in columns:
                # Miss: meaning 的向量独立存一列，不与 content 的 embedding 混合生成，
                # 否则长 content 会稀释掉一句话 meaning 的语义信号。NULL = 该桶未写
                # meaning，或 meaning 向量化尚未成功（无需专门迁移历史桶）。
                conn.execute(
                    "ALTER TABLE embeddings ADD COLUMN meaning_embedding TEXT"
                )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _read_meta(self) -> dict[str, str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT key, value FROM embeddings_meta").fetchall()
            return {k: v for k, v in rows}
        finally:
            conn.close()

    def _write_meta(self, key: str, value: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def _check_meta_consistency(self) -> None:
        """对账历史 model_name / vector_dim 与当前后端是否一致。

        - 主表为空：第一次写入，覆盖 meta，无害
        - meta 与当前后端不一致：记 OB-W005 警告，提示她/他跑迁移
        """
        if not self._backend:
            return
        meta = self._read_meta()
        conn = sqlite3.connect(self.db_path)
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        finally:
            conn.close()

        cur_name = self._backend.model_name()
        cur_dim = str(self._backend.vector_dim())

        if cnt == 0:
            self._write_meta("model_name", cur_name)
            self._write_meta("vector_dim", cur_dim)
            return

        old_name = meta.get("model_name", "")
        old_dim = meta.get("vector_dim", "")
        if not old_name and not old_dim:
            # 老库（2.0.3 之前）没有 meta 表数据，写一次但不报警
            self._write_meta("model_name", cur_name)
            self._write_meta("vector_dim", cur_dim)
            return

        # 归一化模型名再比较：Gemini 的 OpenAI-compat 端点要求 "models/" 前缀，
        # 而 aihubmix / 硅基流动等 OpenAI 兼容代理用裸名，同一个模型会因前缀差异
        # （models/gemini-embedding-001 vs gemini-embedding-001）被误判为 mismatch，
        # 触发假 OB-W005。前缀只是端点书写约定，不代表模型身份不同，比较前一律剥掉。
        if _norm_model(old_name) == _norm_model(cur_name) and old_name != cur_name:
            # 实质相同、只差前缀：顺手把 meta 升级成当前写法，避免每次启动重复对账
            self._write_meta("model_name", cur_name)
            old_name = cur_name

        # 模型名相同、但维度不同：几乎必然是后端 _dim 仍是初始默认值（首颗向量生成前
        # 无法自校正，如 openai_compat 分支 bge-m3 默认 768 而真实 1024），而 db 里
        # old_dim 是该模型真实输出维度。同一模型维度恒定，直接信任 db 维度校正后端，
        # 不报 OB-W005——这正是「重算/redeploy 后仍反复报 W005」的根因（对账永远发生
        # 在自校正之前）。只有模型名真的不同（换了模型）才落到下面报 W005 提示迁移。
        if (
            _norm_model(old_name) == _norm_model(cur_name)
            and old_dim and old_dim != cur_dim
        ):
            try:
                self._backend._dim = int(old_dim)
                cur_dim = old_dim
                logger.info(
                    f"[embedding] 按 db 已存维度校正后端 vector_dim → {old_dim}"
                    f"（模型 {cur_name} 一致，避免假 OB-W005）"
                )
            except (TypeError, ValueError):
                pass

        if _norm_model(old_name) != _norm_model(cur_name) or old_dim != cur_dim:
            try:
                from errors import record_error  # type: ignore
            except ImportError:
                from .errors import record_error  # type: ignore
            record_error(
                "OB-W005",
                (
                    f"embeddings.db meta mismatch: "
                    f"db(model={old_name},dim={old_dim}) vs current(model={cur_name},dim={cur_dim}). "
                    f"Run /api/embedding/migrate to re-index."
                ),
            )

    # -------------------- 生成 + 存储 --------------------

    async def _generate_async(self, text: str) -> list[float]:
        if not self._backend:
            return []
        cache_identity = _provider_input_identity(text)
        cached = self._query_cache.get(cache_identity)
        if cached is not None:
            self._query_cache.move_to_end(cache_identity)
            return list(cached)
        embedding = await self._backend.generate_async(text)
        if embedding:
            self._query_cache[cache_identity] = list(embedding)
            self._query_cache.move_to_end(cache_identity)
            if len(self._query_cache) > _QUERY_CACHE_MAXSIZE:
                self._query_cache.popitem(last=False)
        return embedding

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """为内容生成 embedding 并存入 SQLite。成功返回 True。"""
        if not self.enabled or not content or not content.strip():
            return False
        try:
            embedding = await self._generate_async(content)
            if not embedding:
                return False
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            self._store_embedding(bucket_id, embedding, digest)
            return True
        except Exception as e:
            logger.warning(f"Embedding generation failed for {bucket_id}: {e}")
            return False

    def _store_embedding(
        self, bucket_id: str, embedding: list[float], content_hash: str = ""
    ) -> None:
        try:
            from utils import now_iso  # type: ignore
        except ImportError:
            from .utils import now_iso  # type: ignore
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO embeddings (bucket_id, embedding, updated_at, content_hash)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(bucket_id) DO UPDATE SET
                     embedding=excluded.embedding,
                     updated_at=excluded.updated_at,
                     content_hash=excluded.content_hash""",
                (bucket_id, json.dumps(embedding), now_iso(), content_hash),
            )
            conn.commit()
        finally:
            conn.close()

    async def generate_and_store_meaning(self, bucket_id: str, meaning_text: str) -> bool:
        """Miss: 为 meaning（最近一条）单独生成并存储一份 embedding。

        与 content 的向量分列存储，互不覆盖；桶可能还没有 content 向量行
        （outbox 还在排队），所以用 upsert 而不是要求行已存在。
        """
        if not self.enabled or not meaning_text or not meaning_text.strip():
            return False
        try:
            embedding = await self._generate_async(meaning_text)
            if not embedding:
                return False
            self._store_meaning_embedding(bucket_id, embedding)
            return True
        except Exception as e:
            logger.warning(f"Meaning embedding generation failed for {bucket_id}: {e}")
            return False

    def _store_meaning_embedding(self, bucket_id: str, embedding: list[float]) -> None:
        try:
            from utils import now_iso  # type: ignore
        except ImportError:
            from .utils import now_iso  # type: ignore
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO embeddings (bucket_id, embedding, updated_at, content_hash, meaning_embedding)
                   VALUES (?, '', ?, '', ?)
                   ON CONFLICT(bucket_id) DO UPDATE SET
                     meaning_embedding=excluded.meaning_embedding""",
                (bucket_id, now_iso(), json.dumps(embedding)),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_embedding(self, bucket_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
            conn.commit()
        finally:
            conn.close()

    def list_all_ids(self) -> list[str]:
        """孤儿对账用：embeddings 表里所有 bucket_id。"""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT bucket_id FROM embeddings").fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def delete_meaning_embedding(self, bucket_id: str) -> None:
        """清空 meaning 派生列，保留同桶的 content 向量。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE embeddings SET meaning_embedding = NULL WHERE bucket_id = ?",
                (bucket_id,),
            )
            conn.execute(
                "DELETE FROM embeddings WHERE bucket_id = ? "
                "AND TRIM(embedding) = '' AND meaning_embedding IS NULL",
                (bucket_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def list_content_ids(self) -> list[str]:
        """Return IDs that have a real content vector, not only meaning data.

        Rows created by ``generate_and_store_meaning`` deliberately contain an
        empty ``embedding`` value until the durable content outbox catches up.
        Legacy content rows, on the other hand, may have an empty
        ``content_hash`` after the schema migration while still holding a valid
        vector. Inspecting the vector column keeps those two states distinct.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT bucket_id FROM embeddings WHERE TRIM(embedding) <> ''"
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            conn.close()

    def list_content_hashes(self) -> dict[str, str]:
        """Return hashes recorded by new writes; legacy rows contain ``""``."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT bucket_id, content_hash FROM embeddings"
            ).fetchall()
            return {str(bucket_id): str(digest or "") for bucket_id, digest in rows}
        finally:
            conn.close()

    def get_content_hash(self, bucket_id: str) -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT content_hash FROM embeddings WHERE bucket_id = ?", (bucket_id,)
            ).fetchone()
            return str(row[0] or "") if row else ""
        finally:
            conn.close()

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT embedding FROM embeddings WHERE bucket_id = ?", (bucket_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    # -------------------- 搜索 --------------------

    async def search_similar_strict(
        self,
        query: str,
        top_k: int = 10,
        allowed_bucket_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return ranked neighbors, surfacing provider failures to the caller."""
        if not self.enabled:
            raise RuntimeError("embedding is disabled")

        # Preserve the old behaviour of not calling the provider for an empty
        # index, without retaining any embedding payload across the await.
        conn = sqlite3.connect(self.db_path)
        try:
            has_rows = conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone()
        finally:
            conn.close()
        if not has_rows:
            return []

        query_embedding = await self._generate_async(query)
        if not query_embedding:
            raise RuntimeError("embedding provider returned an empty query vector")

        if top_k <= 0:
            return []

        # Heap entries use (score, -SQLite row index, bucket_id).  Larger is
        # better, so the min-heap root is always the worst retained candidate;
        # for equal scores, later rows are worse.  This preserves the old
        # stable-sort tie order while bounding ranking memory to O(top_k).
        top_results: list[tuple[float, int, str]] = []
        row_index = 0
        query_dim = len(query_embedding)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT bucket_id, embedding, meaning_embedding FROM embeddings"
            )
            while True:
                rows = cursor.fetchmany(_SEARCH_BATCH_ROWS)
                if not rows:
                    break

                bucket_ids: list[str] = []
                best_scores: list[float | None] = []
                candidate_vectors: list[list[float]] = []
                candidate_owners: list[int] = []
                for bucket_id, emb_json, meaning_emb_json in rows:
                    # Access-sensitive callers (currently Letter) must remove
                    # forbidden candidates before their vectors participate in
                    # ranking.  Filtering results after similarity calculation
                    # would leak that hidden content matched the query.
                    if (
                        allowed_bucket_ids is not None
                        and bucket_id not in allowed_bucket_ids
                    ):
                        row_index += 1
                        continue
                    # 一个桶可能同时有 content 向量和 meaning 向量，取相似度
                    # 较高的一个。所有大对象都只活到当前小批次结束。
                    owner = len(bucket_ids)
                    bucket_ids.append(bucket_id)
                    best_scores.append(None)
                    for label, raw_embedding in (
                        ("embedding", emb_json),
                        ("meaning embedding", meaning_emb_json),
                    ):
                        if not raw_embedding:
                            continue
                        try:
                            stored_embedding = json.loads(raw_embedding)
                            if not isinstance(stored_embedding, list):
                                raise TypeError(
                                    f"embedding is {type(stored_embedding).__name__}, not list"
                                )
                            if not stored_embedding:
                                continue
                            stored_embedding = [float(value) for value in stored_embedding]
                        except (json.JSONDecodeError, ValueError, TypeError) as _emb_exc:
                            logger.warning(
                                f"[embedding] Skipping malformed {label} for {bucket_id!r}: "
                                f"{type(_emb_exc).__name__}: {_emb_exc}"
                            )
                            continue
                        if len(stored_embedding) != query_dim:
                            # Preserve the pairwise helper's existing contract:
                            # a dimension mismatch contributes a 0.0 score.
                            logger.warning(
                                f"[embedding] {label} dimension mismatch for {bucket_id!r}: "
                                f"stored={len(stored_embedding)}, query={query_dim}"
                            )
                            current = best_scores[owner]
                            best_scores[owner] = (
                                0.0 if current is None else max(current, 0.0)
                            )
                            continue
                        candidate_vectors.append(stored_embedding)
                        candidate_owners.append(owner)

                if candidate_vectors:
                    similarities = self._cosine_similarity_batch(
                        query_embedding, candidate_vectors
                    )
                    for owner, similarity in zip(candidate_owners, similarities):
                        score = float(similarity)
                        current = best_scores[owner]
                        best_scores[owner] = (
                            score if current is None else max(current, score)
                        )

                for bucket_id, score in zip(bucket_ids, best_scores):
                    current_index = row_index
                    row_index += 1
                    if score is None:
                        continue
                    entry = (score, -current_index, bucket_id)
                    if len(top_results) < top_k:
                        heapq.heappush(top_results, entry)
                    elif entry > top_results[0]:
                        heapq.heapreplace(top_results, entry)
        finally:
            conn.close()

        top_results.sort(reverse=True)
        return [(bucket_id, score) for score, _negative_index, bucket_id in top_results]

    async def search_similar(
        self,
        query: str,
        top_k: int = 10,
        allowed_bucket_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """返回 [(bucket_id, similarity)]；失败时兼容旧调用方并返回空列表。"""
        try:
            return await self.search_similar_strict(
                query,
                top_k=top_k,
                allowed_bucket_ids=allowed_bucket_ids,
            )
        except Exception as e:
            logger.warning(f"Query embedding failed: {e}")
            return []

    async def search(self, query: str, top_k: int = 10) -> list[str]:
        """规范新接口：只返回 bucket_id 列表。"""
        pairs = await self.search_similar(query, top_k=top_k)
        return [bid for bid, _ in pairs]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _cosine_similarity_batch(
        query: list[float], vectors: list[list[float]]
    ) -> "np.ndarray":
        """Return cosine similarity for equally-sized vectors in one matrix pass."""
        query_array = np.asarray(query, dtype=np.float64)
        matrix = np.asarray(vectors, dtype=np.float64)
        dots = matrix @ query_array
        denominator = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_array)
        return np.divide(
            dots,
            denominator,
            out=np.zeros_like(dots),
            where=denominator != 0,
        )

    # -------------------- 前端可读的状态 --------------------

    def status(self) -> dict[str, Any]:
        """前端 /api/embedding/status 用。"""
        if not self._backend:
            return {
                "enabled": False,
                "backend": self.backend,
                "model": "",
                "vector_dim": 0,
                "db_path": self.db_path,
                "embedding_count": 0,
            }
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            cnt = -1
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "model": self._backend.model_name(),
            "vector_dim": self._backend.vector_dim(),
            "db_path": self.db_path,
            "embedding_count": cnt,
        }


__all__ = [
    "BaseEmbeddingEngine",
    "APIEmbeddingEngine",
    "GeminiNativeEmbeddingEngine",
    "EmbeddingEngine",
]
