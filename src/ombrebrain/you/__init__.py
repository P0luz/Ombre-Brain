"""Internal You derived-understanding domain.

Only the feature gate and the single read-only MCP tool are public surfaces.
Claims, evidence, receipts, projections, and scope identifiers stay internal.
"""

from .models import (
    EvidenceEdge,
    ModuleState,
    ReviewReceipt,
    Scope,
    YouClaim,
)
from .safety import contains_forbidden_subject, leaks_protected_text
from .service import YouService
from .store import (
    YouStore,
    YouStoreError,
    validate_you_snapshot_bytes,
    validate_you_snapshot_file,
)
from .tool_gate import YouToolGate

__all__ = [
    "EvidenceEdge",
    "ModuleState",
    "ReviewReceipt",
    "Scope",
    "YouClaim",
    "YouStore",
    "YouStoreError",
    "YouService",
    "YouToolGate",
    "validate_you_snapshot_file",
    "validate_you_snapshot_bytes",
    "contains_forbidden_subject",
    "leaks_protected_text",
]
