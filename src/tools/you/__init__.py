"""可选 You 模块的单一 MCP 入口：读回 / 写入重申 / 撤回，全程不走 LLM。"""

from .core import dispatch

__all__ = ["dispatch"]
