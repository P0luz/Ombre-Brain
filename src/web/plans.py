"""
========================================
web/plans.py — 计划看板（Plan kanban）
========================================

- /api/plans：按状态分组的计划列表（active/resolved/abandoned），含 change_log
- /api/plans/{bucket_id}/action：对计划执行状态流转 / 编辑

对外暴露：register(mcp)。
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from tools._common import check_content_size
from tools.plan.core import is_letter_bucket

logger = sh.logger


def register(mcp) -> None:

    # =============================================================
    # /api/plans — iter 1.7 §G2  Plan kanban list (active / resolved / abandoned)
    # 计划列表（按状态分组），含 change_log 历史
    # =============================================================
    @mcp.custom_route("/api/plans", methods=["GET"])
    async def api_plans(request: Request) -> Response:
        """Return plan buckets grouped by status (looks like a kanban board).

        返回所有 type==plan 的桶，按 status 分三组：active / resolved / abandoned。
        每组内部按 updated_at 倒序（最近动过的在最上面）。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            # 三个空桶子，后面按 status 往里填
            # 类型标注 dict[str, list] 是 Python 3.9+ 语法，不要变运行 IDE 报错
            groups: dict[str, list] = {"active": [], "resolved": [], "abandoned": []}
            for b in all_buckets:
                meta = b.get("metadata", {})
                # 过滤：只要计划类，跳过其他类型的桶
                if meta.get("type") != "plan" or is_letter_bucket(b):
                    continue
                # status 不一定存在（老数据），默认 active；lower() 防御大小写
                st = (meta.get("status") or "active").lower()
                # 未知状态一律当 active 处理，避免 KeyError
                if st not in groups:
                    st = "active"
                groups[st].append({
                    "id": b["id"],
                    "name": meta.get("name") or "",
                    "content": b.get("content", ""),
                    "status": st,
                    "created_at": meta.get("created"),
                    "updated_at": meta.get("last_active"),
                    "related_bucket": meta.get("related_bucket"),
                    "change_log": meta.get("change_log") or [],
                    "tags": meta.get("tags") or [],
                    "importance": meta.get("importance", 7),
                    # iter 1.8: 承诺重量与「为什么」
                    "weight": float(meta.get("weight", 0.5)) if meta.get("weight") is not None else 0.5,
                    "why_remembered": meta.get("why_remembered", ""),
                })
            # 每组按 updated_at 倒序。lambda 是匿名函数；key 函数指定「拿什么排序」
            # `or .. or ""` 堆叠保底：缺字段也不会报 NoneType < str 错
            # iter 1.8: active 列改为 (weight desc, updated_at desc) —— 重的计划在前。
            # 排序键是「越靠后越主」：先按 updated_at 倒序的列表上再按 weight 倒序会使 weight 作为主键，
            # 所以这里用组合 key。resolved/abandoned 只按 updated_at 倒序。
            groups["active"].sort(
                key=lambda p: (-float(p.get("weight") or 0.5), p.get("updated_at") or p.get("created_at") or ""),
                reverse=False,  # 已经用负号使 weight 高为小（排前）；updated_at 字符串低位为后，reverse=False 下新的在后。
            )
            # 反转一下让同 weight 下新的在前：用二次稳定排序。
            groups["active"].sort(
                key=lambda p: p.get("updated_at") or p.get("created_at") or "",
                reverse=True,
            )
            groups["active"].sort(
                key=lambda p: float(p.get("weight") or 0.5),
                reverse=True,
            )
            for k in ("resolved", "abandoned"):
                groups[k].sort(key=lambda p: p.get("updated_at") or p.get("created_at") or "", reverse=True)
            return JSONResponse({
                "active": groups["active"],
                "resolved": groups["resolved"],
                "abandoned": groups["abandoned"],
                # 生成器表达式：sum + len，不需要临时 list
                "total": sum(len(v) for v in groups.values()),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/plans/{bucket_id}/action", methods=["POST"])
    async def api_plans_action(request: Request) -> Response:
        """Frontend kanban actions: mark plan as resolved / abandoned / active, or edit content.

        前端看板操作：勾选/打叉/重新激活，或编辑正文。
        路由里的 {bucket_id} 会被 starlette 解析进 request.path_params。
        Body 示例：{"action": "resolve", "content": "..."} —— content 仅 edit 需要。

        返回码约定：
          400 = 请求参数错（缺字段/超大小/不是 plan）
          404 = 指定桃子不存在
          500 = 底层 update 失败或未知异常
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            bucket_id = request.path_params.get("bucket_id", "").strip()
            if not bucket_id:
                return JSONResponse({"error": "missing bucket_id"}, status_code=400)
            body = await sh._read_json_object(request)
            action_raw = body.get("action") or ""
            if not isinstance(action_raw, str):
                return JSONResponse({"error": "action must be a string"}, status_code=400)
            action = action_raw.strip().lower()
            bucket = await sh.bucket_mgr.get(bucket_id)
            if not bucket:
                return JSONResponse({"error": f"plan not found: {bucket_id}"}, status_code=404)
            # 双重防御：这个端点只能动 plan 桃子，别的类型不允许
            if (
                bucket.get("metadata", {}).get("type") != "plan"
                or is_letter_bucket(bucket)
            ):
                return JSONResponse({"error": "bucket is not a plan"}, status_code=400)

            old_meta = bucket.get("metadata", {})
            # 复制一份历史记录（避免 append 后意外修改原 bucket dict）
            history = list(old_meta.get("change_log") or [])
            from tools._common import append_plan_change_log
            updates: dict[str, object] = {}

            if action in ("resolve", "abandon", "reopen"):
                # action 名 → 目标 status 名 的映射表，比三串 if/elif 清爽
                new_status = {"resolve": "resolved", "abandon": "abandoned", "reopen": "active"}[action]
                old_status = old_meta.get("status", "active")
                # 同状态 noop：不记入历史，下面 updates 为空会走 noop 分支
                if new_status != old_status:
                    updates["status"] = new_status
                    history = append_plan_change_log(
                        history, "status",
                        **{
                            "from": old_status,
                            "to": new_status,
                            "by": "dashboard",
                        },
                    )
            elif action == "edit":
                new_content = body.get("content", "")
                # 双重检查：类型必须是字符串，且 strip 后非空
                if not isinstance(new_content, str) or not new_content.strip():
                    return JSONResponse({"error": "content required for edit"}, status_code=400)
                size_err = check_content_size(new_content)
                if size_err:
                    return JSONResponse({"error": size_err}, status_code=400)
                updates["content"] = new_content.strip()
                history = append_plan_change_log(history, "edit", by="dashboard")
            else:
                return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

            # status 没变 且 不是 edit，成 noop。返回 200 + ok=true，不报错
            if not updates:
                return JSONResponse({"ok": True, "noop": True})
            # status 或正文经显式动作改变后，旧完成建议所依据的 plan 已失效。
            # None 由 BucketManager 解释为删除 frontmatter 字段。
            updates["resolution_suggested"] = None
            updates["change_log"] = history
            ok = await sh.bucket_mgr.update(bucket_id, **updates)
            if not ok:
                return JSONResponse({"error": "update failed"}, status_code=500)
            # --- plan 看板把 plan 显式标 resolved → 联动 related_bucket / resolved_by ---
            # rule.md §1：与 trace_core 同一逻辑（人工/AI 显式路径）。
            cascaded: list[str] = []
            if updates.get("status") == "resolved":
                from tools._common import cascade_plan_resolved_to_buckets
                merged_meta = {**old_meta, **{k: v for k, v in updates.items() if k != "change_log"}}
                try:
                    cascaded = await cascade_plan_resolved_to_buckets(merged_meta, bucket_id)
                except Exception as e:
                    logger.warning(f"plans/action cascade outer error: {e}")
            # 返回体不包含 change_log（它很长，前端会重拉 /api/plans 刷新）
            return JSONResponse({
                "ok": True,
                "id": bucket_id,
                "updates": {
                    k: v for k, v in updates.items()
                    if k not in ("change_log", "resolution_suggested")
                },
                "cascaded_resolved": cascaded,
            })
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
