"""Text -> vectors. Model2Vec (static embeddings: fast, CPU, no PyTorch) by default.

The model is fetched once from the Hugging Face hub into ``<data dir>/models``
and always loaded from there afterwards (offline). That download is the only
network access in Phase 3; file contents never leave the machine.

Vectors are L2-normalized float32 in memory and int8 in the database
(``quantize``: x * 127, rounded), so cosine ~= dot / 127**2.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Protocol

import numpy as np
import numpy.typing as npt

from zachary.core.db import EMBED_DIM

log = logging.getLogger(__name__)

FloatArray = npt.NDArray[np.float32]
Int8Array = npt.NDArray[np.int8]


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> FloatArray:
        """(len(texts), dim) float32, each row L2-normalized (zero rows for empty text)."""
        ...


def normalize(m: FloatArray) -> FloatArray:
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    out: FloatArray = (m / n).astype(np.float32)
    return out


def quantize(v: FloatArray) -> bytes:
    """One normalized vector -> int8 bytes for sqlite-vec / vec_ivf."""
    return np.clip(np.round(v * 127.0), -127, 127).astype(np.int8).tobytes()


def dequantize(b: bytes) -> FloatArray:
    row: FloatArray = normalize(np.frombuffer(b, dtype=np.int8).astype(np.float32)[None, :])[0]
    return row


def dequantize_many(blobs: list[bytes]) -> FloatArray:
    if not blobs:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    m = np.frombuffer(b"".join(blobs), dtype=np.int8).reshape(len(blobs), -1)
    return normalize(m.astype(np.float32))


class Model2VecEmbedder:
    """Model2Vec static model, loaded from a local folder (downloaded on first use)."""

    def __init__(self, model_id: str, models_dir: Path) -> None:
        self.name = model_id
        self.models_dir = models_dir
        self._model: object | None = None
        self._lock = threading.Lock()
        self.dim = EMBED_DIM

    def local_path(self) -> Path:
        return self.models_dir / re.sub(r"[^A-Za-z0-9_.-]+", "--", self.name)

    def _load(self) -> object:
        with self._lock:
            if self._model is not None:
                return self._model
            # Keep the background indexer light: one BLAS/tokenizer thread.
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            os.environ.setdefault("OMP_NUM_THREADS", "1")
            from model2vec import StaticModel

            path = self.local_path()
            if not (path / "config.json").exists():
                log.info("downloading embedding model %s (one time, ~30 MB)", self.name)
                from huggingface_hub import snapshot_download

                path.mkdir(parents=True, exist_ok=True)
                snapshot_download(self.name, local_dir=str(path))
            model = StaticModel.from_pretrained(str(path))
            if model.dim != EMBED_DIM:
                raise ValueError(f"{self.name} has dim {model.dim}, the DB expects {EMBED_DIM}")
            self._model = model
            return model

    def encode(self, texts: list[str]) -> FloatArray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        model = self._load()
        out = model.encode(texts, batch_size=256, show_progress_bar=False)  # type: ignore[attr-defined]
        return normalize(np.asarray(out, dtype=np.float32))


class HashEmbedder:
    """Deterministic bag-of-words embedder for tests: shared words -> similar vectors.

    Each word maps to a fixed random direction; also its 4-letter prefix, so
    "linked"/"link" and "fees"/"fee" overlap like they would in a real model.
    Synonyms can be added so tests can express "meaning" without a model.
    """

    name = "hash-test"

    def __init__(self, synonyms: dict[str, str] | None = None) -> None:
        self.dim = EMBED_DIM
        self.synonyms = synonyms or {}
        self._cache: dict[str, FloatArray] = {}

    def _word(self, w: str) -> FloatArray:
        v = self._cache.get(w)
        if v is None:
            seed = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "little")
            v = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
            self._cache[w] = v
        return v

    def encode(self, texts: list[str]) -> FloatArray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                w = self.synonyms.get(w, w)
                out[i] += self._word(w) + 0.5 * self._word("~" + w[:4])
        return normalize(out)


def models_dir(data_dir: Path) -> Path:
    return data_dir / "models"
