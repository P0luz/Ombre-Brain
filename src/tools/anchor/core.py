"""
========================================
tools/anchor/core.py — anchor / release / pulse 实现
========================================

anchor 是 iter 2.0 引入的「坐标系桶」概念：把某条已经存在的桶钉为
我们关系/身份的基准点。它不会主动浮现在默认 breath，但 query/domain/
emotion/importance_min 命中时仍能返回。硬上限 24 个。

pulse 顺带放在这里：它是系统状态 + 桶清单的总览，调用频次低，把它
塞进一个文件不影响阅读。

关键行为：
- anchor_set / anchor_release：调 bucket_mgr.set_anchor，原样转译结果
- pulse：聚合 stats + list_all，按 type 分组（normal/feel/plan/letter）
  逐行展示 icon + 主题 + 情感 + 权重 + 标签
- pulse 同时附带「索引漂移」自检：embedding.db 的 ID 集合与磁盘桶 ID 集合
  对账，缺失/孤儿 > 0 时在状态块顶部告警，提示运行 backfill / clean 脚本

不做什么（边界）：
- anchor 没有「创建快捷键」：必须先 hold() 写下，确认是坐标系再钉
- pulse 不做 dehydrate：只读元数据，避免大开销

对外暴露：anchor_set(bucket_id) / anchor_release(bucket_id) /
         pulse(include_archive) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .._common import check_metadata_size
from ..plan.core import is_letter_bucket, letter_lock_state
from utils import parse_bool
from errors import safe_error_detail


async def anchor_set(bucket_id: str) -> str:
    # 24 这个数字没什么道理，就是觉得再多就不叫锚了。
    # 人能同时抓住的东西本来也没那么多。
    bucket_id = "" if bucket_id is None else str(bucket_id)
    metadata_err = check_metadata_size(bucket_id=bucket_id)
    if metadata_err:
        return metadata_err
    if rt.mark_op:
        rt.mark_op("anchor")
    result = await rt.bucket_mgr.set_anchor(bucket_id, True)
    if not result["ok"]:
        return f"我没能把它锚住：{result.get('error', '未知错误')}。当前 anchor: {result.get('count', '?')}/{result.get('limit', 24)}。"
    if result.get("noop"):
        return f"它已经是 anchor 了。当前 {result['count']}/{result['limit']}。"
    return f"我把它放进 anchor 了。它现在是坐标系的一部分，不会被默认浮现挤进上下文。当前 {result['count']}/{result['limit']}。"


async def anchor_release(bucket_id: str) -> str:
    bucket_id = "" if bucket_id is None else str(bucket_id)
    metadata_err = check_metadata_size(bucket_id=bucket_id)
    if metadata_err:
        return metadata_err
    if rt.mark_op:
        rt.mark_op("release")
    result = await rt.bucket_mgr.set_anchor(bucket_id, False)
    if not result["ok"]:
        return f"我没能把它移开：{result.get('error', '未知错误')}。"
    if result.get("noop"):
        return f"它本来就不是 anchor。当前 {result['count']}/{result['limit']}。"
    return f"我把它从 anchor 移开了。它会重新参与默认浮现。当前 {result['count']}/{result['limit']}。"


def _organize_progress(buckets: list[dict]) -> str:
    """整理进度行（3.7.0）。

    「引路人」：把 digested 当作「已和小深一起整理过」的记号。整理完一条就用
    trace(bucket_id, digested=1) 标记，标记后它不再自动浮现在 breath —— 所以
    breath 里浮现的自然就是「还没整理的」。这里只统计活跃记忆桶
    （dynamic/permanent），feel/plan/letter/i 各自成层，不混进来。
    """
    _NON_MEMORY = {
        "plan", "feel", "letter", "i", "i_candidate", "identity", "archived",
    }
    memory = [
        b for b in buckets
        if (b.get("metadata") or {}).get("type") not in _NON_MEMORY
    ]
    done = sum(
        1 for b in memory
        if parse_bool((b.get("metadata") or {}).get("digested"), default=False)
    )
    total = len(memory)
    if not total:
        return ""
    filled = round(done / total * 20)
    bar = "█" * filled + "░" * (20 - filled)
    return f"整理进度: {bar} 已整理 {done} / 未整理 {total - done}"


async def pulse(include_archive: Optional[bool] = False) -> str:
    if include_archive is None:
        include_archive = False
    await rt.decay_engine.ensure_started()
    try:
        stats = await rt.bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {safe_error_detail(e)}"

    progress_line = ""
    try:
        _active = await rt.bucket_mgr.list_all(include_archive=False)
        _pl = _organize_progress(_active)
        if _pl:
            progress_line = _pl + "\n"
    except Exception:
        progress_line = ""

    status = (
        f"=== 我现在的记忆 ===\n"
        f"固化桶: {stats['permanent_count']} 个\n"
        f"动态桶: {stats['dynamic_count']} 个\n"
        f"{progress_line}"
        f"归档桶: {stats['archive_count']} 个\n"
        f"feel 桶: {stats.get('feel_count', 0)} 条\n"
        f"plan 桶: {stats.get('plan_count', 0)} 条\n"
        f"letter 桶: {stats.get('letter_count', 0)} 封\n"
        f"总占用: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if rt.decay_engine.is_running else '已停止'}\n"
    )

    # --- 索引/存储一致性检查（iter 2.1+）---
    # 桶文件落在磁盘但 embedding 缺失 → breath 走向量检索时会丢这些桶；
    # 反之孤儿 embedding 不影响检索，但占空间。两边一旦对不上就在 pulse 里告警，
    # 让她/他/模型立刻知道「数对不上是真 bug」而不是错觉。
    try:
        ee = getattr(rt, "embedding_engine", None)
        outbox = getattr(rt.bucket_mgr, "embedding_outbox", None)
        pending_ids = outbox.pending_ids() if outbox is not None else set()
        if outbox is not None:
            queue_state = outbox.status()
            circuit = queue_state.get("circuit") or {}
            status += (
                f"向量索引队列: 待处理 {queue_state['pending']} 个"
                f"（重试中 {queue_state['retrying']} 个）"
                + (
                    f"，供应商熔断中（连续失败 "
                    f"{circuit.get('consecutive_failures', 0)} 次）"
                    if circuit.get("state") == "open" else ""
                )
                + "\n"
            )
        if ee and getattr(ee, "enabled", False):
            disk_buckets = await rt.bucket_mgr.list_all(include_archive=True)
            disk_ids = {
                b["id"] for b in disk_buckets
                if not (b.get("metadata") or {}).get("deleted_at")
                and str(b.get("content") or "").strip()
            }
            index_ids = set(ee.list_all_ids())
            missing = disk_ids - index_ids - pending_ids
            orphan = index_ids - disk_ids
            if missing or orphan:
                status += (
                    f"⚠️ 索引漂移：缺失 embedding {len(missing)} 个 / "
                    f"孤儿 embedding {len(orphan)} 个 "
                    f"（缺失项可在 Dashboard 触发补齐；孤儿项可运行 "
                    f"tools/clean_orphan_embeddings.py 清理）\n"
                )
    except Exception as e:
        rt.logger.warning(f"pulse index/storage drift check failed: {e}")

    try:
        buckets = await rt.bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    normal_lines: list[str] = []
    feel_lines: list[str] = []
    plan_lines: list[str] = []
    letter_lines: list[str] = []
    for b in buckets:
        meta = b.get("metadata", {})
        btype = meta.get("type")
        logical_letter = is_letter_bucket(b)
        letter_locked = (
            logical_letter and letter_lock_state(b, "ai")["locked"]
        )
        if letter_locked:
            icon = "🔒"
        elif logical_letter:
            icon = "💌"
        elif meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif btype == "permanent":
            icon = "📦"
        elif btype == "feel":
            icon = "🫧"
        elif btype == "plan":
            icon = "📋"
        elif btype == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = rt.decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = (
            "letter" if letter_locked else ",".join(meta.get("domain", []))
        )
        val = float(meta.get("valence") or 0.5)
        aro = float(meta.get("arousal") or 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        name = (
            "一封上锁的信"
            if letter_locked else meta.get("name", "") or ""
        )
        name_tag = f" 《{name}》" if name and name != b["id"] else ""
        anchor_tag = (
            " ⚓ [anchor]"
            if parse_bool(meta.get("anchor"), default=False)
            else ""
        )
        line = (
            f"{icon} [{b['id']}]{anchor_tag}{name_tag}{resolved_tag} "
            f"主题:{domains or '未分类'} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f}"
        )
        tags = [] if letter_locked else [
            t for t in (meta.get("tags", []) or [])
            if not (t.startswith("__") and t.endswith("__"))
        ]
        if tags:
            line += f" 标签:{','.join(tags)}"
        if btype == "feel":
            feel_lines.append(line)
        elif btype == "plan":
            plan_status = meta.get("status", "active")
            plan_lines.append(line + f" [{plan_status}]")
        elif logical_letter:
            author = meta.get("author", "?")
            letter_lines.append(line + f" [{author}]")
        else:
            normal_lines.append(line)

    sections = [status]
    if normal_lines:
        sections.append("=== 记忆列表 ===\n" + "\n".join(normal_lines))
    if plan_lines:
        sections.append(f"=== 计划（{len(plan_lines)} 条）===\n" + "\n".join(plan_lines))
    if feel_lines:
        sections.append(f"=== feel（{len(feel_lines)} 条）===\n" + "\n".join(feel_lines))
    if letter_lines:
        sections.append(f"=== 信件（{len(letter_lines)} 封）===\n" + "\n".join(letter_lines))
    return "\n\n".join(sections)
