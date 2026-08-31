"""
========================================
tools/trace/core.py — trace 主路径（修改 / 删除 / 重生 embedding）
========================================

trace 是 OB 唯一的「写元数据」入口，承接所有桶字段更新和删除。模型
传什么字段，就改什么字段；-1 / 空串 表示「不改」。

关键行为：
- delete=True → Markdown 移入 archive/ 并清理可重建的 embedding
- hard_delete=True → 仅清理创建时明确标记 test_data=True 的测试桶；
  必须同时提供非空 delete_reason，普通记忆和 plan 均拒绝且保持原位
- 收集传入字段构造 updates dict（含 status/weight/dont_surface/
  why_remembered/pinned/digested/resolved/content/tags/domain 等）
- pinned=1 时强制 importance=10 并做配额检查；pinned=0 必须在
  同一次调用显式传入 importance=1..10，原子恢复动态评分
- content 改写时同步重建 embedding，并对 plan 桶追加 change_log
- resolved/digested 切换会附中文语义提示
- unlink/relink 修正后端自动建错的桶间关系，双向生效，走独立早返回
  分支（见 _relation_edit.py），不与字段更新混用
- quotes_replace 订正/删除写入那一刻留下的引语，同样走独立早返回分支
  （见 _quote_edit.py）；只能改和删，不能补录
- reinforce=True 是 3.6.0 起唯一的强化入口（见 _reinforce.py）：breath_search
  不再自动 touch 命中桶，「这条要紧」改由模型读完之后针对具体桶显式确认

不做什么（边界）：
- 不创建桶（那是 hold/grow/plan/letter 的事）
- 不凭空建立桶间关系——relink 只改已存在关系的类型，「建立」仍归后端
- 不补录引语——quotes_replace 只改/删已有的，「决定记住哪几句」仍归写入那一刻
- 不把普通记忆转换成可擦除测试数据，也不物理删除普通记忆
- 不返回结构化数据，统一中文短句

对外暴露：trace_core(bucket_id, name, domain, valence, arousal, importance,
                     tags, resolved, pinned, protected, digested, content, delete,
                     status, weight, dont_surface, why_remembered,
                     meaning_append, meaning_replace, media_append, media_replace,
                     hard_delete, delete_reason, restore, old_str, new_str,
                     unlink, relink, relation_type, quotes_replace, reinforce) → str
========================================
"""

import math
from contextlib import AsyncExitStack
from typing import Optional

from errors import ToolInputError
from ombrebrain.domain.memory_messages import resolved_hint
from utils import normalize_memory_title, parse_bool
from .. import _runtime as rt
from .._common import (
    _quota_turn,
    check_content_size,
    check_metadata_size,
    check_pinned_quota,
    check_protected_quota,
)
from ..plan.core import is_letter_bucket, letter_lock_revision, letter_lock_state
from . import _quote_edit, _reinforce, _relation_edit


