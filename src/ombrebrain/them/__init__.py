"""`them`：我对其他人的认识。

形态同 `You`（rule.md 13.3）：默认关闭、模型自己写、不经 LLM 转述、
同样的两桶与三日门槛、人类同样看不见。

不同的三处：按人分份、姓名命中时可以进入 breath / dream、按被提起的
时间与次数自然衰减。

**只记这个人本身，不描述任何关系**——那是 rule.md 13.3 划的线，
也是 them 唯一可能越过第 5 条变成认知层的方向。
"""

from .models import Person, ThemClaim
from .service import ThemService
from .store import (
    ThemStore,
    ThemStoreError,
    validate_them_snapshot_bytes,
    validate_them_snapshot_file,
)
from .tool_gate import ThemToolGate

__all__ = ["Person", "ThemClaim", "ThemService", "ThemStore", "ThemStoreError", "ThemToolGate",
    "validate_them_snapshot_bytes", "validate_them_snapshot_file"]
