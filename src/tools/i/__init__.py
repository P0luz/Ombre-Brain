"""
========================================
tools/i/__init__.py — I 工具入口
========================================

I 是「我写下关于我自己的认识」，而且只收沉淀下来的那部分。

写入先落成一条普通记忆（候选），跟别的记忆一起浮现、衰减、进 dream；
被不同日期的 dream 见证够 3 次之后，才能显式升级成正式 I 条目。正式条目
不参与普通 breath/dream（dont_surface=True），SessionStart 时自动带上最近 3 条。

对外暴露：
- dispatch(...) → str（参数与 server.py 中的 I tool 同名）
- record_dream_pass / dream_dates / is_pending_candidate / I_CANDIDATE_TAG /
  I_PROMOTE_THRESHOLD 给 dream 侧复用——候选的状态归 I 管，dream 只负责摆出来看
========================================
"""

from .core import (  # noqa: F401
    I_CANDIDATE_TAG,
    I_PROMOTE_THRESHOLD,
    dream_dates,
    i_core as dispatch,
    is_pending_candidate,
    record_dream_pass,
)
