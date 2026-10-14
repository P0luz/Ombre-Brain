"""自动召回用的中文分词与 BM25。

分词照 memory v2 的 lexical_query：中日韩字符按相邻两字切（bigram），落单的一个字保留；
英文按字母数字切词，去掉极少量停用词。不用 jieba——口语原句、错别字、新词多，
bigram 不靠词典，和 v2 的数字也能对照。

BM25 常数 k1=1.2、b=0.75 是教科书默认值，不是效果阈值。命中门槛（min_score）
另在配置里，由评测扫出来。

对外暴露：tokens(text) / BM25Index。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({"the", "a", "an", "of", "to", "is", "and", "in", "it"})

BM25_K1 = 1.2
BM25_B = 0.75


def tokens(text: str) -> list[str]:
    lowered = str(text or "").lower()
    out = [word for word in _WORD.findall(lowered) if word not in _STOPWORDS]
    run: list[str] = []
    for char in lowered + " ":
        if _CJK.match(char):
            run.append(char)
            continue
        if len(run) == 1:
            out.append(run[0])
        elif run:
            out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
        run = []
    return out


class BM25Index:
    """不可变的内存倒排索引。要更新就整份重建，换引用即可，读的人不用加锁。"""

    def __init__(self, documents: Sequence[str]) -> None:
        self._tf = [Counter(tokens(doc)) for doc in documents]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(documents)
        self._idf = {
            term: math.log(1 + (n - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }
        postings: dict[str, list[int]] = defaultdict(list)
        for index, tf in enumerate(self._tf):
            for term in tf:
                postings[term].append(index)
        self._postings = dict(postings)

    def __len__(self) -> int:
        return len(self._tf)

    def scores(self, query: str | Iterable[str]) -> dict[int, float]:
        terms = set(tokens(query) if isinstance(query, str) else query)
        result: dict[int, float] = defaultdict(float)
        for term in terms:
            idf = self._idf.get(term)
            if not idf:
                continue
            for index in self._postings[term]:
                freq = self._tf[index][term]
                norm = BM25_K1 * (1 - BM25_B + BM25_B * self._len[index] / self._avg)
                result[index] += idf * freq * (BM25_K1 + 1) / (freq + norm)
        return dict(result)
