#!/usr/bin/env python3
"""
backfill_feel_links — 给存量 feel 桶回填 references 边（感受 → 源事件）。

旧版 feel 写入时 source_bucket 只存 triggered_by 字段、不落关系边。3.9.0 起
新写的 feel 会落 references 边 + referenced_by 反边，但存量老 feel 仍是两头断
（读感受看不到事件、读事件看不到感受）。跑一次本脚本，把 triggered_by 落成：
  feel --references--> 源事件
  源事件 --referenced_by--> feel（反边）

幂等：已有 references 边的 feel 跳过，重复跑不重复建。不依赖 embedding。

用法：
    python backfill_feel_links.py            # 回填
    python backfill_feel_links.py --dry-run  # 只看会处理多少 feel，不写盘
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
import tools._runtime as rt  # noqa: E402
from tools._relation_link import collect_missing_feel_links, backfill_feel_links  # noqa: E402


async def main(dry_run: bool = False) -> None:
    config = load_config()
    setup_logging(config.get("log_level", "INFO"))
    bucket_mgr = BucketManager(config)
    rt.init(
        bucket_mgr=bucket_mgr,
        logger=logging.getLogger("backfill_feel_links"),
    )

    all_buckets = await bucket_mgr.list_all(include_archive=False)
    feels = [b for b in all_buckets if (b.get("metadata") or {}).get("type") == "feel"]
    print(f"feel 桶：{len(feels)}")

    missing = collect_missing_feel_links(all_buckets)
    print(f"缺 references 边的 feel：{len(missing)}")

    if dry_run:
        for feel_id, source_id in missing:
            print(f"  {feel_id} -> {source_id}")
        print("（dry-run，不写盘。）")
        return

    built = await backfill_feel_links(all_buckets)
    print(f"=== 回填完成：新建 {built} 条边（references + referenced_by）===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
