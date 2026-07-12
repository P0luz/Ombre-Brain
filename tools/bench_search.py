#!/usr/bin/env python3
"""
tools/bench_search.py — search_similar 相似度计算基准脚本

这是什么：只跑纯计算（不接 embedding API、不建 EmbeddingEngine 实例），
          用随机向量对比「逐对 Python 循环」（旧路径）和「numpy 矩阵路径」
          （search_similar_strict 现在用的路径）算 top-k 余弦相似度的耗时。
做什么：生成 N 条 D 维随机向量 + 1 条 query，各跑 --repeat 次取平均，打印
        两条路径的耗时和加速比。
不做什么：不读 vault、不连数据库、不需要 API key，纯本地 CPU 计算基准。

用法：
    python tools/bench_search.py                       # 默认 92 条 / 1024 维 / top_k=10
    python tools/bench_search.py --n 10000 --dim 1024   # 外推大规模场景
    python tools/bench_search.py --n 92 --dim 1024 --repeat 200
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

from embedding_engine import EmbeddingEngine  # noqa: E402


def _legacy_top_k(query: list[float], vectors: list[list[float]], top_k: int) -> list[tuple[int, float]]:
    """旧实现：逐对调用 _cosine_similarity + 全量排序取前 top_k。"""
    scored = [(i, EmbeddingEngine._cosine_similarity(query, v)) for i, v in enumerate(vectors)]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def _vectorized_top_k(query: list[float], vectors: list[list[float]], top_k: int) -> list[tuple[int, float]]:
    """新实现：_cosine_similarity_batch 一次矩阵乘法 + argpartition 取 top_k。"""
    sims = EmbeddingEngine._cosine_similarity_batch(query, vectors)
    n = len(vectors)
    k = max(0, min(top_k, n))
    if k == 0:
        return []
    if k < n:
        top_idx = np.argpartition(-sims, k - 1)[:k]
    else:
        top_idx = np.arange(n)
    top_idx = top_idx[np.argsort(-sims[top_idx], kind="stable")]
    return [(int(i), float(sims[i])) for i in top_idx]


def _time_fn(fn, *args, repeat: int) -> float:
    """跑 repeat 次，返回平均耗时（毫秒）。先跑一次热身不计时。"""
    fn(*args)
    start = time.perf_counter()
    for _ in range(repeat):
        fn(*args)
    elapsed = time.perf_counter() - start
    return elapsed / repeat * 1000.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark: pure-Python loop vs numpy-vectorized cosine top-k")
    parser.add_argument("--n", type=int, default=92, help="Number of stored vectors (default: 92)")
    parser.add_argument("--dim", type=int, default=1024, help="Vector dimension (default: 1024)")
    parser.add_argument("--top-k", type=int, default=10, help="Top-K to retrieve (default: 10)")
    parser.add_argument("--repeat", type=int, default=100, help="Repetitions to average over (default: 100)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    args = parser.parse_args(argv)

    random.seed(args.seed)
    query = [random.uniform(-1, 1) for _ in range(args.dim)]
    vectors = [[random.uniform(-1, 1) for _ in range(args.dim)] for _ in range(args.n)]

    legacy_result = _legacy_top_k(query, vectors, args.top_k)
    vectorized_result = _vectorized_top_k(query, vectors, args.top_k)
    legacy_ids = [i for i, _ in legacy_result]
    vectorized_ids = [i for i, _ in vectorized_result]
    assert legacy_ids == vectorized_ids, (
        f"结果不一致，基准数据不可信：legacy={legacy_ids} vectorized={vectorized_ids}"
    )

    legacy_ms = _time_fn(_legacy_top_k, query, vectors, args.top_k, repeat=args.repeat)
    vectorized_ms = _time_fn(_vectorized_top_k, query, vectors, args.top_k, repeat=args.repeat)
    speedup = legacy_ms / vectorized_ms if vectorized_ms > 0 else float("inf")

    print(f"n={args.n} dim={args.dim} top_k={args.top_k} repeat={args.repeat}")
    print(f"legacy (pure Python loop):   {legacy_ms:.4f} ms")
    print(f"vectorized (numpy matmul):   {vectorized_ms:.4f} ms")
    print(f"speedup:                     {speedup:.1f}x")
    print(f"result consistency:          OK (top-{args.top_k} ids identical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
