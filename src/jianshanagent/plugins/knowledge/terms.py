"""关键词检索用的「词」：不分词，不用词典。

- 连续的汉字（含日文假名）拆成相邻两个字一组：「大天狗的技能」→ 大天、天狗、狗的、的技、技能。
  单独一个字的就用这个字。
- 连续的字母数字整个算一个词，转小写；数字里的千分位逗号去掉（「17,613,660」→ 17613660）。
- 其余符号（标点、空格、「·」）只起分隔作用。

这样新出的式神名、活动名、玩家简称不用维护任何词表就能查到。
「的技」这种没意义的组合到处都有，靠下面的权重压下去：一个词出现在越少的切片里越值钱
（BM25 里的 IDF），出现在太多切片里的直接不用。

打分用标准的 BM25（``bm25_score``）：查询里的每个词，按它的权重、在这一片里出现了几次、
这一片有多长来算分再相加。出现次数多加分但有上限，篇幅长的要打折（长文章什么词都容易沾上）。

切片入库时和查询时必须用同一个函数，所以两边都从这里取。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff"
_RUN = re.compile(rf"[{_CJK}]+|[a-z0-9]+(?:[.,][0-9]+)*")
_CJK_START = re.compile(rf"[{_CJK}]")


#: BM25 的两个常数，用的是通用的默认值：K1 管「多出现几次能多加多少分」，B 管「篇幅长打多少折」
BM25_K1 = 1.2
BM25_B = 0.75


def term_counts(text: str) -> Counter[str]:
    """一段文字里每个词出现了几次。"""
    counts: Counter[str] = Counter()
    for match in _RUN.finditer(unicodedata.normalize("NFKC", text).lower()):
        run = match.group(0)
        if _CJK_START.match(run):
            if len(run) == 1:
                counts[run] += 1
            else:
                counts.update(run[i : i + 2] for i in range(len(run) - 1))
        else:
            counts[run.replace(",", "")] += 1
    return counts


def extract_terms(text: str) -> set[str]:
    """一段文字里出现过的词（不重复，不计次数）。"""
    return set(term_counts(text))


def term_weights(
    terms: Iterable[str], df: Mapping[str, int], total: int, *, max_df_ratio: float = 0.2
) -> dict[str, float]:
    """查询里每个词的权重。库里没有的词、出现在超过 ``max_df_ratio`` 的切片里的词不要。

    ``df``：词 → 含有它的切片数；``total``：切片总数。
    """
    weights: dict[str, float] = {}
    if total <= 0:
        return weights
    ceiling = max(max_df_ratio * total, 1.0)
    for term in set(terms):
        count = df.get(term, 0)
        if count <= 0 or count > ceiling:
            continue
        weights[term] = math.log(1.0 + (total - count + 0.5) / (count + 0.5))
    return weights


def bm25_score(weights: Mapping[str, float], text: str, average_chars: float) -> float:
    """一片文字对一个查询的 BM25 得分。``weights`` 来自 ``term_weights``，篇幅按字数算。"""
    counts = term_counts(text)
    length = len(text) / average_chars if average_chars > 0 else 1.0
    norm = BM25_K1 * (1.0 - BM25_B + BM25_B * length)
    score = 0.0
    for term, weight in weights.items():
        tf = counts.get(term, 0)
        if tf:
            score += weight * tf * (BM25_K1 + 1.0) / (tf + norm)
    return score
