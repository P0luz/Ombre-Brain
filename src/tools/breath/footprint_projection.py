"""Owner-safe E2 projection for ordinary breath and query results only."""

from __future__ import annotations

from typing import Any

from ombrebrain.eventsourcing.footprint import (
    FootprintOriginError,
    render_origin_line,
)
from utils import count_tokens_approx

from .. import _identity
from .. import _runtime as rt


_SHARED_PROJECTION_OWNERS = frozenset({"shared", "shared_core"})


def projection_line(bucket: dict[str, Any]) -> str:
    """Return a complete origin line only after independent strict admission."""

    caller = _identity.normalize_caller(_identity.get_caller())
    if caller not in _identity.known_callers() or not _identity.is_enabled():
        return ""

    metadata = bucket.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    try:
        owner = _identity.strict_owner_of(metadata)
    except (TypeError, ValueError):
        return ""
    if owner != caller and owner not in _SHARED_PROJECTION_OWNERS:
        return ""

    if "footprint_origin" not in metadata:
        return ""
    try:
        return render_origin_line(metadata["footprint_origin"])
    except (FootprintOriginError, KeyError, TypeError, ValueError):
        # Bounded diagnostic: never log the receipt, enum value, principal,
        # content, tags, path, or metadata.  Bucket id alone identifies the
        # local record that an administrator may inspect.
        logger = getattr(rt, "logger", None)
        if logger is not None:
            logger.warning(
                "footprint projection omitted: bucket_id=%s reason=invalid_origin",
                str(bucket.get("id") or "?")[:64],
            )
        return ""


def append_projection_if_fits(
    text: str,
    bucket: dict[str, Any],
    remaining_tokens: int,
) -> tuple[str, int]:
    """Append the whole E2 line or omit it; never truncate the line."""

    line = projection_line(bucket)
    if not line:
        return text, 0
    addition = "\n" + line
    cost = count_tokens_approx(addition)
    if cost > max(0, int(remaining_tokens)):
        return text, 0
    return text + addition, cost
