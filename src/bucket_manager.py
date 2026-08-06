"""
========================================
bucket_manager.py — 记忆桶的增删改查与多维索引
========================================

一个「记忆桶」就是一份带 YAML frontmatter 的 Markdown 文件。
这个文件负责把它们读出来、写回去、按主题域+情感坐标+文本模糊匹配筛出来。

关键行为：
- 每个桶 = 一个 .md 文件，按 permanent / dynamic / archive / feel / plans / letters 分目录存
- 创建/读取/更新/删除/搬家（move）都在这里
- 检索 = 先按 domain 预筛，再按情感坐标 + 文本相似度加权排序
- 情感坐标是 Russell 环形模型的连续值：valence 0~1（消极→积极），arousal 0~1（平静→激动）
- create()/update(content=...)/delete() 自动同步 embedding 索引（iter 2.1+），
  避免「文件存在但向量缺失」的孤儿桶导致 breath 检索数对不上 pulse
- iter 2.0：create() 接受 ``bucket_id_override``（feel 用分钟级可读 id），
  以及 ``source_tool`` / ``grow_batch_id`` 用于来源追踪

不做什么（边界）：
- 不做衰减打分（那是 decay_engine 的事）
- 不做 LLM 调用、不做向量化（那是 dehydrator / embedding_engine 的事）
- 不直接对外提供 MCP 工具（被 tools/* 通过 _runtime 引用）

对外暴露：BucketManager 类（create / get / update / delete / search / list_by_type 等）
========================================
"""
# ============================================================

import os
import re
import math
import logging
import shutil
import uuid
from datetime import date, datetime

# 统一错误体系：越界 clamp 时上报 OB-W001/OB-W002（rule.md §11）
try:
    from errors import push_warning as _ob_push_warning  # type: ignore
except Exception:
    try:
        from .errors import push_warning as _ob_push_warning  # type: ignore
    except Exception:
        def _ob_push_warning(*_a, **_kw):  # type: ignore
            return None


def _clamp_importance(v, source: str) -> int:
    """importance 越界 → clamp 到 [1,10]，并产生 OB-W001 提示。"""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        _ob_push_warning("OB-W001", f"importance={v!r} 无法解析，回退为 5（{source}）")
        return 5
    if iv < 1 or iv > 10:
        clamped = max(1, min(10, iv))
        _ob_push_warning("OB-W001", f"importance={iv} 超出 [1,10]，已修正为 {clamped}（{source}）")
        return clamped
    return iv


def _clamp_unit(v, field: str, source: str) -> float:
    """valence/arousal 越界 → clamp 到 [0.0,1.0]，并产生 OB-W002 提示。"""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        _ob_push_warning("OB-W002", f"{field}={v!r} 无法解析，回退为 0.5（{source}）")
        return 0.5
    if fv < 0.0 or fv > 1.0:
        clamped = max(0.0, min(1.0, fv))
        _ob_push_warning("OB-W002", f"{field}={fv} 超出 [0.0,1.0]，已修正为 {clamped}（{source}）")
        return clamped
    return fv


from pathlib import Path
from typing import Any, Optional

import frontmatter
from rapidfuzz import fuzz

from utils import generate_bucket_id, sanitize_name, safe_path, now_iso, parse_iso_aware
from bucket_scoring import (
    calc_topic_score,
    calc_emotion_score,
    calc_time_score,
    calc_touch_score,
)

try:
    from bm25_index import BM25Index as _BM25Index
except ImportError:
    _BM25Index = None  # type: ignore

logger = logging.getLogger("ombre_brain.bucket")


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。检索评分、时间涾漪、字段截断上限集中在这里。
# 修改这些数值 → 请同步跑 tests/regression 验证评分行为。
# ============================================================

# --- 默认元数据值（与 dehydrator/import_memory 保持一致）---
_DEFAULT_VALENCE = 0.5
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_PINNED_IMPORTANCE = 10           # pinned/protected 桶 importance 锁定值
_DEFAULT_DOMAIN_NAME = "未分类"     # 未提供 domain 时的占位

# --- 字段截断长度（避免 frontmatter 肨胀）---
_SOURCE_TOOL_MAX = 32
_GROW_BATCH_ID_MAX = 64
_WHY_REMEMBERED_MAX = 500
_TRIGGERED_BY_MAX = 64

# --- _time_ripple：时间涾漪 ---
_RIPPLE_HOURS = 48.0       # ±该小时内的桶被轻微唤醒
_RIPPLE_MAX_BUCKETS = 5    # 一次 touch 最多唤醒几个邻居（有界 I/O）
_RIPPLE_BOOST = 0.3        # 唤醒时 activation_count 增量

# --- search 评分 ---
_VECTOR_TOPK = 50          # embedding 预取 top_k（仅作 semantic 分源，不窄化候选集）
_RESOLVED_RANK_PENALTY = 0.3   # resolved 桶仅在排序时降权
_LITERAL_MATCH_BONUS = 25.0    # 查询串原样命中 name/tags/domain/正文时的召回加分（修短查询召回）

# topic/emotion/time/touch 四个评分维度的纯函数 + 权重常量已拆到
# bucket_scoring.py（search() 和 _calc_*_score 兼容 wrapper 都从那边导入）。


