"""把对外声明的 inputSchema 压成「每个属性都带 type」的形状。

## 为什么需要这一层

Gemini 的 functionDeclaration 用的是 OpenAPI 3.0 的一个子集，它的 Schema 对象
里 `type` 是 **Required**（见 generativelanguage v1beta discovery 文档）。而
`Optional[X]` 经 pydantic 生成的是

    {"anyOf": [{"type": "X"}, {"type": "null"}], "default": ...}

——`anyOf` 本身 Gemini 是认的，但这一层**没有 type**，于是整个请求被拒：

    functionDeclaration `<tool>.<param>` schema didn't specify the schema type field

一个不合格就整批失败，所以这不是某个工具的问题：14/16 个工具、106 个参数全在
这个形状上。Claude / OpenAI 接受 anyOf，所以此前一直没暴露。

## 为什么不直接把注解里的 Optional 删掉

`Optional` 在这里是承重的。`tools/breath/__init__.py` 有一整段 Null-safe
coercion，说明真的有客户端在传 `null`。把注解改成 `str = ""` 之后 pydantic 会
开始拒绝 `null`，等于用一个兼容性问题换另一个。

FastMCP 里**对外 schema 与运行时校验器是两个独立的东西**：`tool.parameters` 是
发出去的 dict，`tool.fn_metadata.arg_model` 才是校验用的 pydantic 模型。只改前者
不影响后者——老客户端照样传得进 `null`，Gemini 那边看到的则是干净的 type。

## 丢掉的是「告诉模型你可以这么传」，不是能力

联合类型在 OpenAPI 3.0 里没有对应写法，必须挑一个主类型。挑掉的那一支运行时
仍然收，只是不再出现在工具声明里。下面每一条都注明了挑哪边、以及为什么。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ombre_brain.strict_schema")

_NULL = {"type": "null"}

# 去掉 null 分支之后仍然剩多支的联合类型。OpenAPI 3.0 表达不了真联合，只能挑一个。
_UNION_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    # media 的字符串形只是 {"path": ...} 的简写，信息量与字典形一样；而
    # data_base64 那一支是 Dashboard / 导入侧的用法，模型不会手打 base64。
    # 所以挑数组+字符串：保住「一次多项」这个模型真会用的能力。
    ("hold", "media"): {"type": "array", "items": {"type": "string"}},
    ("trace", "media_append"): {"type": "array", "items": {"type": "string"}},
    ("trace", "media_replace"): {"type": "array", "items": {"type": "string"}},
}

# `Optional[list]` 压平后是 {"items": {}, "type": "array"}——items 也是 Schema，
# 同样要 type。这里给每个 list 参数定元素类型。
_ITEM_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    # [[1, 20], ...]：1-based 闭区间，无损。
    ("hold", "source_ranges"): {"type": "array", "items": {"type": "integer"}},
    # 引语挑的是**字典**那一边，和 media 相反。字符串形只是简写，而字典形带
    # speaker / at——压成字符串会让 Gemini 上的模型永远记不下「这句是谁说的」，
    # 那是 rule.md 第 16 条要守的东西。丢简写可以，丢归属不行。
    ("hold", "quotes"): {"type": "object"},
    ("trace", "quotes_replace"): {"type": "object"},
    # list[str]，见 metadata_normalize._normalize_meaning_list。
    ("trace", "meaning_replace"): {"type": "string"},
    # 结构化条目。属性不在这里展开：形状已经写在工具的文档串里，在两处各写一遍
    # 只会漂移。
    ("grow", "items"): {"type": "object"},
    ("You", "bucket_ids"): {"type": "string"},
    ("Them", "bucket_ids"): {"type": "string"},
    ("Them", "names"): {"type": "string"},
}

_CARRY_OVER = ("default", "title", "description")


def _flatten_property(tool: str, name: str, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema

    override = _UNION_OVERRIDES.get((tool, name))
    if override is not None:
        merged = {k: schema[k] for k in _CARRY_OVER if k in schema}
        merged.update(override)
        return merged

    if "anyOf" in schema:
        branches = [b for b in schema["anyOf"] if b != _NULL]
        if len(branches) == 1:
            merged = {k: schema[k] for k in _CARRY_OVER if k in schema}
            merged.update(branches[0])
            schema = merged
        else:
            logger.warning(
                "%s.%s 的 anyOf 去掉 null 之后仍有 %d 支，没有对应的压平规则；"
                "严格校验的客户端（Gemini）会拒绝整批工具。",
                tool, name, len(branches),
            )
            return schema

    if schema.get("type") == "array" and not schema.get("items", {}).get("type"):
        item = _ITEM_OVERRIDES.get((tool, name))
        if item is None:
            logger.warning(
                "%s.%s 是数组但 items 没有 type，也没有对应的压平规则；"
                "严格校验的客户端（Gemini）会拒绝整批工具。",
                tool, name,
            )
        else:
            schema = dict(schema, items=item)

    return schema


def harden(tool_name: str, parameters: Any) -> Any:
    """返回压平后的 inputSchema。原对象不改，方便调用方自己决定要不要替换。"""
    if not isinstance(parameters, dict):
        return parameters
    props = parameters.get("properties")
    if not isinstance(props, dict) or not props:
        return parameters
    return dict(
        parameters,
        properties={k: _flatten_property(tool_name, k, v) for k, v in props.items()},
    )


def harden_tool(tool: Any, tool_name: str) -> None:
    """就地压平一个 FastMCP tool 的对外 schema。校验模型不动。"""
    if tool is None:
        return
    tool.parameters = harden(tool_name, getattr(tool, "parameters", None))


def harden_registered_tools(mcp: Any) -> int:
    """压平当前已注册的全部工具，返回处理了几个。

    动态挂载的 You / Them 在各自的门里另外调 harden_tool——它们可能在这之后
    才被挂上来。`tests/test_advertised_schema_is_strict.py` 兜底：任何一条经
    list_tools 发出去的属性缺 type 都会红，所以漏掉一个调用点会被 CI 抓住。
    """
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        return 0
    count = 0
    for name, tool in list(getattr(manager, "_tools", {}).items()):
        harden_tool(tool, name)
        count += 1
    return count
