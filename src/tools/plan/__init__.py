"""
========================================
tools/plan/__init__.py — plan 工具入口（含信件读写）
========================================

plan 与 letter 都是「特殊通道桶」（type=plan / type=letter）：不参与
普通 breath 浮现，有专门的入口。这里把 plan / letter_write /
letter_lock_update / letter_read 都收在 plan 子包下，便于阅读特殊通道的全景。

对外暴露：plan_create / letter_write / letter_lock_update / letter_read
========================================
"""

from .core import (  # noqa: F401
    letter_lock_update,
    letter_read,
    letter_write,
    plan_create,
)