async def trace_core(
    bucket_id: str,
    name: Optional[str] = "",
    title: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    protected: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
    meaning_append: Optional[str] = "",
    meaning_replace: Optional[list] = None,
    media_append: Optional[list | str] = None,
    media_replace: Optional[list | str] = None,
    hard_delete: Optional[bool] = False,
    delete_reason: Optional[str] = "",
    restore: Optional[bool] = False,
    old_str: Optional[str] = "",
    new_str: Optional[str] = None,
    unlink: Optional[str] = "",
    relink: Optional[str] = "",
    relation_type: Optional[str] = "",
    quotes_replace: Optional[list] = None,
    reinforce: Optional[bool] = False,
) -> str:
    bucket_id = "" if bucket_id is None else str(bucket_id)
    if name is None:
        name = ""
    if title is None:
        title = ""
    if domain is None:
        domain = ""
    if valence is None:
        valence = -1
    if arousal is None:
        arousal = -1
    if importance is None:
        importance = -1
    if tags is None:
        tags = ""
    if resolved is None:
        resolved = -1
    if pinned is None:
        pinned = -1
    if protected is None:
        protected = -1
    if digested is None:
        digested = -1
    if content is None:
        content = ""
    if delete is None:
        delete = False
    if status is None:
        status = ""
    if weight is None:
        weight = -1
    if dont_surface is None:
        dont_surface = -1
    if why_remembered is None:
        why_remembered = ""
    if meaning_append is None:
        meaning_append = ""
    if media_append is None:
        media_append = []
    new_str_provided = new_str is not None
    old_str = "" if old_str is None else str(old_str)
    new_str = "" if new_str is None else str(new_str)
    content = str(content)
    name = str(name)
    domain = str(domain)
    tags = str(tags)
    status = str(status)
    why_remembered = str(why_remembered)
    meaning_append = str(meaning_append)
    delete = parse_bool(delete, default=False)
    hard_delete = parse_bool(hard_delete, default=False)
    restore = parse_bool(restore, default=False)
    delete_reason = "" if delete_reason is None else str(delete_reason).strip()

    def _finite_float(value, default: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return numeric if math.isfinite(numeric) else default

    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    valence = _finite_float(valence, -1)
    arousal = _finite_float(arousal, -1)
    weight = _finite_float(weight, -1)
    importance = _safe_int(importance, -1)
    resolved = _safe_int(resolved, -1)
    pinned = _safe_int(pinned, -1)
    protected = _safe_int(protected, -1)
    digested = _safe_int(digested, -1)
    dont_surface = _safe_int(dont_surface, -1)
    if protected not in (-1, 0, 1):
        raise ToolInputError("protected 只能传 -1、0 或 1；本次未修改。")

    metadata_err = check_metadata_size(
        bucket_id=bucket_id,
        name=name,
        domain=domain,
        tags=tags,
        status=status,
        why_remembered=why_remembered,
        meaning_append=meaning_append,
        delete_reason=delete_reason,
    )
    if metadata_err:
        raise ToolInputError(metadata_err)
    if rt.mark_op:
        rt.mark_op("trace")
    rt.record_v3_tool_event("trace", {
        "bucket_id": bucket_id,
        "name": name,
        "domain": domain,
        "valence": valence,
        "arousal": arousal,
        "importance": importance,
        "tags": tags,
        "resolved": resolved,
        "pinned": pinned,
        "protected": protected,
        "digested": digested,
        "content_length": len(content or ""),
        "delete": delete,
        "hard_delete": hard_delete,
        "restore": restore,
        "delete_reason_length": len(delete_reason),
        "old_str_length": len(old_str),
        "new_str_length": len(new_str) if new_str_provided else 0,
        "status": status,
        "weight": weight,
        "dont_surface": dont_surface,
        "why_remembered_length": len(why_remembered or ""),
    })

    if not bucket_id or not bucket_id.strip():
        raise ToolInputError("请提供有效的 bucket_id。")

    # 关系修正是独立分支：它只动 relation_links，不参与下面的字段收集，
    # 所以在这里就地返回，不和 delete / content 改写等混在一起。
    unlink = str(unlink or "").strip()
    relink = str(relink or "").strip()
    relation_type = str(relation_type or "").strip()
    if unlink or relink or relation_type:
        if quotes_replace is not None:
            raise ToolInputError("quotes_replace 不能与关系修正同时使用，请分开调用；本次未修改。")
        if parse_bool(reinforce, default=False):
            raise ToolInputError("reinforce 不能与关系修正同时使用，请分开调用；本次未修改。")
        if not await rt.bucket_mgr.get(bucket_id):
            raise ToolInputError(f"找不到记忆 {bucket_id}；本次未修改。")
        return await _relation_edit.apply(
            bucket_id.strip(), unlink, relink, relation_type
        )

    # 引语订正同样是独立早返回分支，理由与关系修正一致：它只动 metadata.quotes，
    # 是「回头看这几句，说其中某句不对」，和改 importance、改正文不是一次动作。
    #
    # 冲突必须显式报错而不是静默忽略：早返回分支最容易出的错，就是同一次调用里
    # 另外那半个意图被悄悄丢掉，而返回值看起来是成功的。
    if quotes_replace is not None:
        if not isinstance(quotes_replace, list):
            raise ToolInputError("quotes_replace 必须是列表（空列表表示删除全部引语）；本次未修改。")
        if parse_bool(reinforce, default=False):
            raise ToolInputError("reinforce 不能与 quotes_replace 同时使用，请分开调用；本次未修改。")
        if any((
            bool(name), bool(title), bool(domain), valence != -1, arousal != -1,
            importance != -1, bool(tags), resolved != -1, pinned != -1,
            protected != -1, digested != -1, bool(content), delete, hard_delete,
            restore, bool(status), weight != -1, dont_surface != -1,
            bool(why_remembered), bool(meaning_append), meaning_replace is not None,
            bool(media_append), media_replace is not None, bool(delete_reason),
            bool(old_str), new_str_provided,
        )):
            raise ToolInputError("quotes_replace 必须单独调用，不能与其他字段更新或删除混在一次 "
                "trace 里；本次未修改。")
        return await _quote_edit.apply(bucket_id.strip(), quotes_replace)

    # 显式强化：3.6.0 把 breath_search 的自动 touch 拆掉之后，这里是唯一的
    # 强化入口。同样独立早返回——「这条要紧」是一次事件，不是一个字段更新，
    # 和改 importance 混在一起会让「读完之后的判断」变成一次顺手的副作用。
    if parse_bool(reinforce, default=False):
        if any((
            bool(name), bool(title), bool(domain), valence != -1, arousal != -1,
            importance != -1, bool(tags), resolved != -1, pinned != -1,
            protected != -1, digested != -1, bool(content), delete, hard_delete,
            restore, bool(status), weight != -1, dont_surface != -1,
            bool(why_remembered), bool(meaning_append), meaning_replace is not None,
            bool(media_append), media_replace is not None, bool(delete_reason),
            bool(old_str), new_str_provided,
        )):
            raise ToolInputError(
                "reinforce=True 必须单独调用，不能与其他字段更新或删除混在一次 "
                "trace 里；本次未强化、未修改。"
            )
        return await _reinforce.apply(bucket_id.strip())

    if restore or delete or hard_delete:
        guarded_reader = (
            getattr(rt.bucket_mgr, "get_including_archive", None)
            if restore else None
        )
        if not callable(guarded_reader):
            guarded_reader = rt.bucket_mgr.get
        guarded_bucket = await guarded_reader(bucket_id)
        if (
            guarded_bucket
            and is_letter_bucket(guarded_bucket)
            and letter_lock_state(guarded_bucket, "ai")["locked"]
        ):
            raise ToolInputError("这封信尚未向你开放，不能通过 trace 修改其生命周期。")

    restore_unprotect = bool(restore and protected == 0)
    if restore_unprotect and not (1 <= importance <= 10):
        raise ToolInputError("恢复归档桶时解除 protected，必须在同一次 trace 中显式传入 "
            "importance=1..10；本次未恢复、未修改。")

    restore_conflicts = any((
        delete,
        hard_delete,
        bool(name),
        bool(domain),
        valence != -1,
        arousal != -1,
        importance != -1 and not restore_unprotect,
        bool(tags),
        resolved != -1,
        pinned != -1,
        protected != -1 and not restore_unprotect,
        digested != -1,
        bool(content),
        bool(status),
        weight != -1,
        dont_surface != -1,
        bool(why_remembered),
        bool(meaning_append),
        meaning_replace is not None,
        bool(media_append),
        media_replace is not None,
        bool(delete_reason),
        bool(old_str),
        new_str_provided,
    ))
    if restore and restore_conflicts:
        raise ToolInputError("参数冲突：restore=True 必须单独调用，不能同时删除或修改记忆；"
            "唯一例外是用 protected=0 与 importance=1..10 原子解除归档保护。"
            "本次未恢复、未修改。")
    if restore:
        if not guarded_bucket:
            raise ToolInputError(f"未找到记忆桶: {bucket_id}")
        restore_meta = guarded_bucket.get("metadata", {})
        if not isinstance(restore_meta, dict):
            restore_meta = {}
        restore_is_terminal = bool(
            str(restore_meta.get("type") or "").strip().lower() == "archived"
            or restore_meta.get("deleted_at")
            or parse_bool(restore_meta.get("tombstone"), default=False)
        )
        if not restore_is_terminal:
            return f"记忆桶仍在日常记忆中，无需恢复: {bucket_id}"

        restore_pinned = parse_bool(
            restore_meta.get("pinned"), default=False
        )
        restore_protected = parse_bool(
            restore_meta.get("protected"), default=False
        )
        restore_anchor = parse_bool(
            restore_meta.get("anchor"), default=False
        )
        if restore_unprotect and not restore_protected:
            raise ToolInputError(f"归档记忆桶 {bucket_id} 当前不是 protected；"
                "请单独使用 restore=True 恢复。本次未恢复、未修改。")
        if restore_protected and restore_anchor and not restore_unprotect:
            raise ToolInputError(f"归档记忆桶 {bucket_id} 同时带有 protected 与 anchor，"
                "不能恢复为冲突的活跃状态。请调用 "
                "trace(bucket_id, restore=True, protected=0, "
                "importance=1..10) 原子解除保护并恢复。")
        final_restore_protected = restore_protected and not restore_unprotect
        try:
            restored_type = rt.bucket_mgr.footprint_snapshot().original_kind(
                bucket_id, dict(restore_meta)
            )
        except Exception:
            restored_type = "dynamic"
        if restored_type not in {
            "dynamic", "permanent", "feel", "plan", "letter", "i", "self",
        }:
            restored_type = "dynamic"
        if restore_pinned:
            restored_type = "permanent"

        restored_importance = _safe_int(
            restore_meta.get("importance"), 0
        )
        restored_target_importance = (
            int(importance)
            if restore_unprotect
            else (10 if final_restore_protected else restored_importance)
        )
        restore_snapshot = (
            restore_pinned,
            restore_protected,
            restore_anchor,
            str(restore_meta.get("type") or "").strip().lower(),
            restored_importance,
            parse_bool(restore_meta.get("dont_surface"), default=False),
            str(restore_meta.get("deleted_at") or ""),
            parse_bool(restore_meta.get("tombstone"), default=False),
        )
        importance_override = (
            restored_target_importance if restore_unprotect else None
        )
        async with AsyncExitStack() as restore_stack:
            if final_restore_protected:
                await restore_stack.enter_async_context(
                    _quota_turn("protected")
                )

            locked_reader = getattr(
                rt.bucket_mgr, "get_including_archive", None
            )
            if not callable(locked_reader):
                locked_reader = rt.bucket_mgr.get
            locked_bucket = await locked_reader(bucket_id)
            if not locked_bucket:
                raise ToolInputError(f"未找到记忆桶: {bucket_id}")
            locked_meta = locked_bucket.get("metadata", {})
            if not isinstance(locked_meta, dict):
                locked_meta = {}
            locked_restore_snapshot = (
                parse_bool(locked_meta.get("pinned"), default=False),
                parse_bool(locked_meta.get("protected"), default=False),
                parse_bool(locked_meta.get("anchor"), default=False),
                str(locked_meta.get("type") or "").strip().lower(),
                _safe_int(locked_meta.get("importance"), 0),
                parse_bool(locked_meta.get("dont_surface"), default=False),
                str(locked_meta.get("deleted_at") or ""),
                parse_bool(locked_meta.get("tombstone"), default=False),
            )
            if locked_restore_snapshot != restore_snapshot:
                raise ToolInputError(
                    f"记忆桶 {bucket_id} 在恢复期间已被其他请求更新，"
                    "为避免覆盖或配额误判，请重试。"
                )

            if final_restore_protected:
                quota_err = await check_protected_quota()
                if quota_err:
                    return quota_err

            result = await rt.bucket_mgr.restore_archived(
                bucket_id,
                importance_override=importance_override,
                protected_override=False if restore_unprotect else None,
            )
        if result.get("ok"):
            return f"已重新回忆并恢复记忆桶: {bucket_id}"
        if result.get("error") == "not_archived":
            return f"记忆桶仍在日常记忆中，无需恢复: {bucket_id}"
        if result.get("error") == "not_found":
            raise ToolInputError(f"未找到记忆桶: {bucket_id}")
        if result.get("error") == "incompatible_protected_anchor":
            return (
                f"归档记忆桶 {bucket_id} 同时带有 protected 与 anchor，"
                "请调用 trace(bucket_id, restore=True, protected=0, "
                "importance=1..10) 原子解除保护并恢复。"
            )
        raise ToolInputError(f"恢复记忆桶失败: {result.get('error', 'unknown_error')}")

    patch_args_supplied = bool(old_str) or new_str_provided
    if patch_args_supplied and (delete or hard_delete):
        raise ToolInputError("参数冲突：old_str/new_str 局部替换不能与 delete/hard_delete 同时使用；"
            "本次未修改、未删除、未归档。")
    if patch_args_supplied and content:
        raise ToolInputError("参数冲突：不能同时使用 content 完整替换和 old_str/new_str 局部替换；"
            "本次未修改。")
    if patch_args_supplied and (not old_str or not new_str_provided):
        raise ToolInputError("局部替换必须同时提供 old_str 和 new_str；new_str 可以是空字符串以删除片段。"
            "本次未修改。")
    if patch_args_supplied and old_str == new_str:
        raise ToolInputError("old_str 与 new_str 完全相同，没有内容需要替换；本次未修改。")

    # --- Delete 模式（F-10：普通记忆只允许软删除/归档）---
    if hard_delete and delete:
        raise ToolInputError("参数冲突：delete=True 表示归档，hard_delete=True 仅表示清理测试桶，"
            "两者不能同时使用；本次未删除、未归档。")
    if hard_delete:
        if not delete_reason:
            raise ToolInputError("拒绝永久删除：hard_delete 仅用于创建时明确标记为 test_data 的测试桶，"
                "并且必须提供非空 delete_reason；本次未删除、未归档。")
        if len(delete_reason) > 500:
            raise ToolInputError("拒绝永久删除：delete_reason 不能超过 500 个字符；本次未删除、未归档。")
        result = await rt.bucket_mgr.hard_delete_test_bucket(
            bucket_id, reason=delete_reason
        )
        if result.get("ok"):
            return f"已永久删除测试桶: {bucket_id}"
        if result.get("error") == "not_erasable_test_data":
            raise ToolInputError("拒绝永久删除：普通记忆桶（包括 plan）不可被 trace 物理删除；"
                "只有创建时明确标记为 test_data 的测试桶可以清理。"
                "本次未删除、未归档；若只想从日常召回隐藏，请改用 delete=True 归档。")
        if result.get("error") == "missing_delete_reason":
            raise ToolInputError("拒绝永久删除：必须提供非空 delete_reason；本次未删除、未归档。")
        if result.get("error") == "delete_reason_too_long":
            raise ToolInputError("拒绝永久删除：delete_reason 不能超过 500 个字符；本次未删除、未归档。")
        if result.get("error") == "not_found":
            raise ToolInputError(f"未找到记忆桶: {bucket_id}；本次未删除、未归档。")
        # 兜底保留原始错误码便于排查，但不能只甩一个英文码给调用方。
        raise ToolInputError(f"永久删除失败：{result.get('error', 'unknown_error')}；本次未删除、未归档。")

    if delete:
        success = await rt.bucket_mgr.delete(bucket_id)
        if not success:
            # 与上面 hard_delete 的 not_found 同一回事：桶不存在，这次什么都没归档。
            # 之前这里 return 字符串，同一个函数里三种「找不到桶」两种处理，
            # 调用方拿到 isError=False 会以为归档成功了。
            raise ToolInputError(f"未找到记忆桶: {bucket_id}；本次未归档。")
        return f"已将记忆桶存入档案（不可在日常召回中浮现）: {bucket_id}"

    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        raise ToolInputError(f"未找到记忆桶: {bucket_id}")

    meta = bucket.get("metadata", {})
    current_pinned = parse_bool(meta.get("pinned"), default=False)
    current_protected = parse_bool(meta.get("protected"), default=False)
    current_anchor = parse_bool(meta.get("anchor"), default=False)
    logical_letter = is_letter_bucket(bucket)
    lock_precondition = (
        {"expected_lock_state": letter_lock_revision(bucket)}
        if logical_letter else {}
    )
    if logical_letter and letter_lock_state(bucket, "ai")["locked"]:
        raise ToolInputError("这封信尚未向你开放；请使用 Letter 专用入口管理锁状态。")
    pin_state_changed = (
        pinned in (0, 1) and bool(pinned) != current_pinned
    )
    protected_state_changed = (
        protected in (0, 1) and bool(protected) != current_protected
    )
    if logical_letter and (pin_state_changed or protected_state_changed):
        raise ToolInputError("Letter 不能通过 trace 改变 pinned/protected 状态；"
            "请使用 Letter 专用入口。")

    final_pinned = bool(pinned) if pinned in (0, 1) else current_pinned
    final_protected = (
        bool(protected)
        if protected in (0, 1)
        else current_protected
    )
    if final_pinned and final_protected:
        raise ToolInputError("pinned 与 protected 互斥，本次未修改。")
    if final_protected and current_anchor:
        raise ToolInputError("protected 与 anchor 互斥；请先用 release() 解除 anchor，"
            "再保护该记忆桶。本次未修改。")

    leaving_last_guard = (
        (current_pinned or current_protected)
        and not (final_pinned or final_protected)
    )
    if leaving_last_guard and not (1 <= importance <= 10):
        raise ToolInputError(f"解除记忆桶 {bucket_id} 最后一层 pinned/protected 保护时，"
            "必须在同一次 trace "
            "中显式传入 importance=1..10。本次未修改。")

    if (
        1 <= importance <= 10
        and (final_pinned or final_protected)
        and not (pin_state_changed or protected_state_changed)
    ):
        raise ToolInputError(f"记忆桶 {bucket_id} 是 pinned/受保护桶，importance 被锁定为 10，"
            "本次未修改。解除最后一层保护时请在同一次"
            "调用传入 importance=1..10。")

    # 配额判定 + 落盘必须在同一把锁里：check_pinned_quota/check_protected_quota
    # 到最终 bucket_mgr.update() 之间隔着别的字段处理和一次 await，两个并发 trace()
    # 都可能在对方提交前读到同一个「未满」快照。是否需要哪把锁在动 updates 之前就
    # 能从入参判断出来，所以先算好，再把整段检查+落盘包进对应的 quota turn。
    current_importance = int(meta.get("importance") or 0)
    current_type = str(meta.get("type") or "dynamic").strip().lower()
    protecting_now = final_protected and not current_protected
    pinning_now = final_pinned and not current_pinned
    requested_importance = (
        int(importance) if 1 <= importance <= 10 else current_importance
    )
    final_importance = (
        10
        if final_pinned or final_protected
        else requested_importance
    )
    current_dont_surface = parse_bool(
        meta.get("dont_surface"), default=False
    )
    need_pinned_lock = pin_state_changed
    need_protected_lock = protected_state_changed

    async with AsyncExitStack() as quota_stack:
        if need_pinned_lock:
            await quota_stack.enter_async_context(_quota_turn("pinned"))
        if need_protected_lock:
            await quota_stack.enter_async_context(_quota_turn("protected"))

        if need_pinned_lock or need_protected_lock:
            locked_bucket = await rt.bucket_mgr.get(bucket_id)
            if not locked_bucket:
                raise ToolInputError(f"未找到记忆桶: {bucket_id}")
            locked_meta = locked_bucket.get("metadata", {})
            locked_snapshot = (
                parse_bool(locked_meta.get("pinned"), default=False),
                parse_bool(locked_meta.get("protected"), default=False),
                str(locked_meta.get("type") or "dynamic").strip().lower(),
                int(locked_meta.get("importance") or 0),
                parse_bool(locked_meta.get("dont_surface"), default=False),
                parse_bool(locked_meta.get("anchor"), default=False),
            )
            original_snapshot = (
                current_pinned,
                current_protected,
                current_type,
                current_importance,
                current_dont_surface,
                current_anchor,
            )
            if locked_snapshot != original_snapshot:
                raise ToolInputError(
                    f"记忆桶 {bucket_id} 在本次修改期间已被其他请求更新，"
                    "为避免覆盖或配额误判，请重试。"
                )

        updates: dict = {}
        if name:
            updates["name"] = name
        if title:
            # name 是桶名，title 是这条记忆自己的标题（信件标题就存在这里）。
            # 两个字段各走各的，改一个不该动另一个。
            try:
                updates["title"] = normalize_memory_title(title)
            except ValueError as exc:
                raise ToolInputError(str(exc)) from exc
        if domain:
            updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
        if 0 <= valence <= 1:
            updates["valence"] = valence
        if 0 <= arousal <= 1:
            updates["arousal"] = arousal
        if 1 <= importance <= 10:
            updates["importance"] = final_importance
        if tags:
            updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        if resolved in (0, 1):
            updates["resolved"] = bool(resolved)
        if pinned in (0, 1):
            updates["pinned"] = bool(pinned)
            if pinned == 1:
                if pinning_now:
                    err = await check_pinned_quota()
                    if err:
                        # 配额满 = 这次没钉上，桶原样不动。
                        raise ToolInputError(err)
                updates["importance"] = 10
        if protected in (0, 1):
            updates["protected"] = bool(protected)
            if protected == 1:
                if protecting_now:
                    err = await check_protected_quota()
                    if err:
                        raise ToolInputError(err)
                updates["importance"] = 10
        if digested in (0, 1):
            updates["digested"] = bool(digested)
        if content:
            size_err = check_content_size(content)
            if size_err:
                raise ToolInputError(size_err)
            updates["content"] = content
        if status:
            s = status.strip().lower()
            if s in ("active", "resolved", "abandoned"):
                updates["status"] = s
        if 0 <= weight <= 1:
            updates["weight"] = float(weight)
        if dont_surface in (0, 1):
            updates["dont_surface"] = bool(dont_surface)
        why_remembered = str(why_remembered).strip()
        if why_remembered == "\\clear":
            updates["why_remembered"] = ""
        elif why_remembered:
            updates["why_remembered"] = why_remembered[:500]

        # --- Miss: meaning / media —— 追加是日常操作，整体替换只用于纠错/清理 ---
        if meaning_append.strip():
            updates["meaning_append"] = meaning_append.strip()
        if meaning_replace is not None:
            updates["meaning"] = meaning_replace
        if media_append:
            updates["media_append"] = media_append
        if media_replace is not None:
            updates["media"] = media_replace

        if not updates and not patch_args_supplied:
            # 空调用是干净的 noop，不是失败——调用方什么都没要求改，也就谈不上
            # 「要求了却没做到」。这跟 anchor 的「它已经是 anchor 了」、
            # 「两条记忆之间本来就没有关系」是同一类。
            #
            # 我一度把它改成抛异常（理由是「trace 是写工具，调它就有改的意图」），
            # 那是推翻了既有契约：test_mcp_tools_docker_integration.py 里
            # test_trace_existing_bucket_without_changes_is_a_clean_noop
            # 明确断言它返回这句话且 isError=False。
            return "没有任何字段需要修改。"

        # --- plan 桶：status / content 改变时追加 change_log ---
        content_change_requested = "content" in updates or patch_args_supplied
        is_plan = bucket.get("metadata", {}).get("type") == "plan"
        append_plan_history_in_patch = is_plan and patch_args_supplied
        if is_plan and not patch_args_supplied and (
            "status" in updates or content_change_requested
        ):
            from .._common import append_plan_change_log
            old_meta = bucket.get("metadata", {})
            history = list(old_meta.get("change_log") or [])
            old_status = old_meta.get("status") or "active"
            if "status" in updates and updates["status"] != old_status:
                history = append_plan_change_log(
                    history, "status",
                    **{
                        "from": old_status,
                        "to": updates["status"],
                        "by": "trace",
                    },
                )
            if content_change_requested:
                history = append_plan_change_log(history, "edit", by="trace")
            updates["change_log"] = history

        if is_plan and ("status" in updates or content_change_requested):
            # 显式状态/正文变更会让旧完成建议失效；None 由 BucketManager
            # 解释为删除 frontmatter 字段。失败的局部替换不会进入提交路径。
            updates["resolution_suggested"] = None

        if patch_args_supplied:
            patch_result = await rt.bucket_mgr.update_content_fragment(
                bucket_id,
                old_str=old_str,
                new_str=new_str,
                append_plan_history=append_plan_history_in_patch,
                event_actor="llm",
                **lock_precondition,
                **updates,
            )
            if not patch_result.get("ok"):
                patch_error = patch_result.get("error")
                if patch_error == "not_found":
                    raise ToolInputError(f"未找到记忆桶: {bucket_id}")
                if patch_error == "old_str_not_found":
                    raise ToolInputError("未找到 old_str，正文未修改。请从 Dashboard 或对应记忆类型的读取入口"
                        "核对当前原文；普通记忆也可用 "
                        f'breath_advanced(query="{bucket_id}", max_results=1, '
                        "max_tokens=20000) 按完整 bucket_id 读取。复制连续且逐字一致的片段后重试。")
                if patch_error == "old_str_ambiguous":
                    raise ToolInputError("old_str 在正文中至少出现 2 次，"
                        "无法安全确定要修改哪一处；正文未修改。请提供更长且唯一的原文片段。")
                if patch_error == "invalid_content":
                    raise ToolInputError(
                        str(patch_result.get("message") or "替换后的内容不符合存储限制。")
                    )
                if patch_error == "unchanged":
                    raise ToolInputError("old_str 与 new_str 替换后正文没有变化；本次未修改。")
                raise ToolInputError(f"修改失败: {bucket_id}")
        else:
            success = await rt.bucket_mgr.update(
                bucket_id,
                event_actor="llm",
                **lock_precondition,
                **updates,
            )
            if not success:
                raise ToolInputError(f"修改失败: {bucket_id}")

    # 注意：完整正文更新和局部替换都会在 BucketManager 内先提交 Markdown，
    # 释放桶租约后再投递 embedding outbox。这里不需要、也不应该重复调用
    # generate_and_store，否则同一条内容会多打一次向量 API。

    # --- plan 桶人工/AI 显式 resolve → 联动 related_bucket / resolved_by ---
    # rule.md §1：plan 是承诺，承诺被显式放下，承载它的事件桶也不该再浮上来。
    # 仅在 trace 把 plan.status 改成 resolved 时触发；其他路径（自动二判）不联动。
    cascaded: list[str] = []
    if (
        bucket.get("metadata", {}).get("type") == "plan"
        and updates.get("status") == "resolved"
    ):
        from .._common import cascade_plan_resolved_to_buckets
        # 用更新后的 metadata 视图，确保 related_bucket / resolved_by 是最新值
        merged_meta = {**bucket.get("metadata", {}), **{k: v for k, v in updates.items() if k != "change_log"}}
        try:
            cascaded = await cascade_plan_resolved_to_buckets(merged_meta, bucket_id)
        except Exception as e:
            rt.logger.warning(f"trace plan cascade outer error: {e}")

    _display_updates = {
        k: v for k, v in updates.items()
        if k not in (
            "content", "meaning_append", "meaning", "media_append", "media",
            "resolution_suggested",
        )
    }
    changed = ", ".join(f"{k}={v}" for k, v in _display_updates.items())
    if patch_args_supplied:
        changed += (", content=已局部替换" if changed else "content=已局部替换")
    elif "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    if "meaning_append" in updates:
        changed += (", " if changed else "") + "meaning=已追加一条"
    if "meaning" in updates:
        changed += (", " if changed else "") + f"meaning=整体替换({len(updates['meaning'])}条)"
    if "media_append" in updates:
        changed += (", " if changed else "") + f"media=已追加{len(updates['media_append'])}项"
    if "media" in updates:
        changed += (", " if changed else "") + f"media=整体替换({len(updates['media'])}项)"
    if "resolved" in updates:
        changed += f" → {resolved_hint(bool(updates['resolved']))}"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已从默认/被动浮现与 dream 隐藏，显式检索/审计仍可找回"
        else:
            changed += " → 已取消消化隐藏；若无其他隐藏策略，将重新参与默认浮现与 dream"
    if cascaded:
        changed += f" → 同步把 {len(cascaded)} 个关联事件桶也标为已放下（{', '.join(cascaded)}）"
    return f"已修改记忆桶 {bucket_id}: {changed}"
