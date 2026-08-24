#!/usr/bin/env python3
"""
backfill_relations — 给存量桶回填自动关系边（≈ 同刻 / ↔ 相关 / ← 之前）。

自动建边只在「新桶写入那一刻」触发，从不回填存量老桶。跑一次本脚本，
就能让回溯写入的老桶彼此之间也出现 same_event / related_to 的关系。

前置：存量桶需先有 embedding（先跑 backfill_embeddings.py）。

用法：
    python backfill_relations.py            # 回填
    python backfill_relations.py --dry-run  # 只看会处理多少桶，不写盘
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from utils import load_config, setup_logging  # noqa: E402
from bucket_manager import BucketManager  # noqa: E402
from embedding_engine import EmbeddingEngine  # noqa: E402
import tools._runtime as rt  # noqa: E402
from tools._relation_link import backfill_auto_relations  # noqa: E402


async def main(dry_run: bool = False) -> None:
    config = load_config()
    setup_logging(config.get("log_level", "INFO"))
    engine = EmbeddingEngine(config)
    bucket_mgr = BucketManager(config, embedding_engine=engine)
    rt.init(
        bucket_mgr=bucket_mgr,
        embedding_engine=engine,
        logger=logging.getLogger("backfill_relations"),
    )

    if not engine.enabled:
        print("ERROR: embedding 未启用（缺 API key？）。自动关系回填依赖向量相似度。")
        return

    all_buckets = await bucket_mgr.list_all(include_archive=False)
    print(f"活跃桶：{len(all_buckets)}")

    if dry_run:
        eligible = [b for b in all_buckets if (b.get("content") or "").strip()]
        print(f"会处理 {len(eligible)} 个非空桶（dry-run，不写盘）。")
        return

    result = await backfill_auto_relations(all_buckets)
    print(
        f"=== 回填完成：扫描 {result['scanned']} 个桶，"
        f"新建 {result['built']} 条关系 ==="
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
