"""Local vector RAG for avatar knowledge bases.

Embeddings run fully on-device via fastembed (ONNX, no torch, no API key), so
uploaded documents never leave the machine. Used both at ingest time
(`rag_ingest.py`) and at query time (the agent's per-turn retrieval).
"""

from __future__ import annotations

import pathlib

import numpy as np
from fastembed import TextEmbedding

# Keep the model cache inside the project so it persists (the fastembed default
# lands in the OS temp dir, which can be cleared).
_CACHE_DIR = pathlib.Path(__file__).resolve().parents[1] / ".fastembed_cache"
_MODEL_NAME = "BAAI/bge-small-en-v1.5"  # 384-dim, fast on CPU

_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(model_name=_MODEL_NAME, cache_dir=str(_CACHE_DIR))
    return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    """Return L2-normalized embeddings (so a dot product == cosine similarity)."""
    vecs = list(_get_model().embed(list(texts)))
    arr = np.asarray(vecs, dtype=np.float32)
    if arr.size == 0:
        return arr.reshape(0, 384)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Paragraph-aware character chunking. Packs paragraphs up to ~`size` chars;
    hard-splits any single paragraph longer than `size` (with overlap)."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(1, size - overlap)
            for i in range(0, len(p), step):
                chunks.append(p[i : i + size])
            continue
        if not buf:
            buf = p
        elif len(buf) + 2 + len(p) <= size:
            buf = f"{buf}\n\n{p}"
        else:
            chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def build_index(text: str) -> dict:
    """Chunk + embed text into an in-memory index {chunks, vectors}."""
    chunks = chunk_text(text)
    vectors = embed_texts(chunks) if chunks else np.zeros((0, 384), dtype=np.float32)
    return {"chunks": chunks, "vectors": vectors}


def save_index(path: str, index: dict) -> None:
    np.savez(
        path,
        vectors=index["vectors"].astype(np.float32),
        chunks=np.array(index["chunks"], dtype=object),
    )


def load_index(path: str) -> dict:
    data = np.load(path, allow_pickle=True)
    return {"vectors": data["vectors"].astype(np.float32), "chunks": list(data["chunks"])}


def retrieve(query: str, index: dict, k: int = 6) -> list[str]:
    """Return the top-`k` chunk texts most similar to `query`."""
    chunks = index.get("chunks") or []
    vectors = index.get("vectors")
    if not len(chunks) or vectors is None or len(vectors) == 0:
        return []
    q = embed_texts([query])[0]
    sims = vectors @ q  # both normalized -> cosine similarity
    k = min(k, len(chunks))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    return [chunks[i] for i in top]
