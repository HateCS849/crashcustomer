"""Retrieval over a project's attachments (RAG for the scenario planner).

Default ranking is BM25 over word tokens plus Korean character bigrams, so it works without
an embedding model. When OLLAMA_EMBED_MODEL is set, dense cosine ranking is fused with BM25
(reciprocal rank fusion). Retrieved text is reference data for the planner, never instructions.
"""
import math
import os
import re
from collections import Counter
from typing import Awaitable, Callable

import httpx

from .config import OLLAMA_URL

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]
CHUNK_CHARS = 700
TEXT_FORMATS = {"md", "txt", "yaml", "yml", "json", "py", "js", "ts", "tsx", "jsx", "java", "kt", "html", "css",
                "gradle", "xml", "sql", "csv", "properties"}


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    for word in re.findall(r"[가-힣]+", text):
        tokens += [word] if len(word) < 2 else [word[i:i + 2] for i in range(len(word) - 1)]
    return tokens


def split_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    chunks, current = [], ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        while len(block) > size:
            chunks.append(block[:size])
            block = block[size:]
        if len(current) + len(block) + 2 > size and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


def attachment_chunks(attachments: list[dict], read_text: Callable[[str], str | None]) -> list[dict]:
    chunks = []
    for item in attachments:
        source = item["filename"]
        if item.get("category") == "openapi":
            for op in item.get("summary", {}).get("operations", []):
                params = ", ".join(p["name"] for p in op.get("parameters", []) if p.get("required"))
                text = f"{op['method']} {op['path']} {op.get('summary', '')}" + (f" 필수: {params}" if params else "") + (" 인증 필요" if op.get("authenticated") else "")
                chunks.append({"source": source, "kind": "openapi", "text": text.strip()})
            continue
        if item.get("format") not in TEXT_FORMATS:
            continue
        text = read_text(item["id"])
        if text:
            chunks += [{"source": source, "kind": item.get("category", "reference"), "text": part} for part in split_text(text)]
    return chunks


class Retriever:
    def __init__(self, chunks: list[dict], embedder: Embedder | None = None, k1: float = 1.5, b: float = .75):
        self.chunks, self.embedder, self.k1, self.b = chunks, embedder, k1, b
        self.tokens = [Counter(tokenize(c["text"])) for c in chunks]
        self.lengths = [sum(t.values()) for t in self.tokens]
        self.average = sum(self.lengths) / len(self.lengths) if chunks else 0
        frequency = Counter(term for counts in self.tokens for term in counts)
        n = len(chunks)
        self.idf = {term: math.log(1 + (n - df + .5) / (df + .5)) for term, df in frequency.items()}
        self.vectors = None

    def bm25(self, query: str) -> list[float]:
        terms = tokenize(query)
        scores = []
        for counts, length in zip(self.tokens, self.lengths):
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    score += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.average))
            scores.append(score)
        return scores

    async def search(self, query: str, k: int = 4) -> list[dict]:
        if not self.chunks:
            return []
        lexical = self.bm25(query)
        order = sorted(range(len(self.chunks)), key=lambda i: -lexical[i])
        method = "bm25"
        if self.embedder:
            if self.vectors is None:
                self.vectors = await self.embedder([c["text"] for c in self.chunks])
            query_vector = (await self.embedder([query]))[0]
            dense = [cosine(query_vector, v) for v in self.vectors]
            dense_order = sorted(range(len(self.chunks)), key=lambda i: -dense[i])
            fused = Counter()
            for rank, i in enumerate(order):
                fused[i] += 1 / (60 + rank)
            for rank, i in enumerate(dense_order):
                fused[i] += 1 / (60 + rank)
            order = [i for i, _ in fused.most_common()]
            method = "hybrid"
        else:
            order = [i for i in order if lexical[i] > 0]
        return [{**self.chunks[i], "score": round(lexical[i], 3), "method": method} for i in order[:k]]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def ollama_embedder() -> Embedder | None:
    model = os.getenv("OLLAMA_EMBED_MODEL")
    if not model:
        return None

    async def embed(texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.post(OLLAMA_URL + "/api/embed", json={"model": model, "input": texts})
            response.raise_for_status()
            return response.json()["embeddings"]
    return embed


def project_retriever(store, project_id: str, embedder: Embedder | None = None) -> Retriever:
    def read_text(attachment_id: str) -> str | None:
        path = store.uploads / attachment_id
        try:
            return path.read_text(encoding="utf-8-sig")[:200_000] if path.stat().st_size <= 2 * 1024 * 1024 else None
        except (OSError, UnicodeDecodeError):
            return None
    return Retriever(attachment_chunks(store.list_attachments(project_id), read_text), embedder)
