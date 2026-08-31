"""trace 的关系修正分支（3.3.0）—— 解绑与改类型。

3.2.0 把关系建立交给了后端自动推断，但没留下任何修正入口：连错了就只能
去改 Markdown 的 frontmatter，而且得记住关系是双向的、两个文件都要改。

**为什么走 trace 而不是新开一个工具**：修正关系和「改这条记忆的 importance」
是同一类事——我看了一眼已有的东西，然后说它不对。trace 本来就是 OB 唯一的
「写元数据」入口，关系也是元数据。多一个工具只会多占一个工具位。

**为什么只能改不能建**：`link` 这个动作在 3.0.0 被有意删掉了，理由是关联
不是决定而是结果（见 `tools/_relation_link.py` 开头）。这里只做两件事：
「你连的这条不该连」和「你连对了但类型判错了」，都以**已经存在一条关系**
为前提。凭空建立仍然只归后端。

拆成独立文件是因为 `trace/core.py` 已经 737 行，逼近 800 行硬上限。
"""

from __future__ import annotations

from errors import ToolInputError
from ombrebrain.storage.relation_store import (
    normalize_relation_links,
    normalize_relation_type,
    retype_relation,
    reverse_relation_type,
    unlink_relation,
)

from .. import _runtime as rt

# custom 关系必须带 label（见 normalize_relation_links），而 trace 没有传
# label 的参数。与其加第四个参数，不如明确拒绝——自动关系永远不会是 custom，
# 存量 custom 关系要改标签只能去改文件。
_REJECT_CUSTOM = (
    "relation_type 不支持 custom：custom 关系必须带 label，"
    "而 trace 没有传 label 的入口；本次未修改。"
)


def validate(bucket_id: str, unlink: str, relink: str, relation_type: str) -> None:
    """检查参数组合。不合法一律抛 ToolInputError，通过则静默返回。

    为什么不再返回错误短句：返回值会被调用方 return 出去，在 MCP 侧变成一次
    isError=False 的正常返回——调用方以为关系改好了，实际一个字节都没动。
    """
    if unlink and relink:
        raise ToolInputError("unlink 与 relink 不能同时使用；本次未修改。")
    if relation_type and not relink:
        raise ToolInputError("relation_type 只能配合 relink 使用；本次未修改。")
    if relink and not relation_type:
        raise ToolInputError("relink 必须同时指定 relation_type（caused_by / causes / "
            "continuation_of / continues / related_to / same_event）；本次未修改。")
    target = unlink or relink
    if target == bucket_id:
        raise ToolInputError("不能把一条记忆和它自己解绑或改写关系；本次未修改。")
    if relation_type.strip().lower() == "custom":
        raise ToolInputError(_REJECT_CUSTOM)
    if relation_type:
        try:
            normalize_relation_type(relation_type)
        except ValueError:
            raise ToolInputError(f"未知的 relation_type「{relation_type}」；只支持 caused_by / causes / "
                "continuation_of / continues / related_to / same_event。本次未修改。")


async def apply(bucket_id: str, unlink: str, relink: str, relation_type: str) -> str:
    """执行解绑 / 改类型，返回给模型看的中文短句。"""
    validate(bucket_id, unlink, relink, relation_type)

    target_id = unlink or relink
    if not await rt.bucket_mgr.get(target_id):
        raise ToolInputError(f"找不到目标记忆 {target_id}；本次未修改。")

    if unlink:
        return await _apply_unlink(bucket_id, target_id)
    return await _apply_retype(bucket_id, target_id, relation_type)


def _read_links(post) -> list[dict] | None:
    """读一侧的 relation_links；存量数据写坏了就放弃，不在这里悄悄修复。"""
    try:
        return normalize_relation_links(post.metadata.get("relation_links"))
    except ValueError:
        return None


async def _apply_unlink(bucket_id: str, target_id: str) -> str:
    def _mutation(left_post, right_post):
        left_links = _read_links(left_post)
        right_links = _read_links(right_post)
        if left_links is None or right_links is None:
            return False, False, "broken"

        merged_left, left_changed = unlink_relation(left_links, target_id)
        merged_right, right_changed = unlink_relation(right_links, bucket_id)
        if left_changed:
            left_post["relation_links"] = normalize_relation_links(merged_left)
        if right_changed:
            right_post["relation_links"] = normalize_relation_links(merged_right)
        return left_changed, right_changed, left_changed or right_changed

    result = await rt.bucket_mgr.mutate_relation_pair(bucket_id, target_id, _mutation)
    if result == "broken":
        raise ToolInputError("这两条记忆的 relation_links 数据有问题，无法自动修改；本次未修改。")
    if result is None:
        raise ToolInputError("找不到要修改的记忆文件；本次未修改。")
    if not result:
        return f"{bucket_id} 与 {target_id} 之间本来就没有关系；本次未修改。"
    rt.logger.info(f"op=trace_relation action=unlink {bucket_id} x {target_id}")
    return f"已断开 {bucket_id} 与 {target_id} 的关联（双向）。"


async def _apply_retype(bucket_id: str, target_id: str, relation_type: str) -> str:
    # 类型合法性已在 validate() 拦过，这里可以直接归一化
    forward = normalize_relation_type(relation_type)
    backward = reverse_relation_type(forward)

    def _mutation(left_post, right_post):
        left_links = _read_links(left_post)
        right_links = _read_links(right_post)
        if left_links is None or right_links is None:
            return False, False, "broken"
        # 只要有一侧挂着这条关系就算存在——单向残留也应当能被修正，
        # 修完顺带把两边对齐。
        targets_left = {
            str(link.get("target_bucket_id") or "").strip() for link in left_links
        }
        targets_right = {
            str(link.get("target_bucket_id") or "").strip() for link in right_links
        }
        if target_id not in targets_left and bucket_id not in targets_right:
            return False, False, "missing"

        merged_left, left_changed = retype_relation(left_links, target_id, forward)
        merged_right, right_changed = retype_relation(right_links, bucket_id, backward)
        if left_changed:
            left_post["relation_links"] = normalize_relation_links(merged_left)
        if right_changed:
            right_post["relation_links"] = normalize_relation_links(merged_right)
        return left_changed, right_changed, left_changed or right_changed

    result = await rt.bucket_mgr.mutate_relation_pair(bucket_id, target_id, _mutation)
    if result == "broken":
        raise ToolInputError("这两条记忆的 relation_links 数据有问题，无法自动修改；本次未修改。")
    if result == "missing":
        raise ToolInputError(f"{bucket_id} 与 {target_id} 之间没有已存在的关系，relink 只能修改"
            "已有关系的类型，不能凭空建立；本次未修改。")
    if result is None:
        raise ToolInputError("找不到要修改的记忆文件；本次未修改。")
    if not result:
        return f"{bucket_id} 与 {target_id} 的关系已经是 {forward}；本次未修改。"
    rt.logger.info(
        f"op=trace_relation action=relink {bucket_id} x {target_id} type={forward}"
    )
    return (
        f"已把 {bucket_id} 与 {target_id} 的关系改为 {forward}"
        f"（对侧记为 {backward}），并标记为手动关系，不会再被自动推断改写。"
    )
