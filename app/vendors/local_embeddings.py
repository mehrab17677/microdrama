"""Open-source embedding backend for tests (EMBEDDING_BACKEND=local): all-MiniLM-L6-v2 on CPU.

NOT for production. The database stores 1536-number vectors (OpenAI text-embedding-3-small); MiniLM gives
384, so each vector is padded with zeros. Padding leaves cosine similarity between two such vectors
unchanged, so retrieval and the similarity checks behave correctly. Vectors from this backend and from
OpenAI live in different spaces and must not be mixed in one database.
English only; long text is truncated to the model's 256 word pieces.
"""
from __future__ import annotations

import os
import threading

DIMS = 1536
MODEL_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"
_lock = threading.Lock()
_model: tuple | None = None


def model_id() -> str:
    return os.environ.get("LOCAL_EMBED_MODEL", MODEL_DEFAULT)


def _load():
    global _model
    with _lock:
        if _model is None:
            from transformers import AutoModel, AutoTokenizer

            tok = AutoTokenizer.from_pretrained(model_id())
            mod = AutoModel.from_pretrained(model_id()).eval()
            _model = (tok, mod)
        return _model


def pad(vec: list[float], dims: int = DIMS) -> list[float]:
    if len(vec) > dims:
        raise ValueError(f"embedding has {len(vec)} numbers; the database column holds {dims}")
    return vec + [0.0] * (dims - len(vec))


def embed(texts: list[str]) -> list[list[float]]:
    import torch

    tok, mod = _load()
    out: list[list[float]] = []
    with _lock, torch.no_grad():
        for i in range(0, len(texts), 32):
            batch = tok(texts[i:i + 32], padding=True, truncation=True, max_length=256, return_tensors="pt")
            hidden = mod(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)   # mean pooling
            pooled = torch.nn.functional.normalize(pooled, dim=1)
            out.extend(pad(v.tolist()) for v in pooled)
    return out
