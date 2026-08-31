"""
========================================
tools/trace/__init__.py — trace 工具入口
========================================

trace 是「我修正/更新某条记忆」。整个 trace 没有真正多分支，所以
只放一个 core.py 实现。这里仅做 dispatch 转发。

对外暴露：dispatch(...) → str（参数与 server.py 中的 trace tool 同名）
========================================
"""

from errors import ToolInputError
from ombrebrain.storage.media_store import MediaPersistenceError

from .core import trace_core


async def dispatch(*args, **kwargs) -> str:
    """转发到 trace_core，只多做一件事：翻译媒体存储失败。

    trace 的 media_append / media_replace 与 hold 走同一个 MediaStore。
    存储层抛的 MediaPersistenceError 消息（「请改传 data_base64」）正是
    调用方需要的那句话，不翻译就会被 server 的通用兜底当成未预期异常，
    正文整个隐藏，调用方只知道失败、不知道怎么改。
    """
    try:
        return await trace_core(*args, **kwargs)
    except MediaPersistenceError as exc:
        raise ToolInputError(str(exc)) from exc
