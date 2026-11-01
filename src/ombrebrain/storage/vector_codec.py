"""向量在 embeddings.db 里的两种存法。

历史上向量以 JSON 文本落库。一个 4096 维向量的 JSON 约 80KB，读回来要先
`json.loads` 再逐元素 `float()`——2300 个桶的库单次检索要解析约 350MB 文本，
实测占掉 20 秒里的 19.98 秒（上游 issue #115）。改成 float32 紧凑字节后，
`np.frombuffer` 是零拷贝视图，这一步接近免费，体积也降到约 1/5。

**两种格式永久共存，不做迁移。** SQLite 是动态类型：BLOB 存进 TEXT 亲和性的列
不会被强转，读出来 Python 直接拿到 `bytes`，靠类型就能分辨。老的 JSON 行解出来
仍然是对的，只是慢；那个桶下次重新向量化时自然就变成 BLOB 了。写一个一次性迁移
脚本去改存量数据，收益是让老行快一点，风险是动一个还在被后台 outbox 写的库——
不值。

float32 而不是 float64：余弦相似度只用来排序，float32 的误差在 1e-7 量级，
而体积差一倍。矩阵运算本身仍在 float64 里做。
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

VECTOR_DTYPE = np.float32
_ITEM_BYTES = np.dtype(VECTOR_DTYPE).itemsize


def encode_vector(values: Any) -> bytes:
    """把一串数字编成落库用的紧凑字节。"""
    array = np.asarray(values, dtype=VECTOR_DTYPE)
    if array.ndim != 1:
        raise ValueError("embedding must be one-dimensional")
    if array.size == 0:
        raise ValueError("embedding is empty")
    if not np.isfinite(array).all():
        raise ValueError("embedding contains a non-finite value")
    return array.tobytes()


def decode_vector(raw: Any) -> np.ndarray:
    """从库里读出的值解回向量；两种格式都吃，坏数据一律抛异常。

    返回只读视图（BLOB 那条路是零拷贝），调用方不要原地改它。
    """
    if isinstance(raw, (bytes, bytearray, memoryview)):
        data = bytes(raw)
        if not data or len(data) % _ITEM_BYTES:
            raise ValueError(
                f"embedding blob is {len(data)} bytes, not a multiple of {_ITEM_BYTES}"
            )
        return np.frombuffer(data, dtype=VECTOR_DTYPE)
    if not isinstance(raw, str):
        raise TypeError(f"embedding is {type(raw).__name__}, not bytes or str")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise TypeError(f"embedding is {type(parsed).__name__}, not list")
    if not parsed:
        raise ValueError("embedding is empty")
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise TypeError("embedding contains a non-numeric item")
    array = np.asarray(parsed, dtype=VECTOR_DTYPE)
    if not np.isfinite(array).all():
        raise ValueError("embedding contains a non-finite value")
    return array


def is_valid_stored_vector(raw: Any) -> bool:
    """诊断用：这个单元格能不能解成一个向量。不抛异常。"""
    try:
        return decode_vector(raw).size > 0
    except (ValueError, TypeError, json.JSONDecodeError, RecursionError):
        return False
