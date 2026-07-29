"""Local, offline text embeddings via model2vec (static, no PyTorch).

The organization method uses these two ways, and only these:

  1. cluster the library to DISCOVER which topics exist, so the model only ever
     has to NAME a coherent group of ~10 papers, never invent a taxonomy from the
     whole corpus.
  2. assign a paper its topics by similarity to each topic's prototype -- a cheap
     per-paper match that is multi-label by construction (every topic above a
     threshold), and independent of which cluster the paper happened to fall in.

Static embeddings (potion) are a distilled lookup table: no inference, no torch,
thousands of documents a second on CPU, and small enough to ship. Quality is
lower than a transformer encoder, but the clustering only needs relative
separation, and the model names the clusters, so it is enough.
"""

from __future__ import annotations

import numpy as np

# ~30 MB, downloaded once to the HF cache, then fully offline.
MODEL_NAME = "minishlab/potion-base-8M"
DIM = 256

_model = None


def _load():
    global _model
    if _model is None:
        from model2vec import StaticModel

        _model = StaticModel.from_pretrained(MODEL_NAME)
    return _model


def available() -> bool:
    """True when model2vec is importable; the pipeline degrades gracefully."""
    try:
        import model2vec  # noqa: F401

        return True
    except ImportError:
        return False


def embed(texts: list[str]) -> np.ndarray:
    """L2-normalised float32 embeddings, shape (len(texts), DIM).

    Normalised so a dot product is cosine similarity -- clustering and topic
    assignment both work in cosine space.
    """
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    model = _load()
    vecs = np.asarray(model.encode(texts), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