def _clamp01(value, default: float) -> float:
    """将任意输入钳制到 [0.0, 1.0]；失败返回 default。

    专门处理身体里散落的 ``max(0.0, min(1.0, float(x)))`` 样板
    （model_valence / weight / bucket_type_defaults.weight 等）。
    哲学 valence/arousal 请走 _clamp_unit，那个会 push OB-W002。
    这个 helper 静默钳制，适用于“调用方保证范围、充其量充个防”的场景。
    """
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    记忆桶管理器 —— 所有桶的 CRUD 操作入口。
    桶以 Markdown 文件存储，YAML frontmatter 存元数据，正文存内容。
    天然兼容 Obsidian 直接浏览和编辑。
    """

    def __init__(self, config: dict, embedding_engine=None, v3_runtime=None):
        # iter 1.9 G: 保留原始 config 引用，让 create() 能读 bucket_type_defaults
        # Keep raw config so create() can look up bucket_type_defaults at write time.
        self.config = config
        self.v3_runtime = v3_runtime
        # --- Read storage paths from config / 从配置中读取存储路径 ---
        self.base_dir = config["buckets_dir"]
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.feel_dir = os.path.join(self.base_dir, "feel")
        self.plan_dir = os.path.join(self.base_dir, "plans")
        self.letter_dir = os.path.join(self.base_dir, "letters")
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 50)
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Wikilink config / 双链配置 ---
        wikilink_cfg = config.get("wikilink", {})
        self.wikilink_enabled = wikilink_cfg.get("enabled", True)
        self.wikilink_use_tags = wikilink_cfg.get("use_tags", False)
        self.wikilink_use_domain = wikilink_cfg.get("use_domain", True)
        self.wikilink_use_auto_keywords = wikilink_cfg.get("use_auto_keywords", True)
        self.wikilink_auto_top_k = wikilink_cfg.get("auto_top_k", 8)
        self.wikilink_min_len = wikilink_cfg.get("min_keyword_len", 2)
        self.wikilink_exclude_keywords = set(wikilink_cfg.get("exclude_keywords", []))
        self.wikilink_stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "我们", "你们", "他们", "然后", "今天", "昨天", "明天", "一下",
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "has", "have", "with",
            "this", "that", "from", "they", "been", "said", "will", "each",
        }
        self.wikilink_stopwords |= {w.lower() for w in self.wikilink_exclude_keywords}

        # --- Search scoring weights / 检索权重配置 ---
        scoring = config.get("scoring_weights", {})
        self.w_topic = scoring.get("topic_relevance", 4.0)
        self.w_emotion = scoring.get("emotion_resonance", 2.0)
        self.w_time = scoring.get("time_proximity", 1.5)
        self.w_importance = scoring.get("importance", 1.0)
        self.content_weight = scoring.get("content_weight", 1.0)  # body×1, per spec
        # iter 2.1: touch + semantic 两个新维度
        # touch: 被主动召回越多加分越高（上限 10 次归一化）
        # semantic: embedding 余弦相似度（仅 embedding 启用时生效）
        self.w_touch = scoring.get("touch_weight", 1.0)
        self.w_semantic = scoring.get("semantic_weight", 2.5)
        # BM25: TF-IDF 加权关键词匹配（rank_bm25+jieba，软依赖）
        self.w_bm25 = scoring.get("bm25_weight", 1.5)

        # --- Optional embedding engine for pre-filtering / 可选 embedding 引擎，用于预筛候选集 ---
        self.embedding_engine = embedding_engine

        # BM25 稀疏索引（写操作后脏标记，search() 时懒重建）
        self._bm25: "_BM25Index | None" = _BM25Index() if _BM25Index is not None else None
        self._bm25_dirty: bool = True

    def attach_v3_runtime(self, runtime) -> None:
        self.v3_runtime = runtime

    def _record_v3_bucket_event(
        self,
        action: str,
        bucket_id: str,
        bucket_type: str,
        content: str,
        metadata: dict | None,
    ) -> None:
        runtime = getattr(self, "v3_runtime", None)
        recorder = getattr(runtime, "record_bucket_event", None)
        if not callable(recorder):
            return
        try:
            recorder(
                action=action,
                bucket_id=bucket_id,
                bucket_type=bucket_type,
                content=content,
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.warning(f"v3 bucket event record failed for {action}:{bucket_id}: {exc}")

    # ---------------------------------------------------------
    # Internal helpers【代码多复用、不作为公共 API】
    # 内部工具：目录遍历 / 主域路径 / 装入与开销完全一致于原原本
    # ---------------------------------------------------------
    @property
    def _active_dirs(self) -> list[str]:
        """不含 archive 的活跃桶目录（list_all/_collect_all_tags/查找均使用）。。。顺序不可随意调整：feel/plan/letter 在 dynamic 之后是为了与原代码扫描顺序保持一致。"""
        return [self.permanent_dir, self.dynamic_dir,
                self.feel_dir, self.plan_dir, self.letter_dir]

    def _iter_md_files(self, dirs: list[str]):
        """递归遍历多个目录下的 *.md，yield (root, filename, full_path)。

        原本中 5 处 ``for root, _, files in os.walk(…): for f in files: if not f.endswith('.md'): continue`` 同表现。。。
        这里不加任何过滤逻辑，调用方自己判断是否跳过。
        """
        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    yield root, fname, os.path.join(root, fname)

    @staticmethod
    def _primary_domain(domain: list[str] | None) -> str:
        """取 domain[0] 作为主域子目录名，空/缺失 → 默认 ``未分类``。

        在 create / _move_bucket / archive 三处使用。sanitize_name 后才能当路径用。
        """
        return sanitize_name(domain[0]) if domain else _DEFAULT_DOMAIN_NAME

    # ---------------------------------------------------------
    # Internal: keep embedding index in sync with markdown storage
    # 内部：保证向量索引与 markdown 存储层一致
    # ---------------------------------------------------------
    def _require_embedding_available(self) -> None:
        """create()/update(content=...) 落盘前的硬性前置校验。

        embedding 是记忆系统的强制依赖，不再允许「文件已存在但向量缺失」的
        降级状态：未配置 / 未启用直接拒绝整个写操作，调用方（hold/grow/trace）
        据此向 MCP 客户端和 Dashboard 报错，不静默、不留孤儿桶。
        """
        if not self.embedding_engine or not getattr(self.embedding_engine, "enabled", False):
            raise RuntimeError(
                "embedding 未配置或未启用，拒绝写入：本系统要求向量化必须可用才能记录/修改记忆。"
                "请在设置中配置 OMBRE_EMBED_API_KEY，或用「本地向量模型」面板装好 Ollama + bge-m3。"
            )

    async def _sync_embedding(self, bucket_id: str, content: str) -> None:
        """create()/update(content=...) 调用，写入向量。

        调用前必须已经过 _require_embedding_available() 校验。这里仍然
        显式重复防御一次（未启用直接抛），失败时也不再降级吞掉——调用方
        没有 embedding 就不该被允许把内容落盘。"""
        if not self.embedding_engine or not getattr(self.embedding_engine, "enabled", False):
            raise RuntimeError("embedding 未配置或未启用，拒绝写入。")
        if not content or not content.strip():
            return
        await self.embedding_engine.generate_and_store(bucket_id, content)

    def _invalidate_bm25(self) -> None:
        """写操作后调用，标记 BM25 索引需要重建。search() 时懒触发。"""
        self._bm25_dirty = True

    # ---------------------------------------------------------
    # Create a new bucket
    # 创建新桶
    # Write content and metadata into a .md file
    # 将内容和元数据写入一个 .md 文件
    # ---------------------------------------------------------
    async def create(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        importance: int = 5,
        domain: Optional[list[str]] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        name: Optional[str] = None,
        pinned: bool = False,
        protected: bool = False,
        why_remembered: str = "",
        triggered_by: str = "",
        weight: Optional[float] = None,
        source_tool: str = "",
        grow_batch_id: str = "",
        bucket_id_override: str = "",
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.
        创建一个新的记忆桶，返回桶 ID。

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.
        pinned/protected 桶不参与合并与衰减，importance 强制锁定为 10。

        iter 2.0 来源追踪：
        - source_tool: "hold" | "grow" — 记录由哪个工具创建。feel 走 hold 分支，
          所以 feel 桶 source_tool="hold"，依靠 bucket_type 区分。
        - grow_batch_id: 同一次 grow 调用拆出的所有桶共享同一个 batch_id，
          dashboard 可按 batch 聚合显示。
        - bucket_id_override: 调用方提供的可读 id（如 feel 的
          ``feel_202605011423_V085``）。如果与已有桶冲突，自动追加秒级后缀。
          为空 → 走默认 ``generate_bucket_id()``（12 位 hex）。
        """
        # 写文件之前先校验 embedding 可用——fail-fast，不留「文件已存在但
        # 向量缺失」的孤儿桶（rule.md §1.5 的「不静默」现在延伸为「不降级」）。
        self._require_embedding_available()

        # F-04: 清洗 content / tags / name 中的危险控制字符和双向覆写符
        content = self._sanitize_text(content)
        if tags:
            tags = [self._sanitize_text(t) for t in tags]
        if name:
            name = self._sanitize_text(name)

        # --- iter 2.0: 允许调用方提供可读 bucket_id（feel 分钟级命名等）---
        # 冲突时追加 ``_<ss>``（秒），再冲突追加 ``_<2hex>`` 随机后缀，
        # 兜底用纯 UUID，保证不会无限循环。
        if bucket_id_override:
            candidate = sanitize_name(bucket_id_override) or generate_bucket_id()
            bucket_id = candidate
            if self._find_bucket_file(bucket_id):
                ss = datetime.now().strftime("%S")
                bucket_id = f"{candidate}_{ss}"
                tries = 0
                while self._find_bucket_file(bucket_id) and tries < 5:
                    bucket_id = f"{candidate}_{uuid.uuid4().hex[:2]}"
                    tries += 1
                if self._find_bucket_file(bucket_id):
                    logger.warning(
                        f"bucket_id_override '{candidate}' 反复冲突，回落到随机 id"
                    )
                    bucket_id = generate_bucket_id()
        else:
            bucket_id = generate_bucket_id()
        # 桶名 = "YYYY-MM-DD HH-MM-SS [LLM生成的标题]"，无标题时仅用时间戳。
        # 使用连字符替代冒号，避免 sanitize_name 后续编辑时把冒号去掉破坏可读性。
        # 桶名是给人看的：用 Asia/Shanghai 本地时间；元数据（created/last_active）
        # 仍由 now_iso() 以 aware UTC 写入，两者各司其职。
        from zoneinfo import ZoneInfo
        _ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H-%M-%S")
        _clean = sanitize_name(name) if name else ""
        bucket_name = (f"{_ts} {_clean}" if (_clean and _clean != "unnamed") else _ts)[:80]
        # feel buckets are allowed to have empty domain; others default to ["未分类"]
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = domain or [_DEFAULT_DOMAIN_NAME]
        tags = tags or []
        linked_content = content  # wikilink injection disabled; LLM adds [[]] via prompt

        # --- Pinned/protected buckets: lock importance to 10 ---
        # --- 钉选/保护桶：importance 强制锁定为 10 ---
        if pinned or protected:
            importance = _PINNED_IMPORTANCE

        # --- Build YAML frontmatter metadata / 构建元数据 ---
        # 越界不静默 clamp：会产生 OB-W001/OB-W002 提示走到 MCP 返回末尾
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": _clamp_unit(valence, "valence", f"create:{bucket_id}"),
            "arousal": _clamp_unit(arousal, "arousal", f"create:{bucket_id}"),
            "importance": _clamp_importance(importance, f"create:{bucket_id}"),
            "type": bucket_type,
            "created": now_iso(),
            "last_active": now_iso(),
            "activation_count": 0,
        }
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True
        if bucket_type == "permanent" or pinned:
            metadata["type"] = "permanent"

        # --- iter 2.0: 来源工具与 grow 批次 ---
        # source_tool 留空 = 调用方未声明（兼容老逻辑），不写 frontmatter。
        # grow_batch_id 仅 grow 路径会传，hold/feel 不会有这个字段。
        if source_tool:
            metadata["source_tool"] = str(source_tool).strip()[:_SOURCE_TOOL_MAX]
        if grow_batch_id:
            metadata["grow_batch_id"] = str(grow_batch_id).strip()[:_GROW_BATCH_ID_MAX]

        # --- iter 1.8: 让记忆带「为什么记得」 / why this is worth remembering ---
        # 自由文本字段。模型 / 人类手写。不参与评分，只参与展示与搜索。
        # Empty string = 没说原因，dashboard 直接不渲染该行。
        if why_remembered:
            metadata["why_remembered"] = str(why_remembered).strip()[:_WHY_REMEMBERED_MAX]
        # --- iter 1.8: feel 桶的因果链出口（暂不强校验存在性，只透传） ---
        # triggered_by = 触发这条 feel 的源 bucket_id。1.9 会做 UI 联动。
        if triggered_by:
            metadata["triggered_by"] = str(triggered_by).strip()[:_TRIGGERED_BY_MAX]
        # --- iter 1.8: plan 的「承诺重量」0.0-1.0，与 importance 不同 ---
        # importance = 这件事多重要；weight = 这件事压在我心头多重。
        if bucket_type == "plan" and weight is not None:
            metadata["weight"] = _clamp01(weight, _DEFAULT_VALENCE)
        # --- iter 1.9 G: bucket_type_defaults / 类型默认值 ---
        # config.bucket_type_defaults 里可以写 {letter: {weight: 1.0, dont_surface: false}, ...}
        # 仅在调用方未显式传该字段时套用。letter 默认 weight=1.0 体现「信件天然有重量」。
        # 老配置没这段时静默跳过。
        try:
            type_defaults = (self.config.get("bucket_type_defaults") or {}).get(bucket_type, {})
            if type_defaults:
                if "weight" in type_defaults and "weight" not in metadata and weight is None:
                    metadata["weight"] = _clamp01(type_defaults["weight"], _DEFAULT_VALENCE)
                if "dont_surface" in type_defaults and "dont_surface" not in metadata:
                    if bool(type_defaults["dont_surface"]):
                        metadata["dont_surface"] = True
                if "why_remembered" in type_defaults and not why_remembered:
                    metadata["why_remembered"] = str(type_defaults["why_remembered"]).strip()[:_WHY_REMEMBERED_MAX]
        except Exception as e:
            logger.warning(f"bucket_type_defaults apply failed / 类型默认值应用失败: {e}")
        # --- iter 1.8: 主动遗忘开关，默认 False。新桶不写 frontmatter 节省空间 ---
        # 通过 update(dont_surface=True) 后才会出现在 frontmatter 里。
        # --- iter 1.8: first_of_kind 自动判定 ---
        # 规则：当前桶的 tags 与全库已有 tags 完全无交集 → 这是一个「第一次」
        # 仅对带 tag 的桶判定。空 tag 桶不标。
        if tags:
            try:
                existing_tags = self._collect_all_tags()
                if existing_tags is not None and not (set(tags) & existing_tags):
                    metadata["first_of_kind"] = True
            except Exception as e:
                # 失败不阻塞写入主流程
                logger.warning(f"first_of_kind check failed / 首次标记检测失败: {e}")

        # --- Assemble Markdown file (frontmatter + body) ---
        # --- 组装 Markdown 文件 ---
        post = frontmatter.Post(linked_content, **metadata)  # type: ignore[arg-type]

        # --- Choose directory by type + primary domain ---
        # --- 按类型 + 主题域选择存储目录 ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        elif bucket_type == "plan":
            type_dir = self.plan_dir
        elif bucket_type == "letter":
            type_dir = self.letter_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        elif bucket_type == "plan":
            primary_domain = "active"  # plans/active/ by default; trace can move via status update
        elif bucket_type == "letter":
            primary_domain = "history"
        else:
            primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        # --- Filename: readable_name_bucketID.md (Obsidian friendly) ---
        # --- 文件名：可读名称_桶ID.md ---
        if bucket_name and bucket_name != bucket_id:
            filename = f"{bucket_name}_{bucket_id}.md"
        else:
            filename = f"{bucket_id}.md"
        file_path = safe_path(target_dir, filename)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to write bucket file / 写入桶文件失败: {file_path}: {e}")
            raise

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )

        # --- iter 2.1+: 索引/存储一致性 —— 桶落盘后立刻同步生成 embedding ---
        # _require_embedding_available() 已在函数开头校验过，这里走到的就该成功；
        # 万一调用期间 embedding 真的失败（如网络抖动），不再静默 warning——
        # 文件已经写盘，異常向上抛由调用方决定是否清理半成品文件。
        try:
            await self._sync_embedding(bucket_id, linked_content)
        except Exception:
            try:
                os.remove(file_path)
            except OSError:
                pass
            raise
        self._invalidate_bm25()
        self._record_v3_bucket_event(
            "create",
            bucket_id,
            str(metadata.get("type") or bucket_type),
            linked_content,
            metadata,
        )

        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # 读取桶内容
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        根据 ID 读取单个桶。
        F-10: 软删除的桶（带 deleted_at）对常规调用者透明，返回 None。
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        data = self._load_bucket(file_path)
        # F-10: 软删除的桶不应通过 get() 可见
        if data and data.get("metadata", {}).get("deleted_at"):
            return None
        return data

    # ---------------------------------------------------------
    # Move bucket between directories
    # 在目录间移动桶文件
    # ---------------------------------------------------------
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: Optional[list[str]] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            os.rename(file_path, new_path)
            logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
        return str(new_path)

    # ---------------------------------------------------------
    # Update bucket
    # 更新桶
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    async def update(self, bucket_id: str, **kwargs) -> bool:
        """
        Update bucket content or metadata fields.
        更新桶的内容或元数据字段。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # content 改动会触发 embedding 重新生成（见下方 _sync_embedding 调用），
        # 同样要求 fail-fast：embedding 不可用就拒绝，不碰文件（不写入半新半旧状态）。
        if "content" in kwargs:
            self._require_embedding_available()

        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
            return False

        # --- Pinned/protected buckets: lock importance to 10, ignore importance changes ---
        # --- 钉选/保护桶：importance 不可修改，强制保持 10 ---
        was_pinned = bool(post.get("pinned", False))
        is_pinned = post.get("pinned", False) or post.get("protected", False)
        if is_pinned:
            kwargs.pop("importance", None)  # silently ignore importance update

        # --- Update only fields that were passed in / 只改传入的字段 ---
        if "content" in kwargs:
            post.content = kwargs["content"]  # wikilink injection disabled; LLM adds [[]] via prompt
        if "tags" in kwargs:
            post["tags"] = kwargs["tags"]
        if "importance" in kwargs:
            post["importance"] = _clamp_importance(kwargs["importance"], f"update:{bucket_id}")
        if "domain" in kwargs:
            post["domain"] = kwargs["domain"]
        if "valence" in kwargs:
            post["valence"] = _clamp_unit(kwargs["valence"], "valence", f"update:{bucket_id}")
        if "arousal" in kwargs:
            post["arousal"] = _clamp_unit(kwargs["arousal"], "arousal", f"update:{bucket_id}")
        if "name" in kwargs:
            post["name"] = sanitize_name(kwargs["name"])
        if "resolved" in kwargs:
            post["resolved"] = bool(kwargs["resolved"])
        if "pinned" in kwargs:
            post["pinned"] = bool(kwargs["pinned"])
            if kwargs["pinned"]:
                post["importance"] = _PINNED_IMPORTANCE  # pinned → lock importance to 10
                post.metadata.pop("anchor", None)  # pinned 与 anchor 互斥：钉为核心准则即清除坐标系标记
        if "digested" in kwargs:
            post["digested"] = bool(kwargs["digested"])
        if "model_valence" in kwargs:
            post["model_valence"] = _clamp01(kwargs["model_valence"], _DEFAULT_VALENCE)
        # --- Pass-through fields for plan/letter lifecycle ---
        # --- plan/letter/iter1.7 生命周期相关字段直接透传到 frontmatter ---
        # 这一组字段没有「校验/转换」逻辑，给什么写什么。新增字段往这个元组里加即可。
        # iter 1.7 §G3 在这里加入了 "change_log"——plan 桶的状态/编辑历史 list[dict]，
        # 由 server.py 的 plan() / trace() / /api/plans/{id}/action 维护，bucket_manager 不参与生成。
        for k in ("status", "type", "resolution_reason", "resolved_by",
                  "related_bucket", "author", "user_name", "title", "letter_date",
                  "change_log",
                  # iter 1.8 新增字段。除 weight 外全部透传不转换。
                  # weight 在 plan 上才有意义；这里不在这个循环里校验类型，由上层 server.py 保证传入范围。
                  "why_remembered", "dont_surface", "first_of_kind",
                  "weight", "triggered_by",
                  # iter 2.0 新增 anchor。bool 字段，不参与评分，硬上限 24。
                  # 上限校验在下面 anchor 分支里做（False→True 切换时计数），
                  # set_anchor() 仍是首选入口，update() 只是兜底兼容批量迁移脚本。
                  "anchor",
                  # iter 2.0 来源追踪字段：
                  # source_tool / grow_batch_id 一般在 create() 时定型，
                  # 这里的透传只服务于迁移脚本（给历史桶补字段）。
                  # last_merged_by 由 _common.merge_or_create 在 merge 后写入，
                  # 表示「最后一次合并是 hold 还是 grow 触发的」。
                  # _pre_anchor_source_tool 是 anchor 时保存的原始 source_tool，
                  # release 时自动恢复；None 表示删除该字段。
                  "source_tool", "grow_batch_id", "last_merged_by", "_pre_anchor_source_tool"):
            if k in kwargs:
                if k == "weight" and kwargs[k] is not None:
                    post[k] = _clamp01(kwargs[k], _DEFAULT_VALENCE)
                elif k == "dont_surface":
                    post[k] = bool(kwargs[k])
                elif k == "first_of_kind":
                    post[k] = bool(kwargs[k])
                elif k == "anchor":
                    # iter 2.0: anchor 是布尔；False 时直接删除字段保持 frontmatter 干净。
                    # 修复：透传路径之前会绕过 ANCHOR_LIMIT，导致批量脚本/前端直接 update(anchor=True)
                    # 可以让 anchor 总数突破 24 上限。这里补一道校验：
                    # 仅当从 False→True 切换时才计数；当前已是 anchor 的桶重复设置不计数。
                    if bool(kwargs[k]):
                        already_anchor = bool(post.get("anchor", False))
                        if not already_anchor:
                            # FIX (RED-02): count_anchors 是 async，必须 await，否则
                            # `coroutine >= int` 会 TypeError，整个上限校验失效。
                            current = await self.count_anchors()
                            if current >= self.ANCHOR_LIMIT:
                                logger.warning(
                                    f"update() 拒绝 anchor=True：已达上限 "
                                    f"{self.ANCHOR_LIMIT}（当前 {current}）。bucket={bucket_id}"
                                )
                                return False
                        post["anchor"] = True
                    else:
                        post.metadata.pop("anchor", None)
                else:
                    if kwargs[k] is None:
                        # None = 明确删除该 frontmatter 字段（用于 anchor release 清理临时字段）
                        post.metadata.pop(k, None)
                    else:
                        post[k] = kwargs[k]

        # --- Auto-refresh activation time / 自动刷新激活时间 ---
        post["last_active"] = now_iso()

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to write bucket update / 写入桶更新失败: {file_path}: {e}")
            return False

        # --- Auto-move: pinned → permanent/ ---
        # --- 自动移动：钉选 → permanent/ ---
        # NOTE: resolved buckets are NOT auto-archived here.
        # They stay in dynamic/ and decay naturally until score < threshold.
        # 注意：resolved 桶不在此自动归档，留在 dynamic/ 随衰减引擎自然归档。
        domain: list[str] = post.get("domain") or ["未分类"]  # type: ignore[assignment]
        if kwargs.get("pinned") and post.get("type") != "permanent":
            post["type"] = "permanent"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            self._move_bucket(file_path, self.permanent_dir, domain)
        # --- Reverse: unpin → demote only buckets that were actually pinned.
        # `type=permanent` is also a first-class bucket type, so an idempotent
        # pinned=False update must not move explicit permanent memories.
        elif (
            "pinned" in kwargs
            and not kwargs.get("pinned")
            and was_pinned
            and not post.get("protected")
            and post.get("type") == "permanent"
        ):
            post["type"] = "dynamic"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            self._move_bucket(file_path, self.dynamic_dir, domain)

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")

        # --- iter 2.1+: content 改动 → 同步刷新 embedding ---
        # 函数开头已校验过 embedding 可用；这里仍可能因调用瞬间网络/限流失败，
        # 失败不再静默吞掉——异常向上抛，调用方（trace 等）据此向用户报错。
        # 注意：文件内容此时已落盘，调用方需要知道这是「半失败」状态。
        if "content" in kwargs:
            await self._sync_embedding(bucket_id, post.content or "")
        self._invalidate_bm25()
        self._record_v3_bucket_event(
            "update",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )

        return True

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # 自动添加 Obsidian 双链 — 已禁用
    # Now handled by LLM prompts (Gemini adds [[]] for proper nouns)
    # 现在由 LLM prompt 处理（Gemini 对人名/地名/专有名词加 [[]]）
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Delete bucket
    # 删除桶
    # ---------------------------------------------------------
    async def delete(self, bucket_id: str) -> bool:
        """
        Soft-delete a memory bucket: move to archive/ and stamp `deleted_at`.
        F-10: 记忆不消失，只是淡去。不做物理删除，将文件移入 archive/
        并在 frontmatter 中写入 deleted_at 时间戳；embedding 仍清理以节省空间。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # --- 读取文件，写入 deleted_at，移入 archive/ ---
        try:
            post = frontmatter.load(file_path)
            post["deleted_at"] = now_iso()
            os.makedirs(self.archive_dir, exist_ok=True)
            dest = os.path.join(self.archive_dir, os.path.basename(file_path))
            # 若 archive/ 里已有同名文件（极罕见），追加 bucket_id 后缀避免覆盖
            if os.path.exists(dest) and dest != file_path:
                dest = os.path.join(
                    self.archive_dir,
                    f"{os.path.splitext(os.path.basename(file_path))[0]}_{bucket_id}.md",
                )
            with open(dest, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            if dest != file_path:
                os.remove(file_path)
        except OSError as e:
            logger.error(f"Failed to soft-delete bucket / 软删除桶文件失败: {file_path}: {e}")
            return False

        # iter 1.6 §4：仍清理 embedding，避免孤儿向量占用空间
        if self.embedding_engine is not None:
            try:
                self.embedding_engine.delete_embedding(bucket_id)
            except Exception as e:
                logger.warning(f"delete embedding failed for {bucket_id}: {e}")

        self._invalidate_bm25()
        logger.info(f"Soft-deleted bucket (moved to archive) / 软删除记忆桶: {bucket_id}")
        self._record_v3_bucket_event(
            "delete",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # Touch bucket (refresh activation time + increment count)
    # 触碰桶（刷新激活时间 + 累加激活次数）
    # Called on every recall hit; affects decay score.
    # 每次检索命中时调用，影响衰减得分。
    # ---------------------------------------------------------
    async def touch(self, bucket_id: str) -> None:
        """
        Update a bucket's last activation time and count.
        Also triggers time ripple: nearby memories get a slight activation boost.
        更新桶的最后激活时间和激活次数。
        同时触发时间涟漪：时间上相邻的记忆轻微唤醒。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return

        try:
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()
            post["activation_count"] = int(post.get("activation_count") or 0) + 1  # type: ignore[call-overload]

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # --- Time ripple: boost nearby memories within ±48h ---
            # --- 时间涟漪：±48小时内的记忆轻微唤醒 ---
            current_time = parse_iso_aware(str(post.get("created", post.get("last_active", ""))))
            await self._time_ripple(bucket_id, current_time)
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")

    async def _time_ripple(self, source_id: str, reference_time: datetime, hours: float = _RIPPLE_HOURS) -> None:
        """
        Slightly boost activation_count of buckets created/activated near the reference time.
        轻微提升时间相邻桶的激活次数（+0.3），不改 last_active 避免递归唤醒。
        Max 5 buckets rippled per touch to bound I/O.
        """
        try:
            all_buckets = await self.list_all(include_archive=False)
        except Exception:
            return

        rippled = 0
        for bucket in all_buckets:
            if rippled >= _RIPPLE_MAX_BUCKETS:
                break
            if bucket["id"] == source_id:
                continue
            meta = bucket.get("metadata", {})
            # Skip pinned/permanent/feel
            if meta.get("pinned") or meta.get("protected") or meta.get("type") in ("permanent", "feel"):
                continue

            created_str = meta.get("created", meta.get("last_active", ""))
            try:
                created = parse_iso_aware(str(created_str))
                delta_hours = abs((reference_time - created).total_seconds()) / 3600
            except (ValueError, TypeError):
                continue

            if delta_hours <= hours:
                # Boost activation_count by _RIPPLE_BOOST (fractional), don't change last_active
                file_path = self._find_bucket_file(bucket["id"])
                if not file_path:
                    continue
                try:
                    post = frontmatter.load(file_path)
                    current_count = float(post.get("activation_count") or 0)  # type: ignore[arg-type]
                    # Store as float for fractional increments; calculate_score handles it
                    post["activation_count"] = round(current_count + _RIPPLE_BOOST, 1)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(post))
                    rippled += 1
                except Exception as _ripple_exc:
                    logger.warning(
                        f"[ripple] Failed to update activation_count for {bucket['id']!r}: "
                        f"{type(_ripple_exc).__name__}: {_ripple_exc}"
                    )
                    continue

    # ---------------------------------------------------------
    # Multi-dimensional search (core feature)
    # 多维搜索（核心功能）
    #
    # Strategy: domain pre-filter → weighted multi-dim ranking
    # 策略：主题域预筛 → 多维加权精排
    #
    # Ranking formula:
    #   total = topic(×w_topic) + emotion(×w_emotion)
    #           + time(×w_time) + importance(×w_importance)
    #
    # Per-dimension scores (normalized to 0~1):
    #   topic     = rapidfuzz weighted match (name/tags/domain/body)
    #   emotion   = 1 - Euclidean distance (query v/a vs bucket v/a)
    #   time      = e^(-0.02 × days) (recent memories first)
    #   importance = importance / 10
    # ---------------------------------------------------------
    async def search(
        self,
        query: str,
        limit: Optional[int] = None,
        domain_filter: Optional[list[str]] = None,
        query_valence: Optional[float] = None,
        query_arousal: Optional[float] = None,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.
        多维索引搜索记忆桶。

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if not query or not query.strip():
            return []

        limit = limit or self.max_results
        # 字面召回：把查询原样（小写、去空白）留作子串匹配，保证显式搜的词必被召回
        q_norm = query.strip().lower()
        all_buckets = await self.list_all(include_archive=False)

        if not all_buckets:
            return []

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            # 预筛为空则回退全量搜索
            if not candidates:
                candidates = all_buckets
        else:
            candidates = all_buckets

        # --- Layer 1.5: embedding 语义分数（仅作为打分维度，不再窄化候选集）---
        # 历史上这里把候选集替换成「在 embeddings.db 里的桶」，导致：
        #   - 任何缺少 embedding 的桶（落盘时 embed key 失败 / 旧脚本批量导入未补向量）
        #     只要查询命中过任意向量，就会被整体过滤掉 → breath 检索数对不上 pulse。
        # 修复：保留 vector_scores 给 Layer 2 的 semantic 维度用，但不动 candidates。
        # 没 embedding 的桶 semantic_score=0，仍可凭 topic/emotion/time/importance 命中。
        vector_scores: dict[str, float] = {}
        if self.embedding_engine and self.embedding_engine.enabled:
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=_VECTOR_TOPK)
                if vector_results:
                    vector_scores = {bid: score for bid, score in vector_results}
            except Exception as e:
                logger.warning(f"Embedding score failed, using fuzzy only / embedding 评分失败: {e}")

        # --- BM25 懒重建 ---
        # all_buckets 已在上方加载，直接复用；写操作后脏标记触发重建
        if self._bm25 is not None and self._bm25_dirty:
            self._bm25.build(all_buckets)
            self._bm25_dirty = False
        bm25_scores: dict[str, float] = self._bm25.score(query) if self._bm25 is not None else {}

        # --- Layer 2: weighted multi-dim ranking ---
        # --- 第二层：多维加权精排 ---
        scored = []
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # 字面命中：查询串原样出现在 name/tags/domain/正文 → 召回保障 + 排序加分
                literal_hit = False
                if q_norm:
                    hay = " ".join([
                        str(meta.get("name", "")),
                        " ".join(str(t) for t in (meta.get("tags") or [])),
                        " ".join(str(d) for d in (meta.get("domain") or [])),
                        bucket.get("content", "") or "",
                    ]).lower()
                    literal_hit = q_norm in hay

                # Dim 1: topic relevance (fuzzy text, 0~1)
                topic_score = self._calc_topic_score(query, bucket)

                # Dim 2: emotion resonance (coordinate distance, 0~1)
                emotion_score = self._calc_emotion_score(
                    query_valence, query_arousal, meta
                )

                # Dim 3: time proximity (exponential decay, 0~1)
                time_score = self._calc_time_score(meta)

                # Dim 4: importance (direct normalization)
                importance_score = max(1, min(10, int(meta.get("importance") or 5))) / 10.0

                # Dim 5: touch frequency (召回频率, 0~1) — iter 2.1
                touch_score = self._calc_touch_score(meta)

                # --- Weighted sum / 加权求和 ---
                total = (
                    topic_score * self.w_topic
                    + emotion_score * self.w_emotion
                    + time_score * self.w_time
                    + importance_score * self.w_importance
                    + touch_score * self.w_touch
                )
                weight_sum = (
                    self.w_topic + self.w_emotion + self.w_time
                    + self.w_importance + self.w_touch
                )
                # Dim 6: semantic similarity — only when embedding is available (iter 2.1)
                # 仅 embedding 可用时加入语义相似度维度；不可用时不影响 weight_sum 平衡
                if vector_scores:
                    semantic_score = vector_scores.get(bucket["id"], 0.0)
                    total += semantic_score * self.w_semantic
                    weight_sum += self.w_semantic
                # Dim 7: BM25 TF-IDF 关键词分（rank_bm25+jieba，软依赖，缺包时 bm25_scores={}）
                if bm25_scores:
                    total += bm25_scores.get(bucket["id"], 0.0) * self.w_bm25
                    weight_sum += self.w_bm25
                # Normalize to 0~100 for readability
                normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0

                # 字面命中加分 + 召回保障：修复短查询（如 2 字"杭州"）即使正文里有也
                # 因加权分被各维度稀释到 fuzzy_threshold 以下而整条搜不到。
                # 用户显式搜的词必须召回，故 literal_hit 直接放行（OR），并给排序加分。
                if literal_hit:
                    normalized = min(100.0, normalized + _LITERAL_MATCH_BONUS)

                # Threshold check uses raw (pre-penalty) score so resolved buckets
                # 阈值用原始分数判定，确保 resolved 桶在关键词命中时仍可被搜出
                # remain reachable by keyword (penalty applied only to ranking).
                if normalized >= self.fuzzy_threshold or literal_hit:
                    # Resolved buckets get ranking penalty (but still reachable by keyword)
                    # 已解决的桶仅在排序时降权
                    if meta.get("resolved", False):
                        normalized *= _RESOLVED_RANK_PENALTY
                    bucket["score"] = round(normalized, 2)
                    scored.append(bucket)
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    # ---------------------------------------------------------
    # 四个评分维度的纯函数实现已拆到 bucket_scoring.py；这里保留同名
    # wrapper 方法 —— 测试和历史调用方一直用 bucket_mgr._calc_xxx_score(...)
    # 这种实例方法写法，wrapper 保持该接口不变，同时让实现本身可独立单测/复用。
    # ---------------------------------------------------------
    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        return calc_topic_score(query, bucket, content_weight=self.content_weight)

    def _calc_emotion_score(
        self, q_valence: Optional[float], q_arousal: Optional[float], meta: dict
    ) -> float:
        return calc_emotion_score(q_valence, q_arousal, meta)

    def _calc_time_score(self, meta: dict) -> float:
        return calc_time_score(meta)

    def _calc_touch_score(self, meta: dict) -> float:
        return calc_touch_score(meta)

    # ---------------------------------------------------------
    # iter 2.0: anchor 系统（坐标系桶，硬上限 24）
    # anchor system — coordinate-system buckets, hard cap of 24
    # ---------------------------------------------------------
    ANCHOR_LIMIT = 24

    async def count_anchors(self) -> int:
        """Return current count of buckets with anchor=True."""
        # 用 list_all 数；规模小（最多 24）所以扫描成本可忽略。
        all_b = await self.list_all(include_archive=False)
        return sum(1 for b in all_b if b.get("metadata", {}).get("anchor"))

    async def set_anchor(self, bucket_id: str, value: bool) -> dict:
        """
        Toggle the anchor flag on a bucket. Hard-rejects if cap reached.
        切换桶的 anchor 标记。设为 True 且当前已满 24 时拒绝。

        Returns: {"ok": bool, "anchor": bool, "count": int, "limit": int, "error": Optional[str]}
        """
        bucket = await self.get(bucket_id)
        if not bucket:
            return {"ok": False, "error": "bucket not found", "count": 0, "limit": self.ANCHOR_LIMIT}
        current_value = bool(bucket["metadata"].get("anchor", False))
        target = bool(value)
        # Idempotent: same state → noop
        if current_value == target:
            count = await self.count_anchors()
            return {"ok": True, "anchor": target, "count": count, "limit": self.ANCHOR_LIMIT, "noop": True}
        if target is True:
            # pinned/protected 与 anchor 互斥：pinned=永远置顶浮现（核心准则），
            # anchor=刻意不浮现（坐标系），两者语义直接矛盾。允许并存会让一个
            # pinned+anchor 桶每会话都以「核心准则」冒头，诱导模型反复 release
            # 却压不住它。这里直接拒绝，提示先 trace(pinned=0) 再改坐标系。
            if bucket["metadata"].get("pinned") or bucket["metadata"].get("protected"):
                return {
                    "ok": False,
                    "error": "这是 pinned 核心准则，不能同时设为 anchor（两者互斥）。要改成坐标系请先 trace(pinned=0)。",
                    "count": await self.count_anchors(),
                    "limit": self.ANCHOR_LIMIT,
                }
            count = await self.count_anchors()
            if count >= self.ANCHOR_LIMIT:
                return {
                    "ok": False,
                    "error": f"anchor 已达上限 {self.ANCHOR_LIMIT}。请先 release 一条再 anchor 新的。",
                    "count": count,
                    "limit": self.ANCHOR_LIMIT,
                }
        # iter 2.0：钉为 anchor 时同步把 source_tool 改为 "anchor"，
        # 释放时恢复为原始来源（保存在 _pre_anchor_source_tool 里）。
        # 这样 dashboard 「按来源筛选」能正确反映桶的当前状态。
        update_kwargs: dict = {"anchor": target}
        bucket_meta = bucket.get("metadata", {})
        if target:
            # 先把当前 source_tool 存为 _pre_anchor_source_tool，再覆写为 "anchor"
            original = bucket_meta.get("source_tool", "")
            update_kwargs["_pre_anchor_source_tool"] = original
            update_kwargs["source_tool"] = "anchor"
        else:
            # 释放：恢复原始 source_tool，清掉临时字段
            original = bucket_meta.get("_pre_anchor_source_tool", "")
            update_kwargs["source_tool"] = original
            update_kwargs["_pre_anchor_source_tool"] = None  # 删除字段
        ok = await self.update(bucket_id, **update_kwargs)
        if not ok:
            return {"ok": False, "error": "update failed", "count": 0, "limit": self.ANCHOR_LIMIT}
        new_count = await self.count_anchors()
        return {"ok": True, "anchor": target, "count": new_count, "limit": self.ANCHOR_LIMIT}

    async def list_anchors(self) -> list[dict]:
        """Return all buckets with anchor=True, sorted by created ascending."""
        all_b = await self.list_all(include_archive=False)
        anchors = [b for b in all_b if b.get("metadata", {}).get("anchor")]
        anchors.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        return anchors

    # ---------------------------------------------------------
    # List all buckets
    # 列出所有桶
    # ---------------------------------------------------------
    async def get_triggered_feels(self, source_bucket_id: str) -> list[dict]:
        """
        Return all feel buckets whose triggered_by == source_bucket_id.
        只扫 feel_dir，O(feel桶数) 而非 O(全库)。iter 2.0 §10 U-04 优化反向链查询。
        每条返回 {id, name, created}。
        """
        results = []
        for _root, _fname, file_path in self._iter_md_files([self.feel_dir]):
            bucket = self._load_bucket(file_path)
            if not bucket:
                continue
            meta = bucket.get("metadata", {})
            if meta.get("triggered_by") == source_bucket_id:
                results.append({
                    "id": bucket["id"],
                    "name": meta.get("name") or bucket["id"],
                    "created": meta.get("created", ""),
                })
        results.sort(key=lambda x: x.get("created", ""), reverse=True)
        return results

    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        递归遍历目录（含域子目录），列出所有记忆桶。
        """
        buckets = []
        dirs = list(self._active_dirs)
        if include_archive:
            dirs.append(self.archive_dir)

        for _root, _fname, file_path in self._iter_md_files(dirs):
            bucket = self._load_bucket(file_path)
            if bucket:
                buckets.append(bucket)

        return buckets

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # 统计信息（各分类桶数量 + 总体积）
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        返回记忆桶的统计数据。
        """
        stats: dict[str, Any] = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "plan_count": 0,
            "letter_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
            (self.plan_dir, "plan_count"),
            (self.letter_dir, "letter_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts / 每个域的桶数量
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # 归档桶（从 permanent/dynamic 移入 archive）
    # Called by decay engine to simulate "forgetting"
    # 由衰减引擎调用，模拟"遗忘"
    # ---------------------------------------------------------
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        将指定桶移入归档目录（保留域子目录结构）。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            # Read once, get domain info and update type / 一次性读取
            post = frontmatter.load(file_path)
            domain: list[str] = post.get("domain") or [_DEFAULT_DOMAIN_NAME]  # type: ignore[assignment]
            primary_domain = self._primary_domain(domain)
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)

            dest = safe_path(archive_subdir, os.path.basename(file_path))

            # Update type marker then move file / 更新类型标记后移动文件
            post["type"] = "archived"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # Use shutil.move for cross-filesystem safety
            # 使用 shutil.move 保证跨文件系统安全
            shutil.move(file_path, str(dest))
        except Exception as e:
            logger.error(
                f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
            )
            return False

        self._invalidate_bm25()
        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        self._record_v3_bucket_event(
            "archive",
            bucket_id,
            str(post.get("type") or "archived"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # iter 1.8: 收集全库已有 tag 集合，用于 first_of_kind 检测
    # Collect all tags currently in the vault (excluding archive)
    # 返回 set[str]；空 vault 返回空 set；遇异常返回 None 提示调用方放弃
    # ---------------------------------------------------------
    def _collect_all_tags(self) -> Optional[set]:
        tags = set()
        # 不包括 archive：归档桶代表“过去”，不应阻止“第一次”判定
        # archive_dir is excluded — archived buckets are "the past", they
        # shouldn't block a tag from being marked first_of_kind today.
        for _root, _fname, full_path in self._iter_md_files(self._active_dirs):
            try:
                post = frontmatter.load(full_path)
                for t in list(post.get("tags") or []):  # type: ignore[call-overload]
                    if t:
                        tags.add(str(t))
            except Exception:
                # 单个桶解析失败不影响整体；first_of_kind 是软特性
                continue
        return tags

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # 内部：在三个目录中查找桶文件
    # ---------------------------------------------------------
    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.
        在 permanent/dynamic/archive 中递归查找指定 ID 的桶文件。
        """
        if not bucket_id:
            return None
        # 含 archive：软删除后的桶仍然需要可被内部路径查找。
        dirs = [self.permanent_dir, self.dynamic_dir, self.archive_dir,
                self.feel_dir, self.plan_dir, self.letter_dir]
        candidates = []
        for _root, fname, full_path in self._iter_md_files(dirs):
            name_part = fname[:-3]  # remove .md
            if name_part == bucket_id or name_part.endswith(f"_{bucket_id}"):
                return full_path
            candidates.append(full_path)
        # Fallback: check frontmatter id field (for imported files where filename ≠ id)
        for full_path in candidates:
            try:
                post = frontmatter.load(full_path)
                if post.get("id") == bucket_id:
                    return full_path
            except Exception:
                pass
        return None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # 内部：从 .md 文件加载桶数据
    # ---------------------------------------------------------
    @staticmethod
    def _sanitize_text(text: str) -> str:
        """F-04 fix: 清除 NUL、危险控制字符和双向覆写符（Unicode bidi override / isolate）。

        保留 \\n（LF）、\\r（CR）、\\t（Tab）。
        清除范围：
          U+0000~U+0008, U+000B, U+000C, U+000E~U+001F, U+007F（C0/C1 控制字符）
          U+202A~U+202E 双向控制符（LRE / RLE / PDF / LRO / RLO）
          U+2066~U+2069 双向隔离符（LRI / RLI / FSI / PDI）
        Emoji 与 CJK 不受影响。
        """
        _ctrl_table = {
            c: None
            for c in list(range(0x00, 0x09))    # 0x00..0x08
            + [0x0B, 0x0C]                       # VT, FF
            + list(range(0x0E, 0x20))            # 0x0E..0x1F
            + [0x7F]                             # DEL
            + list(range(0x202A, 0x202F))        # bidi controls 0x202A..0x202E
            + list(range(0x2066, 0x206A))        # bidi isolates 0x2066..0x2069
        }
        return str(text).translate(_ctrl_table)

    @staticmethod
    def _sanitize_float_field(value, default: float) -> float:
        """从任意格式提取 float（兼容 'V0.9'、'[我的视角:V0.3]'、0.9 等老格式）"""
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
        try:
            nums = re.findall(r'[-+]?\d*\.?\d+', str(value))
            return max(0.0, min(1.0, float(nums[0]))) if nums else default
        except Exception:
            return default

    @classmethod
    def _normalize_metadata_value(cls, value):
        """Return JSON-safe metadata values from YAML frontmatter reads."""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: cls._normalize_metadata_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._normalize_metadata_value(v) for v in value]
        return value

    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        解析 Markdown 文件，返回桶的结构化数据。
        """
        try:
            post = frontmatter.load(file_path)
            metadata = {
                key: self._normalize_metadata_value(value)
                for key, value in dict(post.metadata).items()
            }
            domain_value = metadata.get("domain")
            if isinstance(domain_value, str):
                metadata["domain"] = [domain_value] if domain_value.strip() else []
            elif domain_value is None:
                metadata["domain"] = []
            elif not isinstance(domain_value, list):
                metadata["domain"] = list(domain_value) if isinstance(domain_value, tuple) else [str(domain_value)]
            # 兼容老桶可能存储了 'V0.9'、'[我的视角:V0.3]' 等字符串格式
            for field, default in (("valence", 0.5), ("arousal", 0.3)):
                if field in metadata:
                    metadata[field] = self._sanitize_float_field(metadata[field], default)
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": metadata,
                "content": post.content,
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None
