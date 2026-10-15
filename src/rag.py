"""
src/rag.py
==========

离线检索增强（RAG）—— 为推荐解释提供**可核验的知识片段**。

外部 multimodal_ai 使用 BGE+FAISS；本项目离线演示不能依赖重型模型，
因此实现一个纯 Python 的 BM25 检索器（jieba 分词，缺失时退化为字符二元组），
对 data/kb/ 下的知识文档建立索引。

关键约束：检索不到就返回空列表 —— 由 explain 层决定「无证据不编造」，
绝不用 LLM 幻觉补全。
"""
from __future__ import annotations

import math
import re
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

from config import settings

CFG = settings.RAG_CONFIG

# 停止词与标点
_STOP = set("的 了 和 与 是 在 有 我 你 他 她 它 这 那 一个 一些 什么 怎么 吗 呢 都 也 就 很 要 想 请 帮 把 给 个 只 可以 a an the is are of to for and or in on with".split())
_PUNCT = re.compile(r"[\s，。、；：！？“”‘’（）《》【】,.;:!?\"'()\[\]{}<>/\\|~`@#$%^&*_+=\-]+")

_jieba = None
_jieba_warned = False


def _tokenize(text: str) -> list[str]:
    """分词：优先 jieba，失败退化为字符二元组。"""
    global _jieba, _jieba_warned
    text = (text or "").lower()
    if _jieba is None and not _jieba_warned:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import jieba  # type: ignore

            jieba.setLogLevel(60)
            _jieba = jieba
        except Exception:
            _jieba_warned = True
    if _jieba is not None:
        tokens = [t.strip() for t in _jieba.lcut(text)]
        tokens = [t for t in tokens if t and t not in _STOP and not _PUNCT.fullmatch(t)]
        if tokens:
            return tokens
    cleaned = _PUNCT.sub(" ", text)
    cleaned = " ".join(seg for seg in cleaned.split() if seg)
    grams = [cleaned[i:i + 2] for i in range(max(0, len(cleaned) - 1))]
    return [g for g in grams if g.strip()] or [cleaned] if cleaned else []


class _Chunk:
    __slots__ = ("chunk_id", "doc", "text", "tokens", "tf")

    def __init__(self, chunk_id: str, doc: str, text: str):
        self.chunk_id = chunk_id
        self.doc = doc
        self.text = text
        self.tokens = _tokenize(text)
        self.tf = Counter(self.tokens)


class KnowledgeBase:
    """BM25 知识检索器。"""

    def __init__(self, kb_dir: Path | str | None = None) -> None:
        self.kb_dir = Path(kb_dir) if kb_dir else (settings.PROJECT_ROOT / CFG["kb_dir"])
        self.chunks: list[_Chunk] = []
        self._df: Counter[str] = Counter()
        self._avgdl = 0.0
        self._docs: list[str] = []
        self._loaded = False

    # ---------------- 构建 ----------------
    def load(self) -> "KnowledgeBase":
        if self._loaded:
            return self
        if not self.kb_dir.exists():
            self._loaded = True
            return self
        for path in sorted(self.kb_dir.rglob("*")):
            if path.suffix.lower() not in (".md", ".txt"):
                continue
            text = path.read_text(encoding="utf-8-sig")
            self._docs.append(path.name)
            for i, chunk in enumerate(self._split(text)):
                self.chunks.append(_Chunk(f"{path.stem}#{i}", path.name, chunk))
        for chunk in self.chunks:
            for token in set(chunk.tokens):
                self._df[token] += 1
        self._avgdl = (sum(len(c.tokens) for c in self.chunks) / len(self.chunks)) if self.chunks else 0.0
        self._loaded = True
        return self

    def _split(self, text: str) -> list[str]:
        size = CFG["chunk_size"]
        overlap = CFG["chunk_overlap"]
        # 先按段落切，再按长度合并/切分
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks: list[str] = []
        buffer = ""
        for para in paragraphs:
            if len(buffer) + len(para) + 1 <= size:
                buffer = f"{buffer}\n{para}".strip()
            else:
                if buffer:
                    chunks.append(buffer)
                if len(para) <= size:
                    buffer = para
                else:
                    for i in range(0, len(para), max(1, size - overlap)):
                        chunks.append(para[i:i + size])
                    buffer = ""
        if buffer:
            chunks.append(buffer)
        return [c for c in chunks if len(c.strip()) >= 8]

    # ---------------- 检索 ----------------
    def search(self, query: str, k: int | None = None, min_score: float | None = None) -> list[dict[str, Any]]:
        self.load()
        k = k or CFG["top_k"]
        min_score = CFG["min_score"] if min_score is None else min_score
        if not self.chunks:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        n = len(self.chunks)
        avgdl = self._avgdl or 1.0
        k1, b = 1.5, 0.75
        scored: list[tuple[float, _Chunk]] = []
        for chunk in self.chunks:
            dl = len(chunk.tokens) or 1
            score = 0.0
            for token in set(q_tokens):
                if token not in chunk.tf:
                    continue
                df = self._df.get(token, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                tf = chunk.tf[token]
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: (-x[0], x[1].chunk_id))
        if scored:
            peak = scored[0][0] or 1.0
            normalized = [(round(s / peak, 6), c) for s, c in scored]
        else:
            normalized = []
        return [
            {"chunk_id": c.chunk_id, "doc": c.doc, "text": c.text,
             "score": s, "raw_score": round(raw, 6)}
            for (s, c), raw in zip(normalized[:k], [x[0] for x in scored[:k]])
            if s >= min_score
        ]

    def size(self) -> int:
        self.load()
        return len(self.chunks)

    def docs(self) -> list[str]:
        self.load()
        return list(self._docs)


_GLOBAL_KB: KnowledgeBase | None = None


def get_kb() -> KnowledgeBase:
    """进程级单例，避免重复建索引。"""
    global _GLOBAL_KB
    if _GLOBAL_KB is None:
        _GLOBAL_KB = KnowledgeBase().load()
    return _GLOBAL_KB
