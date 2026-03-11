"""Unified search for EnhancedMem: lightest (literal match) and BM25 strategies."""

from __future__ import annotations

import re
from typing import Any, Callable, Literal

from loguru import logger

# Minimal Chinese stopwords for BM25 (common function words)
_CHINESE_STOPWORDS = frozenset(
    {
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
        "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
        "没有", "看", "好", "自己", "这", "那", "什么", "我们", "他们", "可以",
        "因为", "所以", "如果", "但是", "等", "与", "或", "及", "而", "之",
        "它", "她", "他", "被", "把", "让", "给", "对", "从", "向", "以",
    }
)


def extract_episode_text(ep: dict) -> str:
    """Extract searchable text from an episode dict."""
    return " ".join(str(ep.get(k, "")) for k in ("title", "summary", "content"))


def _tokenize_lightest(query: str) -> list[str]:
    """Lightest: simple whitespace split, filter len>=2."""
    return [t for t in query.split() if len(t) >= 2]


def _score_lightest(text: str, terms: list[str]) -> int:
    """Lightest: count term matches in text."""
    return sum(1 for t in terms if t in text)


def _ensure_bm25_deps() -> tuple[Any, Any, Any, Any]:
    """Import BM25 deps; raise ImportError with install hint if missing."""
    try:
        import jieba
    except ImportError as e:
        raise ImportError(
            "retrieve_method=bm25 requires jieba. Install with: pip install jieba"
        ) from e
    try:
        import nltk
        from nltk.corpus import stopwords
        from nltk.stem import PorterStemmer
        from nltk.tokenize import word_tokenize
        from rank_bm25 import BM25Okapi
    except ImportError as e:
        raise ImportError(
            "retrieve_method=bm25 requires rank-bm25 and nltk. "
            "Install with: pip install rank-bm25 nltk"
        ) from e

    # Ensure NLTK data
    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        nltk.download("punkt", quiet=True)
    try:
        nltk.data.find("tokenizers/punkt_tab")
    except LookupError:
        nltk.download("punkt_tab", quiet=True)
    try:
        nltk.data.find("corpora/stopwords")
    except LookupError:
        nltk.download("stopwords", quiet=True)

    stemmer = PorterStemmer()
    stop_words = set(stopwords.words("english"))
    return jieba, BM25Okapi, stemmer, stop_words


def _tokenize_bm25(
    text: str,
    jieba: Any,
    stemmer: Any,
    stop_words: set[str],
) -> list[str]:
    """Tokenize text for BM25 (Chinese: jieba + stopwords; English: nltk)."""
    from nltk.tokenize import word_tokenize

    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))
    if has_chinese:
        tokens = list(jieba.cut(text))
        return [
            t for t in tokens
            if len(t) >= 2 and t.strip() and t not in _CHINESE_STOPWORDS
        ]
    tokens = word_tokenize(text.lower())
    return [
        stemmer.stem(t)
        for t in tokens
        if t.isalpha() and len(t) >= 2 and t not in stop_words
    ]


def search(
    query: str,
    documents: list[Any],
    limit: int,
    strategy: Literal["lightest", "bm25"],
    text_extractor: Callable[[Any], str],
    sort_key_extractor: Callable[[Any], Any] | None = None,
    *,
    bm25_min_score_ratio: float = 0.1,
    bm25_min_score_absolute: float = 0.01,
) -> list[tuple[Any, float]]:
    """
    Unified search over documents. Returns [(doc, score), ...] sorted by score desc.
    When strategy=lightest, only docs with score>0 are returned.
    When strategy=bm25, filters by relevance:
      - If max_score < bm25_min_score_absolute: return [] (query unrelated to corpus).
      - Else keep only results with score >= max_score * bm25_min_score_ratio.
    """
    if not query or not query.strip():
        return []
    query = query.strip()
    if len(query) < 2:
        return []

    if strategy == "lightest":
        terms = _tokenize_lightest(query)
        if not terms:
            return []
        scored: list[tuple[Any, float]] = []
        for doc in documents:
            text = text_extractor(doc)
            score = _score_lightest(text, terms)
            if score > 0:
                sort_key = sort_key_extractor(doc) if sort_key_extractor else ""
                scored.append((doc, float(score)))
        # Sort by (-score, sort_key)
        def key(item: tuple[Any, float]) -> tuple[float, Any]:
            doc, s = item
            sk = sort_key_extractor(doc) if sort_key_extractor else ""
            return (-s, sk)

        scored.sort(key=key)
        return scored[:limit]

    if strategy == "bm25":
        jieba_mod, BM25Okapi, stemmer, stop_words = _ensure_bm25_deps()
        texts = [text_extractor(d) for d in documents]
        tokenized_docs = [
            _tokenize_bm25(t, jieba_mod, stemmer, stop_words) for t in texts
        ]
        bm25 = BM25Okapi(tokenized_docs)
        tokenized_query = _tokenize_bm25(query, jieba_mod, stemmer, stop_words)
        if not tokenized_query:
            return []
        scores = bm25.get_scores(tokenized_query)
        scored = list(zip(documents, scores))
        # Sort by (-score, sort_key)
        def key(item: tuple[Any, float]) -> tuple[float, Any]:
            doc, s = item
            sk = sort_key_extractor(doc) if sort_key_extractor else ""
            return (-s, sk)

        scored.sort(key=key)
        if not scored:
            return []
        max_score = float(scored[0][1])
        # max_score == 0: no term matched any doc -> unrelated
        if max_score == 0:
            return []
        # max_score < 0: small corpus artifact (BM25 can be negative), return top N
        if max_score < 0:
            return scored[:limit]
        # max_score > 0: apply relevance filters
        if max_score < bm25_min_score_absolute:
            return []
        threshold = max(max_score * bm25_min_score_ratio, bm25_min_score_absolute)
        filtered = [(d, s) for d, s in scored if float(s) >= threshold]
        return filtered[:limit]

    logger.warning("Unknown search strategy: {}, falling back to lightest", strategy)
    return search(
        query, documents, limit, "lightest", text_extractor, sort_key_extractor
    )
