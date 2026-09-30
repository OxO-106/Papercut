"""Passage embeddings from Qwen3-Embedding (via Ollama), cached per paper.

Vectors are stored in the paper's folder as embeddings.npz, keyed by a hash
of each passage's text, so re-parsing only embeds passages whose text changed.
"""

import hashlib

import numpy as np

from . import library, llm

MODEL = "qwen3-embedding:0.6b"
# Qwen3-Embedding expects an instruction on queries (not on documents).
QUERY_PREFIX = "Instruct: Given a question about a research paper, retrieve passages from the paper that answer it\nQuery: "


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _path(paper_id: str):
    return library.paper_dir(paper_id) / "embeddings.npz"


def _load(paper_id: str) -> dict[str, np.ndarray]:
    p = _path(paper_id)
    if not p.exists():
        return {}
    with np.load(p, allow_pickle=False) as z:
        if str(z["model"]) != MODEL:
            return {}
        return dict(zip(z["keys"].tolist(), z["vecs"]))


def _save(paper_id: str, cache: dict[str, np.ndarray]) -> None:
    keys = list(cache)
    np.savez_compressed(
        _path(paper_id),
        model=np.array(MODEL),
        keys=np.array(keys),
        vecs=np.stack([cache[k] for k in keys]).astype(np.float16),
    )


def passage_vectors(paper_id: str, texts: list[str]) -> np.ndarray:
    """Unit-length vectors for the passages, embedding only what isn't cached."""
    cache = _load(paper_id)
    missing = [t for t in dict.fromkeys(texts) if _key(t) not in cache]
    for i in range(0, len(missing), 64):
        batch = missing[i:i + 64]
        for t, v in zip(batch, llm.embed(batch, MODEL)):
            cache[_key(t)] = np.asarray(v, dtype=np.float32)
    if missing:
        _save(paper_id, cache)
    m = np.stack([cache[_key(t)].astype(np.float32) for t in texts])
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)


def query_vector(query: str) -> np.ndarray:
    v = np.asarray(llm.embed([QUERY_PREFIX + query], MODEL)[0], dtype=np.float32)
    return v / max(np.linalg.norm(v), 1e-9)
