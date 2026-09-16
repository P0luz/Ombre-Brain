#!/usr/bin/env python3
"""
========================================
write_memory.py — 手动写入记忆的命令行小工具
========================================

不走 MCP、不走 HTTP，直接把一条记忆写成一个 .md 文件。
主要用于调试 / 在 Copilot 端快速补东西 / API 不可用时的底圈。

关键行为：
- 两种用法：命令行参数、或交互 input
- 路径优先级：OMBRE_BUCKETS_DIR > config.yaml > 内置默认
- 必填：name / content；可选：domain / tags / valence / arousal / importance
- 写入 dynamic/ 目录，生成 12 位 hex bucket_id

不做什么（边界）：
- 不调 LLM、不做 analyze、不做合并查重
- 不启动 BucketManager，直接拼 frontmatter 写文件
- 不直接生成 embedding（服务启动对账 / decay 自愈 / 手动 backfill 会排队补齐）

对外暴露：CLI 入口。
========================================
"""

import os
import tempfile
import uuid
import argparse
import math

import frontmatter

from ombrebrain.eventsourcing.footprint import cli_origin
from runtime_owner import admitted_sync
from snapshot_barrier import markdown_writer_turn_sync
from tools import _identity
from utils import load_config, now_iso


_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_MAX_METADATA_ITEMS = 64
_MAX_METADATA_CHARS = 128


def _resolve_dynamic_dir() -> str:
    """
    Resolve the `dynamic/` directory under the configured bucket root.
    Priority: $OMBRE_BUCKETS_DIR > config.yaml > built-in default.
    优先级：环境变量 > config.yaml > 内置默认。
    """
    env_dir = (
        os.environ.get("OMBRE_VAULT_DIR", "").strip()
        or os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
    )
    if env_dir:
        return os.path.join(os.path.expanduser(env_dir), "dynamic")
    try:
        cfg = load_config()
        return os.path.join(cfg["buckets_dir"], "dynamic")
    except Exception:
        # Fallback to project-local ./buckets/dynamic
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "buckets", "dynamic"
        )


VAULT_DIR = _resolve_dynamic_dir()


def gen_id():
    return uuid.uuid4().hex[:12]


def _atomic_create_text(path: str, text: str) -> None:
    """Publish a new CLI bucket without replacing an existing file."""
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(
        dir=directory, prefix=".write-memory-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        os.remove(temporary)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _max_bucket_bytes() -> int:
    try:
        raw = (load_config().get("limits") or {}).get(
            "max_bucket_bytes", _DEFAULT_MAX_BUCKET_BYTES
        )
        value = int(raw)
    except (TypeError, ValueError, OverflowError, OSError):
        return _DEFAULT_MAX_BUCKET_BYTES
    return value if value >= 0 else _DEFAULT_MAX_BUCKET_BYTES


def _bounded_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()[:_MAX_METADATA_CHARS]
        if text and text not in result:
            result.append(text)
        if len(result) >= _MAX_METADATA_ITEMS:
            break
    return result


def _finite_unit(value: float, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(numeric):
        return default
    return max(0.0, min(1.0, numeric))


def write_memory(
    name: str,
    content: str,
    domain: list[str],
    tags: list[str],
    owner: str,
    importance: int = 7,
    valence: float = 0.5,
    arousal: float = 0.3,
):
    name = str(name or "").strip()[:120]
    content = str(content or "")
    if not name:
        raise ValueError("name cannot be empty")
    if not content.strip():
        raise ValueError("content cannot be empty")
    cap = _max_bucket_bytes()
    size = len(content.encode("utf-8"))
    if cap > 0 and size > cap:
        raise ValueError(
            f"content exceeds max_bucket_bytes ({size} > {cap})"
        )

    target_owner = str(owner or "").strip().lower().replace("-", "_")
    if target_owner == "human" or target_owner not in _identity.known_owner_values():
        raise ValueError("CLI creation requires one explicit validated target owner")
    tags = _identity.ensure_write_owner(tags, caller=target_owner)
    if _identity.strict_owner_of({"tags": tags}) != target_owner:
        raise ValueError("CLI owner tag must exactly match --owner")

    mid = gen_id()
    now = now_iso()
    try:
        normalized_importance = max(1, min(10, int(importance)))
    except (TypeError, ValueError, OverflowError):
        normalized_importance = 7
    metadata = {
        "activation_count": 0,
        "arousal": _finite_unit(arousal, 0.3),
        "created": now,
        "domain": _bounded_strings(domain) or ["未分类"],
        "id": mid,
        "importance": normalized_importance,
        "last_active": now,
        "name": name,
        "tags": _bounded_strings(tags),
        "type": "dynamic",
        "valence": _finite_unit(valence, 0.5),
        "footprint_origin": cli_origin(),
    }
    post = frontmatter.Post(content, **metadata)
    path = os.path.join(VAULT_DIR, f"{mid}.md")
    vault_root = os.path.dirname(VAULT_DIR)
    with admitted_sync():
        with markdown_writer_turn_sync(vault_root):
            os.makedirs(VAULT_DIR, exist_ok=True)
            _atomic_create_text(path, frontmatter.dumps(post))

    print(f"✓ 已写入: {path}")
    print(f"  ID: {mid} | 名称: {name}")
    return mid


def interactive():
    print("=== Ombre Brain 手动写入 ===")
    name = input("记忆名称: ").strip()
    content = input("内容: ").strip()
    domain = [d.strip() for d in input("主题域(逗号分隔): ").split(",") if d.strip()]
    tags = [t.strip() for t in input("标签(逗号分隔): ").split(",") if t.strip()]
    owner = input("目标 owner（必填）: ").strip()
    importance = int(input("重要性(1-10, 默认7): ").strip() or "7")
    valence = float(input("效价(0-1, 默认0.5): ").strip() or "0.5")
    arousal = float(input("唤醒(0-1, 默认0.3): ").strip() or "0.3")
    write_memory(name, content, domain, tags, owner, importance, valence, arousal)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="手动写入 Ombre Brain 记忆")
    parser.add_argument("--name", help="记忆名称")
    parser.add_argument("--content", help="记忆内容")
    parser.add_argument("--domain", help="主题域,逗号分隔")
    parser.add_argument("--tags", help="标签,逗号分隔")
    parser.add_argument("--owner", required=False, help="目标 owner（必填）")
    parser.add_argument("--importance", type=int, default=7)
    parser.add_argument("--valence", type=float, default=0.5)
    parser.add_argument("--arousal", type=float, default=0.3)
    args = parser.parse_args()

    if args.name and args.content and args.domain and args.owner:
        write_memory(
            name=args.name,
            content=args.content,
            domain=[d.strip() for d in args.domain.split(",")],
            tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
            owner=args.owner,
            importance=args.importance,
            valence=args.valence,
            arousal=args.arousal,
        )
    else:
        interactive()
